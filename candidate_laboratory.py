"""Shadow-only DecisionEngine candidate laboratory.

The module deliberately has no execution, portfolio, notification, or active
setup dependencies. Candle resolution is conservative: when SL and TP are both
touched in one candle, SL is assumed to have occurred first.
"""

from __future__ import annotations

import csv
import json
import math
import os
import threading
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from feature_logger import classify_market_regime, classify_session
from strategies import registry

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "candidate_configs.json"
DECISIONS_FILE = BASE_DIR / "candidate_decisions.csv"
TRADES_FILE = BASE_DIR / "candidate_shadow_trades.csv"
LOCK = threading.RLock()

DECISION_FIELDS = [
    "timestamp", "cycle_id", "symbol", "timeframe", "candidate_id",
    "candidate_version", "direction", "decision", "status", "score",
    "weighted_score", "confidence", "quality", "edge", "primary_blocker",
    "trend_score", "structure_score", "momentum_score", "risk_score",
    "entry", "stop_loss", "take_profit", "rr", "market_regime", "session",
    "atr", "atr_percentile", "adx", "volume_ratio",
    "distance_to_ema200_pct", "source_snapshot_id", "shadow_only",
]
TRADE_FIELDS = [
    "shadow_trade_id", "candidate_id", "symbol", "direction", "opened_at",
    "entry", "stop_loss", "take_profit", "rr", "score", "confidence",
    "quality", "market_regime", "session", "status", "closed_at",
    "exit_price", "result", "pnl_r", "bars_held", "close_reason",
]


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _ensure_csv(path: Path, fields: list[str]) -> None:
    if path.exists() and path.stat().st_size:
        try:
            with path.open(newline="", encoding="utf-8") as stream:
                if next(csv.reader(stream), []) == fields:
                    return
        except (OSError, csv.Error):
            pass
        path.replace(path.with_suffix(path.suffix + ".corrupt"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        csv.DictWriter(stream, fieldnames=fields).writeheader()


def _read_rows(path: Path, fields: list[str]) -> list[dict[str, str]]:
    _ensure_csv(path, fields)
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            return [row for row in csv.DictReader(stream) if row and any(row.values())]
    except (OSError, csv.Error):
        return []


def _rewrite(path: Path, fields: list[str], rows: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


class CandidateLaboratory:
    def __init__(
        self, decision_fn: Callable[..., Any], *,
        config_path: str | Path = CONFIG_FILE,
        decisions_path: str | Path = DECISIONS_FILE,
        trades_path: str | Path = TRADES_FILE,
        track_trades: bool = True,
    ) -> None:
        self.decision_fn = decision_fn
        self.config_path = Path(config_path)
        self.decisions_path = Path(decisions_path)
        self.trades_path = Path(trades_path)
        self.track_trades = track_trades

    def _configs(self) -> dict[str, Any]:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return {key: value for key, value in data.items() if key != "session_utc"}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _normalized(weights: Mapping[str, Any]) -> dict[str, float]:
        values = {key: max(0.0, _num(weights.get(key))) for key in ("trend", "structure", "momentum", "risk")}
        total = sum(values.values())
        if total <= 0:
            raise ValueError("candidate weights must have a positive sum")
        return {key: value / total for key, value in values.items()}

    def run(
        self, *, snapshot: Mapping[str, Any], live_decision: Any,
        trend: Any, structure: Any, momentum: Any, risk: Any,
        live_weights: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        source = deepcopy(dict(snapshot))
        timestamp = str(source.get("timestamp") or datetime.now(timezone.utc).isoformat())
        results = []
        for candidate_id, config in self._configs().items():
            if not config.get("enabled", False):
                continue
            try:
                if config.get("shadow_only") is not True:
                    raise ValueError("candidate must explicitly be shadow_only")
                # An absent override is an exact baseline pass-through. Only
                # explicit candidate weights are normalized.
                weights = (
                    self._normalized(config["weights"])
                    if config.get("weights")
                    else deepcopy(dict(live_weights))
                )
                candidate_momentum = deepcopy(momentum)
                delta = _num(config.get("overrides", {}).get("momentum_threshold_delta"))
                if delta:
                    adjustment = -delta
                    candidate_momentum = replace(
                        candidate_momentum,
                        long=candidate_momentum.long + adjustment if candidate_momentum.long > 0 else candidate_momentum.long,
                        short=candidate_momentum.short + adjustment if candidate_momentum.short > 0 else candidate_momentum.short,
                    )
                decision = self.decision_fn(
                    deepcopy(trend), deepcopy(structure), candidate_momentum,
                    deepcopy(risk), weights,
                )
                row = self._decision_row(
                    source, timestamp, candidate_id, config, decision,
                    trend, structure, candidate_momentum, risk, weights,
                )
                strategy = registry.get(candidate_id)
                if strategy and strategy.shadow_only:
                    gate = strategy.evaluate({**source, **row})
                    if not gate.get("accepted", False):
                        row["decision"] = "WAIT"
                        row["primary_blocker"] = " | ".join(gate.get("reasons", []))
                # Retained in memory/open JSON and research.db. The legacy CSV
                # schema intentionally remains unchanged for compatibility.
                row["feature_snapshot"] = deepcopy(source)
                if self._append_decision(row) and self.track_trades:
                    self._open_shadow_if_needed(row)
                results.append(row)
            except Exception as exc:  # candidate isolation is a core safety property
                results.append({"candidate_id": candidate_id, "status": "ERROR", "error": str(exc), "shadow_only": True})
        assert snapshot == source, "Candidate Laboratory mutated its snapshot"
        return results

    def _decision_row(self, s, timestamp, candidate_id, config, decision, trend, structure, momentum, risk, weights):
        entry = _num(s.get("current_price", s.get("entry")))
        atr = _num(s.get("atr"))
        direction = decision.direction
        stop = entry - atr if direction == "LONG" else entry + atr
        take = entry + atr * 2 if direction == "LONG" else entry - atr * 2
        edge = abs(_num(getattr(decision, "long_total", 0)) - _num(getattr(decision, "short_total", 0)))
        regime = s.get("market_regime") or classify_market_regime(s)
        session = s.get("session") or classify_session(timestamp)
        return {
            "timestamp": timestamp, "cycle_id": s.get("cycle_id", ""),
            "symbol": s.get("symbol", ""), "timeframe": s.get("timeframe", "1h"),
            "candidate_id": candidate_id, "candidate_version": config.get("version", "1.0.0"),
            "direction": direction, "decision": decision.signal, "status": "EVALUATED",
            "score": decision.score, "weighted_score": decision.score,
            "confidence": decision.confidence, "quality": decision.quality, "edge": edge,
            "primary_blocker": s.get("primary_blocker", ""),
            "trend_score": max(_num(trend.long), _num(trend.short)) * weights["trend"],
            "structure_score": max(_num(structure.long), _num(structure.short)) * weights["structure"],
            "momentum_score": max(_num(momentum.long), _num(momentum.short)) * weights["momentum"],
            "risk_score": max(_num(risk.long), _num(risk.short)) * weights["risk"],
            "entry": entry, "stop_loss": stop, "take_profit": take,
            "rr": 2.0 if atr > 0 else "", "market_regime": regime, "session": session,
            "atr": s.get("atr", ""), "atr_percentile": s.get("atr_percentile", ""),
            "adx": s.get("adx", ""), "volume_ratio": s.get("volume_ratio", ""),
            "distance_to_ema200_pct": s.get("distance_to_ema200_pct", ""),
            "source_snapshot_id": s.get("snapshot_id", ""), "shadow_only": True,
        }

    def _append_decision(self, row: Mapping[str, Any]) -> bool:
        key = tuple(str(row.get(k, "")) for k in ("timestamp", "cycle_id", "symbol", "candidate_id"))
        with LOCK:
            rows = _read_rows(self.decisions_path, DECISION_FIELDS)
            existing = {
                tuple(item.get(k, "") for k in ("timestamp", "cycle_id", "symbol", "candidate_id"))
                for item in rows
            }
            if key in existing:
                return False
            with self.decisions_path.open("a", newline="", encoding="utf-8") as stream:
                csv.DictWriter(
                    stream, fieldnames=DECISION_FIELDS, extrasaction="ignore",
                ).writerow(row)
                stream.flush()
                os.fsync(stream.fileno())
            return True

    def _open_shadow_if_needed(self, decision: Mapping[str, Any]) -> None:
        if decision.get("decision") not in ("SETUP", "HIGH PRIORITY") or not _num(decision.get("rr")):
            return
        with LOCK:
            rows = _read_rows(self.trades_path, TRADE_FIELDS)
            if any(
                row.get("candidate_id") == decision.get("candidate_id")
                and row.get("symbol") == decision.get("symbol")
                and row.get("status") == "OPEN" for row in rows
            ):
                return
            trade = {
                "shadow_trade_id": uuid.uuid4().hex,
                "candidate_id": decision["candidate_id"], "symbol": decision["symbol"],
                "direction": decision["direction"], "opened_at": decision["timestamp"],
                "entry": decision["entry"], "stop_loss": decision["stop_loss"],
                "take_profit": decision["take_profit"], "rr": decision["rr"],
                "score": decision["score"], "confidence": decision["confidence"],
                "quality": decision["quality"], "market_regime": decision["market_regime"],
                "session": decision["session"], "status": "OPEN", "closed_at": "",
                "exit_price": "", "result": "", "pnl_r": "", "bars_held": 0,
                "close_reason": "",
            }
            with self.trades_path.open("a", newline="", encoding="utf-8") as stream:
                csv.DictWriter(stream, fieldnames=TRADE_FIELDS).writerow(trade)

    def update_shadow_trades(
        self, *, symbol: str, high: float, low: float,
        timestamp: str | None = None, max_bars: int = 100,
    ) -> list[dict[str, str]]:
        now = timestamp or datetime.now(timezone.utc).isoformat()
        with LOCK:
            rows = _read_rows(self.trades_path, TRADE_FIELDS)
            changed = []
            for row in rows:
                if row.get("status") != "OPEN" or row.get("symbol") != symbol:
                    continue
                row["bars_held"] = str(int(_num(row.get("bars_held"))) + 1)
                stop, take = _num(row["stop_loss"]), _num(row["take_profit"])
                is_long = row["direction"] == "LONG"
                hit_sl = low <= stop if is_long else high >= stop
                hit_tp = high >= take if is_long else low <= take
                if hit_sl:  # deliberately first: conservative same-candle policy
                    row.update(status="LOSS", closed_at=now, exit_price=str(stop), result="LOSS", pnl_r="-1", close_reason="SL")
                elif hit_tp:
                    row.update(status="WIN", closed_at=now, exit_price=str(take), result="WIN", pnl_r=str(_num(row["rr"])), close_reason="TP")
                elif int(row["bars_held"]) >= max_bars:
                    row.update(status="EXPIRED", closed_at=now, result="EXPIRED", pnl_r="0", close_reason="MAX_BARS")
                changed.append(row)
            _rewrite(self.trades_path, TRADE_FIELDS, rows)
            return changed
