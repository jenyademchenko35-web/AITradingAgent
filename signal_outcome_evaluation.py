"""Observer-only evaluation of published signal episodes against later prices.

The module consumes snapshots which already exist.  It never calls an exchange,
does not alter a decision, and deliberately records PENDING instead of inventing
future evidence.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from runtime_contract import normalize_runtime_timestamp

BASE_DIR = Path(__file__).resolve().parent
EPISODES = "signal_episodes.jsonl"
OUTCOMES = "signal_outcomes.jsonl"
STATE = "signal_evaluation_state.json"
REPORT = "signal_evaluation_report.json"
HORIZONS_HOURS = (1, 4, 12, 24)
ACTIVE_STATUSES = {"WATCH", "SETUP", "HIGH PRIORITY"}
MINIMUM_SAMPLE = 20
DEFAULT_HORIZON_TOLERANCE_SECONDS = 300
STATUS_PRIORITY = {"WATCH": 1, "SETUP": 2, "HIGH PRIORITY": 3}
LOGGER = logging.getLogger(__name__)


def _horizon_tolerance_seconds() -> int:
    """Return a bounded observer-only grace period for delayed snapshots."""
    try:
        return max(0, int(os.getenv(
            "SIGNAL_OUTCOME_HORIZON_TOLERANCE_SECONDS",
            str(DEFAULT_HORIZON_TOLERANCE_SECONDS),
        )))
    except (TypeError, ValueError):
        return DEFAULT_HORIZON_TOLERANCE_SECONDS


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _time(value: Any) -> datetime | None:
    normalized = normalize_runtime_timestamp(value)
    if normalized is None:
        return None
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def _key(row: Mapping[str, Any]) -> str:
    return "|".join((str(row.get("symbol") or "UNKNOWN").upper(), str(row.get("timeframe") or "1h").lower()))


def episode_id(row: Mapping[str, Any]) -> str:
    """Stable identifier based only on immutable signal-time context."""
    seed = "|".join((
        _key(row), str(row.get("side") or row.get("direction") or "UNKNOWN").upper(),
        str(row.get("signal_timestamp") or row.get("timestamp") or ""),
        str(row.get("signal_status") or row.get("signal") or ""),
        str(row.get("decision_id") or row.get("signal_fingerprint") or ""),
    ))
    return "episode-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return fallback


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [payload for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
                if isinstance((payload := json.loads(line)), dict)]
    except (OSError, TypeError, ValueError):
        return []


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(handle.name, path)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def _append_unique(path: Path, rows: list[Mapping[str, Any]], identity: str) -> None:
    existing = {str(row.get(identity)) for row in _read_jsonl(path)}
    pending = [dict(row) for row in rows if str(row.get(identity)) not in existing]
    if not pending:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in pending:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush(); os.fsync(handle.fileno())


def _confidence_bucket(value: Any) -> str:
    number = _number(value)
    if number is None: return "UNKNOWN"
    if number < 40: return "0-39"
    if number < 50: return "40-49"
    if number < 60: return "50-59"
    if number < 70: return "60-69"
    if number < 80: return "70-79"
    return "80+"


def _score_bucket(value: Any) -> str:
    number = _number(value)
    if number is None: return "UNKNOWN"
    return f"{int(math.floor(number / 10) * 10)}-{int(math.floor(number / 10) * 10 + 9)}"


def _new_episode(row: Mapping[str, Any], timestamp: str) -> dict[str, Any]:
    status = str(row.get("signal") or row.get("signal_status") or "NO TRADE").upper()
    side = str(row.get("side") or row.get("direction") or "UNKNOWN").upper()
    episode = {
        "episode_id": "", "symbol": str(row.get("symbol") or "UNKNOWN"), "timeframe": str(row.get("timeframe") or "1h"),
        "side": side, "signal_timestamp": timestamp, "signal_status": status,
        "initial_signal_status": status, "highest_signal_status": status,
        "current_signal_status": status,
        "confidence": _number(row.get("confidence")), "score": _number(row.get("score")),
        "market_regime": str(row.get("market_regime") or "UNKNOWN"), "trend_context": row.get("trend_context") or row.get("trend_score"),
        "momentum_context": row.get("momentum_context") or row.get("momentum_score"),
        "structure_context": row.get("structure_context") or row.get("structure_score"),
        "entry": _number(row.get("entry")), "stop_loss": _number(row.get("stop_loss")), "take_profit": _number(row.get("take_profit")),
        "agent_version": row.get("agent_version"), "decision_id": row.get("decision_id") or row.get("signal_fingerprint"),
        "snapshot_id": row.get("snapshot_id"), "entry_price": _number(row.get("current_price") or row.get("entry")),
    }
    episode["episode_id"] = episode_id(episode)
    return episode


def _outcome(episode: Mapping[str, Any], price: Any, horizon: int, observed_at: str, *, target_at: str,
             horizon_delay_seconds: int, high: Any = None, low: Any = None) -> dict[str, Any]:
    entry, future = _number(episode.get("entry_price")), _number(price)
    side = str(episode.get("side") or "UNKNOWN").upper()
    outcome_id = f"{episode.get('episode_id')}:{horizon}H"
    base = {"outcome_id": outcome_id, "episode_id": episode.get("episode_id"), "symbol": episode.get("symbol"), "timeframe": episode.get("timeframe"), "horizon": f"{horizon}H", "source_timestamp": episode.get("signal_timestamp"), "observed_at": observed_at, "target_at": target_at, "horizon_delay_seconds": horizon_delay_seconds, "evaluated_at": observed_at, "future_price": future,
            "return_pct": None, "return_r": None, "mfe": None, "mae": None, "tp_hit": None, "sl_hit": None,
            "direction_correct": "UNKNOWN", "outcome_status": "PENDING", "label": "PENDING"}
    if entry is None or future is None or side not in {"LONG", "SHORT"}:
        base.update({"outcome_status": "INVALID", "label": "INVALID"})
        return base
    signed = (future - entry) / entry * 100 * (1 if side == "LONG" else -1)
    high_value, low_value = _number(high), _number(low)
    if high_value is None or low_value is None:
        high_value = low_value = future
    if side == "LONG":
        mfe, mae = (high_value - entry) / entry * 100, (low_value - entry) / entry * 100
        tp_hit = _number(episode.get("take_profit")) is not None and high_value >= _number(episode.get("take_profit"))
        sl_hit = _number(episode.get("stop_loss")) is not None and low_value <= _number(episode.get("stop_loss"))
    else:
        mfe, mae = (entry - low_value) / entry * 100, (entry - high_value) / entry * 100
        tp_hit = _number(episode.get("take_profit")) is not None and low_value <= _number(episode.get("take_profit"))
        sl_hit = _number(episode.get("stop_loss")) is not None and high_value >= _number(episode.get("stop_loss"))
    stop = _number(episode.get("stop_loss")); risk = abs(entry - stop) if stop is not None else None
    label = "WIN" if signed > 0 else "LOSS" if signed < 0 else "NEUTRAL"
    direction = "CORRECT" if signed > 0 else "WRONG" if signed < 0 else "FLAT"
    base.update({"return_pct": round(signed, 6), "return_r": round((future - entry) * (1 if side == "LONG" else -1) / risk, 6) if risk else None,
                 "mfe": round(mfe, 6), "mae": round(mae, 6), "tp_hit": tp_hit, "sl_hit": sl_hit,
                 "direction_correct": direction, "outcome_status": "EVALUATED", "label": label})
    return base


def _missed_horizon_outcome(episode: Mapping[str, Any], horizon: int, observed_at: str, *,
                            target_at: str, horizon_delay_seconds: int) -> dict[str, Any]:
    """Record unavailable evidence without treating a late price as an outcome."""
    return {
        "outcome_id": f"{episode.get('episode_id')}:{horizon}H",
        "episode_id": episode.get("episode_id"), "symbol": episode.get("symbol"),
        "timeframe": episode.get("timeframe"), "horizon": f"{horizon}H",
        "source_timestamp": episode.get("signal_timestamp"), "observed_at": observed_at,
        "target_at": target_at, "horizon_delay_seconds": horizon_delay_seconds,
        "evaluated_at": None, "future_price": None, "return_pct": None,
        "return_r": None, "mfe": None, "mae": None, "tp_hit": None,
        "sl_hit": None, "direction_correct": "UNKNOWN",
        "outcome_status": "MISSED_HORIZON", "label": "MISSED_HORIZON",
    }


def _metrics(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    complete = [row for row in rows if row.get("outcome_status") == "EVALUATED"]
    if len(complete) < MINIMUM_SAMPLE:
        return {"sample_count": len(complete), "status": "INSUFFICIENT_DATA", "winrate": None, "direction_accuracy": None, "expectancy": None, "mean_return": None, "median_return": None, "mean_mfe": None, "mean_mae": None, "mean_r": None, "net_r": None, "profit_factor": None}
    returns = [float(row["return_pct"]) for row in complete if _number(row.get("return_pct")) is not None]
    r_values = [float(row["return_r"]) for row in complete if _number(row.get("return_r")) is not None]
    wins, losses = [value for value in r_values if value > 0], [value for value in r_values if value < 0]
    ordered = sorted(returns); median = ordered[len(ordered) // 2] if ordered else None
    mean = lambda key: round(sum(float(row[key]) for row in complete if _number(row.get(key)) is not None) / len([row for row in complete if _number(row.get(key)) is not None]), 6) if any(_number(row.get(key)) is not None for row in complete) else None
    return {"sample_count": len(complete), "status": "READY", "winrate": round(100 * sum(row.get("label") == "WIN" for row in complete) / len(complete), 2), "direction_accuracy": round(100 * sum(row.get("direction_correct") == "CORRECT" for row in complete) / len(complete), 2), "expectancy": mean("return_r") if r_values else mean("return_pct"), "mean_return": mean("return_pct"), "median_return": round(median, 6) if median is not None else None, "mean_mfe": mean("mfe"), "mean_mae": mean("mae"), "mean_r": round(sum(r_values) / len(r_values), 6) if r_values else None, "net_r": round(sum(r_values), 6) if r_values else None, "profit_factor": round(sum(wins) / abs(sum(losses)), 6) if losses else None}


def build_oos_windows(outcomes: list[Mapping[str, Any]], *, train_size: int = 20, test_size: int = 5, step: int = 5) -> list[dict[str, Any]]:
    """Provide chronological, shared boundaries without reordering outcome data."""
    ordered = sorted((dict(row) for row in outcomes if _time(row.get("source_timestamp"))), key=lambda row: _time(row["source_timestamp"]))
    windows = []
    for start in range(0, max(0, len(ordered) - train_size - test_size + 1), max(1, step)):
        train, test = ordered[start:start + train_size], ordered[start + train_size:start + train_size + test_size]
        if not train or not test or _time(train[-1]["source_timestamp"]) >= _time(test[0]["source_timestamp"]):
            continue
        windows.append({"train": train, "test": test, "train_end": train[-1]["source_timestamp"], "test_start": test[0]["source_timestamp"]})
    return windows


def _grouped(rows: list[Mapping[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows: groups[str(row.get(field) or "UNKNOWN")].append(row)
    return {name: _metrics(values) for name, values in sorted(groups.items())}


def _calibration(episodes: Mapping[str, Mapping[str, Any]], outcomes: list[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for outcome in outcomes:
        if outcome.get("outcome_status") == "EVALUATED":
            groups[_confidence_bucket((episodes.get(str(outcome.get("episode_id"))) or {}).get("confidence"))].append(outcome)
    items = {}
    for bucket, values in sorted(groups.items()):
        rate = 100 * sum(row.get("label") == "WIN" for row in values) / len(values)
        midpoint = {"0-39": 19.5, "40-49": 44.5, "50-59": 54.5, "60-69": 64.5, "70-79": 74.5, "80+": 90}.get(bucket)
        status = "INSUFFICIENT_DATA" if len(values) < MINIMUM_SAMPLE else "OVERCONFIDENT" if midpoint is not None and rate + 5 < midpoint else "UNDERCONFIDENT" if midpoint is not None and rate - 5 > midpoint else "CALIBRATED"
        items[bucket] = {"sample_count": len(values), "actual_win_rate": round(rate, 2), "status": status}
    statuses = [item["status"] for item in items.values()]
    overall = "INSUFFICIENT_DATA" if not statuses or all(status == "INSUFFICIENT_DATA" for status in statuses) else next((status for status in statuses if status in {"OVERCONFIDENT", "UNDERCONFIDENT"}), "CALIBRATED")
    return {"status": overall, "buckets": items}


def process_snapshot(snapshot: Mapping[str, Any], *, base_dir: Path = BASE_DIR) -> dict[str, Any]:
    """Observe one already-published runtime snapshot and write only derived artifacts."""
    generated_at = normalize_runtime_timestamp(snapshot.get("generated_at"))
    signals = snapshot.get("signals") if isinstance(snapshot.get("signals"), list) else []
    if generated_at is None:
        return {"status": "INVALID", "episodes_total": 0, "episodes_evaluated": 0, "episodes_pending": 0, "accuracy": None, "expectancy": None, "calibration_status": "INSUFFICIENT_DATA", "generated_at": None}
    state = _read_json(base_dir / STATE, {"active": {}, "extrema": {}, "episodes": {}})
    last_generated_at = _time(state.get("last_processed_generated_at"))
    snapshot_id = str(snapshot.get("snapshot_id") or "")
    last_snapshot_id = str(state.get("last_processed_snapshot_id") or "")
    current_generated_at = _time(generated_at)
    if ((last_generated_at is not None and current_generated_at is not None and current_generated_at <= last_generated_at)
            or (snapshot_id and snapshot_id == last_snapshot_id)):
        LOGGER.warning("OUT_OF_ORDER_SNAPSHOT snapshot_id=%s generated_at=%s", snapshot_id, generated_at)
        return {
            "status": "OUT_OF_ORDER_SNAPSHOT", "evaluated": 0, "pending": None,
            "accuracy": None, "expectancy": None, "calibration_status": "INSUFFICIENT_DATA",
            "generated_at": generated_at,
        }
    active = state.get("active") if isinstance(state.get("active"), dict) else {}
    extrema = state.get("extrema") if isinstance(state.get("extrema"), dict) else {}
    episode_state = state.get("episodes") if isinstance(state.get("episodes"), dict) else {}
    existing_episodes = {
        str(row.get("episode_id")): {**row, **(episode_state.get(str(row.get("episode_id")), {}) if isinstance(episode_state.get(str(row.get("episode_id")), {}), Mapping) else {})}
        for row in _read_jsonl(base_dir / EPISODES)
    }
    created: list[dict[str, Any]] = []
    seen_keys = set()
    prices: dict[str, tuple[float, float, float]] = {}
    for raw in signals:
        if not isinstance(raw, Mapping): continue
        key = _key(raw); seen_keys.add(key)
        price = _number(raw.get("current_price") or raw.get("price"))
        if price is not None: prices[key] = (price, _number(raw.get("high")) or price, _number(raw.get("low")) or price)
        status = str(raw.get("signal") or raw.get("signal_status") or "NO TRADE").upper()
        side = str(raw.get("side") or raw.get("direction") or "UNKNOWN").upper()
        current_id = active.get(key)
        current = existing_episodes.get(str(current_id))
        if status in ACTIVE_STATUSES and side in {"LONG", "SHORT"}:
            if current is None or current.get("side") != side:
                episode = _new_episode({**raw, "timestamp": generated_at, "snapshot_id": snapshot.get("snapshot_id"), "agent_version": snapshot.get("agent_version")}, generated_at)
                created.append(episode); existing_episodes[episode["episode_id"]] = episode; active[key] = episode["episode_id"]
                episode_state[episode["episode_id"]] = {
                    "initial_signal_status": status, "highest_signal_status": status,
                    "current_signal_status": status, "lifecycle_status": "ACTIVE",
                }
            else:
                current_id = str(current.get("episode_id"))
                prior_highest = str(current.get("highest_signal_status") or current.get("signal_status") or "WATCH").upper()
                episode_state[current_id] = {
                    "initial_signal_status": current.get("initial_signal_status") or current.get("signal_status"),
                    "highest_signal_status": status if STATUS_PRIORITY.get(status, 0) > STATUS_PRIORITY.get(prior_highest, 0) else prior_highest,
                    "current_signal_status": status, "lifecycle_status": "ACTIVE",
                }
                current.update(episode_state[current_id])
            current_id = active.get(key)
            if current_id and price is not None:
                old = extrema.get(current_id, {}) if isinstance(extrema.get(current_id), Mapping) else {}
                extrema[current_id] = {"high": max(_number(old.get("high")) or price, prices[key][1]), "low": min(_number(old.get("low")) or price, prices[key][2])}
        else:
            if current_id:
                prior = episode_state.get(str(current_id), {})
                episode_state[str(current_id)] = {
                    **(prior if isinstance(prior, Mapping) else {}),
                    "current_signal_status": status, "lifecycle_status": "CLOSED",
                }
            active.pop(key, None)
    for key in list(active):
        if key not in seen_keys:
            current_id = active.get(key)
            if current_id:
                prior = episode_state.get(str(current_id), {})
                episode_state[str(current_id)] = {
                    **(prior if isinstance(prior, Mapping) else {}),
                    "current_signal_status": "DISAPPEARED", "lifecycle_status": "CLOSED",
                }
            active.pop(key, None)
    _append_unique(base_dir / EPISODES, created, "episode_id")
    outcomes = _read_jsonl(base_dir / OUTCOMES)
    known = {str(row.get("outcome_id")) for row in outcomes}
    new_outcomes = []
    now = _time(generated_at)
    for episode in existing_episodes.values():
        started = _time(episode.get("signal_timestamp")); price_data = prices.get(_key(episode))
        if started is None or now is None or price_data is None: continue
        for horizon in HORIZONS_HOURS:
            identity = f"{episode.get('episode_id')}:{horizon}H"
            target = started + timedelta(hours=horizon)
            if identity in known or now < target: continue
            delay = int((now - target).total_seconds())
            target_at = target.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if delay > _horizon_tolerance_seconds():
                new_outcomes.append(_missed_horizon_outcome(
                    episode, horizon, generated_at, target_at=target_at,
                    horizon_delay_seconds=delay,
                ))
                continue
            bounds = extrema.get(str(episode.get("episode_id")), {})
            new_outcomes.append(_outcome(
                episode, price_data[0], horizon, generated_at, target_at=target_at,
                horizon_delay_seconds=delay, high=bounds.get("high", price_data[1]),
                low=bounds.get("low", price_data[2]),
            ))
    _append_unique(base_dir / OUTCOMES, new_outcomes, "outcome_id")
    all_outcomes = outcomes + new_outcomes
    overall = _metrics(all_outcomes)
    calibration = _calibration(existing_episodes, all_outcomes)
    evaluated = [row for row in all_outcomes if row.get("outcome_status") == "EVALUATED"]
    report = {"schema_version": "signal-evaluation-v1", "generated_at": generated_at, "evaluation_status": overall["status"], "episodes_total": len(existing_episodes), "episodes_evaluated": len({row.get("episode_id") for row in evaluated}), "episodes_pending": max(0, len(existing_episodes) - len({row.get("episode_id") for row in evaluated})), "latest_evaluated_at": generated_at if evaluated else None, "label_distribution": dict(Counter(str(row.get("label")) for row in all_outcomes)), "minimum_sample_status": overall["status"], "metrics": overall, "by_symbol": _grouped(all_outcomes, "symbol"), "by_regime": _grouped([{**row, "market_regime": (existing_episodes.get(str(row.get("episode_id"))) or {}).get("market_regime")} for row in all_outcomes], "market_regime"), "by_direction": _grouped([{**row, "direction": (existing_episodes.get(str(row.get("episode_id"))) or {}).get("side")} for row in all_outcomes], "direction"), "by_confidence_bucket": _grouped([{**row, "confidence_bucket": _confidence_bucket((existing_episodes.get(str(row.get("episode_id"))) or {}).get("confidence"))} for row in all_outcomes], "confidence_bucket"), "by_score_bucket": _grouped([{**row, "score_bucket": _score_bucket((existing_episodes.get(str(row.get("episode_id"))) or {}).get("score"))} for row in all_outcomes], "score_bucket"), "by_timeframe": _grouped([{**row, "timeframe": (existing_episodes.get(str(row.get("episode_id"))) or {}).get("timeframe")} for row in all_outcomes], "timeframe"), "calibration": calibration, "oos_inputs": sorted([{**row, "episode_id": row.get("episode_id"), "source_timestamp": row.get("source_timestamp")} for row in evaluated], key=lambda row: str(row.get("source_timestamp")))}
    _atomic_json(base_dir / REPORT, report)
    _atomic_json(base_dir / STATE, {
        "active": active, "extrema": extrema, "episodes": episode_state,
        "last_processed_snapshot_id": snapshot_id,
        "last_processed_generated_at": generated_at,
    })
    return {"status": report["evaluation_status"], "evaluated": len(evaluated), "pending": report["episodes_pending"], "accuracy": overall["direction_accuracy"], "expectancy": overall["expectancy"], "calibration_status": calibration["status"], "generated_at": generated_at}
