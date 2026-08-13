"""Read-only projection of existing immutable AITradingAgent artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Mapping

from research_lab_v2.dashboard import ResearchDashboardV2
from telegram_ui.data import (
    available_timeframes,
    latest_rows,
    normalize_symbol,
    signal_payload_from_rows,
)
from trade_metrics_normalizer import aggregate_trade_metrics, is_closed_trade
from runtime_contract import (
    DEFAULT_STALE_AFTER_SECONDS, evaluate_freshness, find_non_finite_value,
    normalize_runtime_timestamp, read_runtime_snapshot,
)

from .runtime_ingest import validate_stored_bundle

from .cache import MTimeCSVCache


@dataclass(frozen=True)
class _SnapshotCacheEntry:
    signature: tuple[int, int] | None
    expires_at: float
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _RuntimeJsonCacheEntry:
    signature: tuple[int, int] | None
    expires_at: float
    payload: Mapping[str, Any]

def _number(value: Any) -> float | None:
    try:
        number = float(value) if value not in (None, "") else None
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


class ReadOnlyRepository:
    def __init__(self, base_dir: str | Path, *, cache_ttl_seconds: float = 5,
                 query_timeout_seconds: float = 2.0, max_source_rows: int = 10_000,
                 similar_min_sample: int = 20, ingest_dir: str | Path | None = None,
                 ingest_stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS) -> None:
        self.base_dir = Path(base_dir)
        self.query_timeout_seconds = max(0.1, float(query_timeout_seconds))
        self.cache_ttl_seconds = max(0.1, float(cache_ttl_seconds))
        self.max_source_rows = max(100, int(max_source_rows))
        self.similar_min_sample = max(1, int(similar_min_sample))
        self.ingest_dir = Path(ingest_dir) if ingest_dir is not None else self.base_dir / "runtime_ingest"
        self.ingest_stale_after_seconds = max(0, int(ingest_stale_after_seconds))
        self._cache = MTimeCSVCache(
            ttl_seconds=cache_ttl_seconds, max_rows=self.max_source_rows,
            timeout_seconds=self.query_timeout_seconds,
        )
        self._snapshot_cache: _SnapshotCacheEntry | None = None
        self._snapshot_lock = threading.RLock()
        self._runtime_json_cache: dict[str, _RuntimeJsonCacheEntry] = {}
        self._runtime_json_lock = threading.RLock()
        self._max_snapshot_bytes = max(
            1_048_576, min(33_554_432, self.max_source_rows * 4096),
        )

    def read_csv(self, path: str | Path) -> list[dict[str, str]]:
        return self._cache.read(path)

    def _runtime_json_at(self, path: Path, cache_key: str) -> dict[str, Any]:
        """Read one bounded, published runtime object without writing or importing runtime code."""
        try:
            stat = path.stat()
            signature: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        now = time.monotonic()
        with self._runtime_json_lock:
            cached = self._runtime_json_cache.get(cache_key)
            if cached and cached.signature == signature and now < cached.expires_at:
                return dict(cached.payload)

        payload: Mapping[str, Any] = {}
        if signature is not None and signature[1] <= self._max_snapshot_bytes:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    raw = handle.read(self._max_snapshot_bytes + 1)
                if len(raw.encode("utf-8")) <= self._max_snapshot_bytes:
                    decoded = json.loads(raw)
                    if isinstance(decoded, Mapping) and find_non_finite_value(decoded) is None:
                        payload = decoded
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                payload = {}
        frozen = dict(payload)
        with self._runtime_json_lock:
            self._runtime_json_cache[cache_key] = _RuntimeJsonCacheEntry(
                signature=signature, expires_at=now + self.cache_ttl_seconds, payload=frozen,
            )
        return dict(frozen)

    def _runtime_json(self, filename: str) -> dict[str, Any]:
        """Read a bounded runtime object under the configured data root."""
        return self._runtime_json_at(self.base_dir / filename, filename)

    def _ingested_bundle(self) -> dict[str, Any] | None:
        """Read a validated bundle only; this repository never writes ingest storage."""
        document = validate_stored_bundle(
            self._runtime_json_at(self.ingest_dir / "current.json", "__runtime_ingest_current__"),
        )
        if document is None:
            return None
        payload = document["payload"]
        snapshot = dict(payload["runtime_snapshot"])
        snapshot["freshness"] = evaluate_freshness(
            snapshot.get("generated_at"),
            source_updated_at=(snapshot.get("freshness") or {}).get("source_updated_at"),
            stale_after_seconds=self.ingest_stale_after_seconds,
        )
        return {"metadata": document["metadata"], "payload": {**payload, "runtime_snapshot": snapshot}}

    def _canonical_snapshot(self) -> dict[str, Any] | None:
        """Read only a validated v1 snapshot; future schemas intentionally fail closed."""
        stale_after = os.getenv("RUNTIME_SNAPSHOT_STALE_AFTER_SECONDS", str(DEFAULT_STALE_AFTER_SECONDS))
        try:
            threshold = int(stale_after)
        except (TypeError, ValueError):
            threshold = DEFAULT_STALE_AFTER_SECONDS
        return read_runtime_snapshot(
            self.base_dir / "runtime_snapshot.json", stale_after_seconds=max(0, threshold),
        )

    def _primary_snapshot(self) -> dict[str, Any] | None:
        ingested = self._ingested_bundle()
        if ingested is not None:
            return dict(ingested["payload"]["runtime_snapshot"])
        return self._canonical_snapshot()

    def _freshness(self, timestamp: Any) -> dict[str, Any]:
        """One consistent, honest freshness shape for frontend DTOs."""
        return evaluate_freshness(
            timestamp, stale_after_seconds=self.ingest_stale_after_seconds,
        )

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, Mapping) else {}

    def _integrity_projection(self, integrity: Mapping[str, Any]) -> dict[str, Any]:
        """A stable, read-only integrity DTO for production clients."""
        checks = self._mapping(integrity.get("checks"))
        sync = self._mapping(checks.get("OUTCOME_SYNC_GAP"))
        unresolved = self._mapping(checks.get("UNRESOLVED_ATTRIBUTION"))
        current = self._mapping(checks.get("CURRENT_PIPELINE_ATTRIBUTION"))
        features = self._mapping(checks.get("FEATURE_SNAPSHOT_COVERAGE"))
        stale_metrics = self._mapping(checks.get("STALE_METRICS"))
        stale_walk_forward = self._mapping(checks.get("STALE_WALK_FORWARD"))
        gates = self._mapping(integrity.get("gates"))
        return {
            "state": integrity.get("state", "UNKNOWN"),
            "integrity_state": integrity.get("state", "UNKNOWN"),
            "ledger_closed": sync.get("ledger_closed"),
            "canonical_outcomes": sync.get("canonical_outcomes"),
            "sync_gap": sync.get("sync_gap"),
            "historical_unresolved_joins": unresolved.get("historical_unresolved_joins"),
            "current_pipeline_unresolved_joins": unresolved.get("current_pipeline_unresolved_joins"),
            "feature_join_coverage": features.get("coverage_pct"),
            "new_outcomes_since_attribution_fix": current.get("new_outcomes_since_attribution_fix"),
            "new_outcomes_fully_joined": current.get("new_outcomes_fully_joined"),
            "new_outcomes_join_coverage_pct": current.get("new_outcomes_join_coverage_pct"),
            "ranking_allowed": gates.get("ranking_allowed"),
            "walk_forward_allowed": gates.get("walk_forward_allowed"),
            "promotion_allowed": gates.get("promotion_allowed"),
            "metrics_fresh": self._freshness(stale_metrics.get("metrics_calculated_at")),
            "walk_forward_fresh": self._freshness(stale_walk_forward.get("walk_forward_calculated_at")),
            "checked_at": integrity.get("checked_at"),
        }

    def _ingested_research(self) -> dict[str, Any] | None:
        ingested = self._ingested_bundle()
        payload = self._mapping((ingested or {}).get("payload"))
        summary = self._mapping(payload.get("research_summary"))
        integrity = self._mapping(payload.get("research_integrity"))
        if not summary and not integrity:
            return None
        strategy_modes = self._mapping(summary.get("strategy_modes"))
        evaluated = {str(item) for item in summary.get("strategies_enabled", []) if isinstance(item, str)}
        strategies = [
            {
                "strategy_id": strategy_id,
                "runtime_enabled": strategy_id in evaluated,
                "runtime_mode": mode,
                "registry_enabled": None,
                "evidence_state": "UNKNOWN",
                "closed_evidence": None,
                "profit_factor": None,
                "winrate": None,
                "net_r": None,
                "strategy_version": None,
                "walk_forward_status": "NOT_PUBLISHED",
                "confidence": None,
            }
            for strategy_id, mode in sorted(strategy_modes.items())
        ]
        runtime_stamp = summary.get("updated_at") or summary.get("last_processed_at") or summary.get("last_processed_cycle")
        checks = self._mapping(integrity.get("checks"))
        sync = self._mapping(checks.get("OUTCOME_SYNC_GAP"))
        unresolved = self._mapping(checks.get("UNRESOLVED_ATTRIBUTION"))
        current = self._mapping(checks.get("CURRENT_PIPELINE_ATTRIBUTION"))
        features = self._mapping(checks.get("FEATURE_SNAPSHOT_COVERAGE"))
        gates = self._mapping(integrity.get("gates"))
        return {
            "top_strategies": [], "top_features": [], "worst_features": [],
            "research_progress": {}, "best_candidate": None,
            "promotion_probability": None, "strategies": strategies,
            "runtime_status": summary, "shadow_ledger": None,
            "feature_analysis": {
                "status": "INSUFFICIENT_DATA" if not features else (
                    "READY" if int(features.get("valid_feature_snapshot") or 0) >= 20 else "INSUFFICIENT_DATA"
                ),
                "closed_outcomes": features.get("closed_outcomes"),
                "joined_outcomes": features.get("valid_feature_snapshot"),
                "join_coverage_percent": features.get("coverage_pct"),
                "reason": "Feature attribution uses fully joined canonical outcomes only.",
            },
            "research_data_integrity": integrity or None,
            "integrity_projection": self._integrity_projection(integrity) if integrity else {},
            "research_health": {
                "state": integrity.get("state", "UNKNOWN"), "gates": gates,
                "outcome_sync": {
                    "ledger_closed_total": sync.get("ledger_closed"),
                    "db_closed_total": sync.get("canonical_outcomes"),
                    "outcome_sync_gap": sync.get("sync_gap"),
                    "historical_unresolved_joins": unresolved.get("historical_unresolved_joins"),
                    "current_pipeline_unresolved_joins": unresolved.get("current_pipeline_unresolved_joins"),
                },
                "current_pipeline_attribution": current,
            },
            "source_mode": "runtime_ingest_v1",
            "freshness": self._freshness(runtime_stamp or integrity.get("checked_at")),
        }

    @staticmethod
    def _published_value(*values: Any) -> Any:
        for value in values:
            if isinstance(value, float) and not math.isfinite(value):
                continue
            if value not in (None, "", "N/A", "NEVER"):
                return value
        return None

    @classmethod
    def _published_value_with_source(cls, primary: tuple[Any, ...], fallback: tuple[Any, ...], *, primary_source: str) -> tuple[Any, str]:
        """Resolve a read-only value while retaining which published layer supplied it."""
        value = cls._published_value(*primary)
        if value is not None:
            return value, primary_source
        value = cls._published_value(*fallback)
        return value, "legacy_fallback" if value is not None else "unavailable"

    @staticmethod
    def _adapt_signal_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
        adapted: list[dict[str, Any]] = []
        for source in rows:
            row: dict[str, Any] = dict(source)
            timeframe = str(row.get("timeframe") or "1h").lower()
            if timeframe == "multi":
                row["source_timeframe"] = timeframe
                row["timeframe"] = "1h"
            adapted.append(row)
        return adapted

    def _snapshot_rows(self) -> list[dict[str, Any]]:
        path = self.base_dir / "decision_snapshot.json"
        try:
            stat = path.stat()
            signature: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        now = time.monotonic()
        with self._snapshot_lock:
            cached = self._snapshot_cache
            if cached and cached.signature == signature and now < cached.expires_at:
                return [dict(row) for row in cached.rows]

        rows: list[dict[str, Any]] = []
        deadline = now + self.query_timeout_seconds
        if signature is not None and signature[1] <= self._max_snapshot_bytes:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    raw = handle.read(self._max_snapshot_bytes + 1)
                if len(raw.encode("utf-8")) <= self._max_snapshot_bytes and time.monotonic() <= deadline:
                    payload = json.loads(raw)
                    if isinstance(payload, Mapping):
                        from .intelligence import decision_snapshot_rows
                        rows = decision_snapshot_rows(payload, max_rows=self.max_source_rows)
            except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                rows = []
        frozen = tuple(dict(row) for row in rows)
        with self._snapshot_lock:
            self._snapshot_cache = _SnapshotCacheEntry(
                signature=signature,
                expires_at=now + self.cache_ttl_seconds,
                rows=frozen,
            )
        return [dict(row) for row in frozen]

    def _legacy_decision_rows(self) -> list[dict[str, Any]]:
        snapshots = self._snapshot_rows()
        if snapshots:
            return snapshots
        for filename in ("signals_v3.csv", "signals.csv"):
            rows = self.read_csv(self.base_dir / filename)
            if rows:
                return self._adapt_signal_rows(rows)
        return self.read_csv(self.base_dir / "decision_debug.csv")

    def _decision_source(self) -> tuple[list[dict[str, Any]], str, str, str | None]:
        """Resolve fresh canonical data first, without presenting stale data as current."""
        ingested = self._ingested_bundle()
        if ingested is not None:
            snapshot = ingested["payload"]["runtime_snapshot"]
            freshness = str((snapshot.get("freshness") or {}).get("status") or "UNKNOWN").upper()
            rows = snapshot.get("signals")
            ingest_rows = [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
            return ingest_rows, "runtime_ingest_v1" if freshness == "FRESH" else "runtime_ingest_stale", freshness, None
        canonical = self._canonical_snapshot()
        freshness = str(((canonical or {}).get("freshness") or {}).get("status") or "UNKNOWN").upper()
        rows = (canonical or {}).get("signals")
        canonical_rows = [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
        if canonical_rows and freshness == "FRESH":
            return canonical_rows, "canonical_v1", freshness, None

        legacy_rows = self._legacy_decision_rows()
        if canonical_rows and freshness == "STALE":
            if legacy_rows:
                return legacy_rows, "legacy", freshness, "canonical_stale_legacy_available"
            return canonical_rows, "canonical_stale", freshness, "legacy_unavailable"
        if legacy_rows:
            reason = "canonical_missing_or_invalid" if canonical is None else "canonical_not_fresh"
            return legacy_rows, "legacy", freshness, reason
        if canonical_rows:
            return canonical_rows, "canonical_stale", freshness, "legacy_unavailable"
        return [], "legacy", freshness, "no_valid_signal_source"

    def decision_rows(self) -> list[dict[str, Any]]:
        return self._decision_source()[0]

    def trade_rows(self) -> list[dict[str, str]]:
        return self.read_csv(self.base_dir / "trades.csv")

    def diagnostics(self) -> dict[str, Any]:
        """Read pre-generated diagnostics only; this API never triggers analysis or writes."""
        names = ("signal_episode_report.json", "blocker_statistics.json", "trade_quality_report.json", "symbol_statistics.json", "direction_bias.json", "feature_importance.json", "strategy_recommendations.json")
        return {name.removesuffix(".json"): self._runtime_json(name) for name in names}

    def impulse_radar(self) -> list[dict[str, Any]]:
        path = self.base_dir / "impulse_probability.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        return [dict(row) for row in payload[:self.max_source_rows] if isinstance(row, Mapping)] if isinstance(payload, list) else []
    def impulse_report(self, filename: str) -> Any:
        path=self.base_dir/filename
        try: return json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError,TypeError): return {} if filename.endswith(".json") else []

    def impulse_learning(self) -> dict[str, Any]:
        payload = self.impulse_report("impulse_learning_report.json")
        return payload if isinstance(payload, dict) else {}

    def evaluation(self) -> dict[str, Any]:
        """Read a generated observer report only; this method never evaluates signals."""
        ingested = self._ingested_bundle()
        report = ((ingested or {}).get("payload") or {}).get("signal_evaluation_report")
        if isinstance(report, Mapping):
            return dict(report)
        payload = self.impulse_report("signal_evaluation_report.json")
        return payload if isinstance(payload, dict) else {
            "evaluation_status": "INSUFFICIENT_DATA", "episodes_total": 0,
            "episodes_evaluated": 0, "episodes_pending": 0,
        }

    def scenarios(self) -> list[dict[str, Any]]:
        ingested = self._ingested_bundle()
        report = ((ingested or {}).get("payload") or {}).get("scenario_report")
        if isinstance(report, list):
            return [dict(row) for row in report if isinstance(row, Mapping)]
        payload = self.impulse_report("scenario_report.json")
        return [dict(row) for row in payload if isinstance(row, Mapping)] if isinstance(payload, list) else []

    def scenario(self, symbol: str) -> dict[str, Any]:
        normalized = symbol.replace("/", "").upper()
        for row in self.scenarios():
            if str(row.get("symbol", "")).replace("/", "").upper() == normalized: return row
        return {"symbol": symbol, "status": "EMPTY", "data_quality": "INSUFFICIENT_DATA"}

    def updated_at(self) -> str:
        canonical = self._primary_snapshot()
        _, source_mode, _, _ = self._decision_source()
        if source_mode in {"canonical_v1", "canonical_stale"} and canonical and isinstance(canonical.get("generated_at"), str):
            return canonical["generated_at"]
        paths = [
            self.base_dir / "signals.csv", self.base_dir / "decision_snapshot.json",
            self.base_dir / "decision_debug.csv", self.base_dir / "signals_v3.csv",
            self.base_dir / "trades.csv", self.base_dir / "research.db",
        ]
        mtimes = [path.stat().st_mtime for path in paths if path.exists()]
        stamp = max(mtimes) if mtimes else datetime.now(timezone.utc).timestamp()
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat()

    def watchlist(self) -> list[dict[str, Any]]:
        rows, source_mode, source_freshness, _ = self._decision_source()
        latest = latest_rows(rows)
        items = []
        for (symbol, timeframe), row in sorted(latest.items()):
            items.append({
                "symbol": symbol,
                "status": str(row.get("signal") or row.get("decision") or "NO TRADE").upper(),
                "side": str(row.get("direction") or row.get("side") or "NEUTRAL").upper(),
                "confidence": _number(row.get("confidence")),
                "quality": str(row.get("quality") or "N/A"),
                "score": _number(row.get("score") or row.get("weighted_score")),
                "timeframe": timeframe,
                "updated_at": str(row.get("timestamp") or ""),
                "source": source_mode,
                "freshness": self._freshness(row.get("timestamp") or None),
                "source_freshness": source_freshness,
            })
        return items

    def _matching_trade(self, row: Mapping[str, Any], symbol: str, timeframe: str) -> dict[str, Any] | None:
        matching = [item for item in self.trade_rows() if (
            normalize_symbol(item.get("symbol")) == symbol
            and str(item.get("timeframe") or "1h").lower() == timeframe
        )]
        fingerprint, cycle_id = str(row.get("signal_fingerprint") or ""), str(row.get("cycle_id") or "")
        exact = [item for item in matching if (
            (fingerprint and item.get("signal_fingerprint") == fingerprint)
            or (cycle_id and item.get("cycle_id") == cycle_id)
        )]
        opened = [item for item in matching if str(item.get("status", "")).upper() == "OPEN"]
        return (opened or exact)[-1] if (opened or exact) else None

    def _candles(self, symbol: str, timeframe: str) -> tuple[dict[str, Any], ...]:
        path = self.base_dir / "ohlcv_cache" / f"{symbol.replace('/', '_')}_{timeframe}.csv"
        candles = []
        for row in self.read_csv(path)[-500:]:
            values = {key: _number(row.get(key)) for key in ("open", "high", "low", "close", "volume")}
            if any(values[key] is None for key in ("open", "high", "low", "close")):
                continue
            candles.append({"time": row.get("timestamp", ""), **values})
        return tuple(candles)

    def signal(self, symbol_value: str, timeframe: str | None = None) -> dict[str, Any] | None:
        symbol = normalize_symbol(symbol_value)
        if not re.fullmatch(r"[A-Z0-9]{2,20}/USDT", symbol):
            return None
        rows = self.decision_rows()
        timeframes = available_timeframes(rows, symbol)
        selected = (timeframe or ("1h" if "1h" in timeframes else timeframes[0] if timeframes else "1h")).lower()
        if selected not in {"15m", "1h", "4h", "1d"}:
            return None
        row = latest_rows(rows).get((symbol, selected))
        if row is None:
            return None
        payload = signal_payload_from_rows(
            row, trade_row=self._matching_trade(row, symbol, selected), timeframe=selected,
        )
        payload_data = asdict(payload)
        targets = {
            "tp1": payload.take_profit,
            "tp2": _number(row.get("take_profit_2") or row.get("tp2")),
            "tp3": _number(row.get("take_profit_3") or row.get("tp3")),
        }
        return {
            "symbol": symbol, "timeframe": selected,
            "available_timeframes": timeframes, "payload": payload_data,
            "targets": targets, "candles": self._candles(symbol, selected),
        }

    def open_trades(self) -> list[dict[str, str]]:
        return [row for row in self.trade_rows() if str(row.get("status", "")).upper() == "OPEN"]

    def trade_history(self) -> list[dict[str, str]]:
        return [row for row in self.trade_rows() if str(row.get("status", "")).upper() != "OPEN"]

    def stats(self) -> dict[str, Any]:
        ingested = self._ingested_bundle()
        if ingested is not None:
            # Railway has no authoritative local trades.csv. Only metrics explicitly
            # published in the canonical portfolio may be shown there.
            payload = self._mapping((ingested or {}).get("payload"))
            snapshot = self._mapping(payload.get("runtime_snapshot"))
            portfolio = self._mapping(snapshot.get("portfolio"))
            return {
                key: value for key, value in portfolio.items()
                if key in {"closed_trades", "winrate", "profit_factor", "net_r", "max_drawdown", "average_r", "average_hold_time"}
                and isinstance(value, (int, float)) and not isinstance(value, bool)
            }
        closed = [row for row in self.trade_rows() if is_closed_trade(row)]
        return aggregate_trade_metrics(closed)

    def research(self) -> dict[str, Any]:
        ingested = self._ingested_research()
        if ingested is not None:
            return ingested
        return ResearchDashboardV2(self.base_dir / "research.db").build_report()

    def system(self) -> dict[str, Any]:
        """Safe projection of already-published runtime status; no process inspection."""
        canonical = self._primary_snapshot()
        _, signal_source_mode, canonical_freshness, fallback_reason = self._decision_source()
        dashboard_state = self._runtime_json("dashboard_state.json")
        live_monitor = self._runtime_json("live_monitor_state.json")
        agent_stats = self._runtime_json("agent_v3_stats.json")
        ingested = self._ingested_bundle()
        ingested_payload = self._mapping((ingested or {}).get("payload"))
        system_summary = self._mapping(ingested_payload.get("system_summary"))
        ingested_research_status = ingested_payload.get("research_summary")
        research_status = (
            ingested_research_status if isinstance(ingested_research_status, Mapping)
            else self._runtime_json("research_lab_v2_status.json")
        )
        system_status = dashboard_state.get("system")
        trading = dashboard_state.get("trading")
        dashboard_live = dashboard_state.get("live_monitor")
        dashboard_research = dashboard_state.get("strategy_lab")
        telegram = dashboard_state.get("telegram")
        news = dashboard_state.get("news")
        safe_system = system_status if isinstance(system_status, Mapping) else {}
        safe_trading = trading if isinstance(trading, Mapping) else {}
        safe_dashboard_live = dashboard_live if isinstance(dashboard_live, Mapping) else {}
        safe_dashboard_research = dashboard_research if isinstance(dashboard_research, Mapping) else {}
        safe_telegram = telegram if isinstance(telegram, Mapping) else {}
        safe_news = news if isinstance(news, Mapping) else {}
        snapshot_freshness = str((canonical or {}).get("freshness", {}).get("status") or canonical_freshness).upper()
        has_agent_runtime = bool(canonical) or any((dashboard_state, live_monitor, agent_stats))
        agent_status = "ONLINE" if has_agent_runtime and (canonical is None or snapshot_freshness == "FRESH") else (
            "STALE" if has_agent_runtime and snapshot_freshness == "STALE" else None
        )
        integrity = self._mapping(ingested_payload.get("research_integrity"))
        if ingested is not None:
            primary_source = "runtime_ingest_v1" if snapshot_freshness == "FRESH" else "runtime_ingest_stale"
        elif canonical is not None:
            primary_source = "local_canonical" if snapshot_freshness == "FRESH" else "canonical_stale"
        else:
            primary_source = "legacy_fallback"

        server, server_source = self._published_value_with_source(
            # The derived agent status comes from the selected canonical
            # snapshot, so it is not a legacy fallback when an ingest/local
            # contract exists.
            (system_summary.get("server"), agent_status if canonical is not None else None),
            (safe_system.get("status"), live_monitor.get("status")),
            primary_source=primary_source,
        )
        telegram, telegram_source = self._published_value_with_source(
            (system_summary.get("telegram"),), (safe_telegram.get("status"),), primary_source=primary_source,
        )
        research, research_source = self._published_value_with_source(
            (
                ingested_research_status.get("status") if isinstance(ingested_research_status, Mapping) else None,
                "ONLINE" if isinstance(ingested_research_status, Mapping) and ingested_research_status.get("enabled") is True else None,
                "OFF" if isinstance(ingested_research_status, Mapping) and ingested_research_status.get("enabled") is False else None,
            ),
            (
                research_status.get("status") if not isinstance(ingested_research_status, Mapping) else None,
                "ONLINE" if not isinstance(ingested_research_status, Mapping) and research_status.get("enabled") is True else None,
                "OFF" if not isinstance(ingested_research_status, Mapping) and research_status.get("enabled") is False else None,
                safe_dashboard_research.get("status"),
            ), primary_source=primary_source,
        )
        news, news_source = self._published_value_with_source(
            (system_summary.get("news"),), (safe_news.get("status"),), primary_source=primary_source,
        )
        cycle, cycle_source = self._published_value_with_source(
            ((canonical or {}).get("cycle_id"), system_summary.get("cycle")),
            (safe_trading.get("last_cycle"), safe_trading.get("cycle"), live_monitor.get("cycle"),
             live_monitor.get("current_cycle"), live_monitor.get("last_cycle"), agent_stats.get("runs")),
            primary_source=primary_source,
        )
        interval_seconds, interval_source = self._published_value_with_source(
            (system_summary.get("interval_seconds"),),
            (live_monitor.get("interval"), safe_dashboard_live.get("interval"), agent_stats.get("interval_seconds")),
            primary_source=primary_source,
        )
        next_cycle_seconds, next_cycle_source = self._published_value_with_source(
            (system_summary.get("next_cycle_seconds"),),
            (safe_trading.get("next_cycle_seconds"), live_monitor.get("next_cycle_seconds"), agent_stats.get("next_cycle_seconds")),
            primary_source=primary_source,
        )
        last_cycle_timestamp, timestamp_source = self._published_value_with_source(
            ((canonical or {}).get("generated_at"), system_summary.get("generated_at")),
            (safe_trading.get("last_cycle"), live_monitor.get("generated_at"), dashboard_state.get("generated_at")),
            primary_source=primary_source,
        )
        uptime_seconds, uptime_source = self._published_value_with_source(
            (system_summary.get("uptime_seconds"),),
            (safe_system.get("uptime_seconds"), safe_trading.get("uptime_seconds"), live_monitor.get("uptime_seconds"),
             agent_stats.get("uptime_seconds")),
            primary_source=primary_source,
        )
        provenance = {
            "server": server_source, "telegram": telegram_source, "research": research_source,
            "news": news_source, "cycle": cycle_source, "interval_seconds": interval_source,
            "next_cycle_seconds": next_cycle_source, "last_cycle_timestamp": timestamp_source,
            "uptime_seconds": uptime_source,
        }
        signal_source = {
            "runtime_ingest_v1": "runtime_ingest_v1",
            "runtime_ingest_stale": "runtime_ingest_stale",
            "canonical_v1": "local_canonical",
            "canonical_stale": "canonical_stale",
            "legacy": "legacy_fallback",
        }.get(signal_source_mode, signal_source_mode)
        provenance["signals"] = signal_source
        fallback_blocks = sorted(name for name, source in provenance.items() if source == "legacy_fallback")
        active_sources = {source for source in provenance.values() if source != "unavailable"}
        response_source_mode = "mixed" if len(active_sources) > 1 else (next(iter(active_sources), primary_source))
        return {
            "server": server,
            "agent": agent_status,
            "telegram": telegram,
            "research": research or "UNKNOWN",
            "research_integrity_state": integrity.get("state") or "UNKNOWN",
            "news": news,
            "cycle": cycle,
            "interval_seconds": interval_seconds,
            "next_cycle_seconds": next_cycle_seconds,
            "last_cycle_timestamp": last_cycle_timestamp,
            "uptime_seconds": uptime_seconds,
            "read_only": True,
            "source_mode": response_source_mode,
            "canonical_freshness": canonical_freshness,
            "fallback_reason": fallback_reason,
            "source_provenance": {"fields": provenance, "fallback_fields": fallback_blocks},
            "runtime_contract": {
                "schema_version": (canonical or {}).get("schema_version"),
                "generated_at": (canonical or {}).get("generated_at"),
                "age_seconds": ((canonical or {}).get("freshness") or {}).get("age_seconds"),
                "freshness": canonical_freshness,
                "data_quality": ((canonical or {}).get("data_quality") or {}).get("status", "INSUFFICIENT"),
            },
            "runtime_ingest": dict((ingested or {}).get("metadata") or {}),
            "freshness": {
                "agent_snapshot": self._freshness((canonical or {}).get("generated_at")),
                "market_data": self._freshness(self._mapping((canonical or {}).get("freshness")).get("source_updated_at")),
                "trade_stats": self._freshness(self._mapping((canonical or {}).get("portfolio")).get("updated_at")),
                "research_runtime": self._freshness(research_status.get("updated_at") or research_status.get("last_processed_at")),
                "research_integrity": self._freshness(integrity.get("checked_at")),
                "news": self._freshness(system_summary.get("news_updated_at")),
                "candidate_ranking": self._freshness(None),
                "walk_forward": self._freshness(None),
            },
        }

    def activity(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return only timestamped, saved signal rows; never fabricate history."""
        events: list[dict[str, Any]] = []
        for row in self.decision_rows()[-max(1, min(limit, 100)):]:
            timestamp = row.get("timestamp") or row.get("updated_at")
            if not timestamp:
                continue
            symbol = row.get("symbol")
            status = row.get("signal") or row.get("decision") or row.get("status")
            events.append({
                "timestamp": str(timestamp), "type": "signal_snapshot",
                "symbol": str(symbol) if symbol else None,
                "timeframe": str(row.get("timeframe")) if row.get("timeframe") else None,
                "status": str(status) if status else None,
            })
        for event in events:
            event["freshness"] = self._freshness(event["timestamp"])
        events.sort(key=lambda event: normalize_runtime_timestamp(event["timestamp"]) or "", reverse=True)
        return events[:max(1, min(limit, 100))]

    def shadow(self) -> dict[str, Any]:
        """Expose Research Lab's existing separate shadow ledger without aggregation."""
        report = self.research()
        ledger = report.get("shadow_ledger")
        safe_ledger = ledger if isinstance(ledger, Mapping) else {}
        runtime = report.get("runtime_status")
        safe_runtime = runtime if isinstance(runtime, Mapping) else {}
        ledger_available = safe_ledger != {}
        return {
            "active": safe_ledger.get("open", []) if ledger_available else None,
            "closed": safe_ledger.get("closed", []) if ledger_available else None,
            "strategies": safe_runtime.get("strategy_modes", []),
            "symbols": None,
            "updated": safe_runtime.get("updated_at"),
            "availability": "PUBLISHED" if ledger_available else "NOT_PUBLISHED",
        }

    def research_live(self) -> dict[str, Any]:
        """Read-only subset of the existing ResearchDashboardV2 report."""
        report = self.research()
        return {
            "runtime_status": report.get("runtime_status"),
            "best_candidate": report.get("best_candidate") or None,
            "promotion_probability": report.get("promotion_probability"),
            "ranking": report.get("top_strategies", []),
            "recommendation": report.get("recommendation"),
            "top_features": report.get("top_features", []),
            "worst_features": report.get("worst_features", []),
            "strategies": report.get("strategies", []),
            "integrity": report.get("integrity_projection") or self._integrity_projection(
                self._mapping(report.get("research_data_integrity")),
            ),
            "research_health": report.get("research_health"),
            "freshness": report.get("freshness"),
            "source_mode": report.get("source_mode", "local_read_only"),
        }

    def health_checks(self) -> dict[str, bool]:
        """Filesystem availability only; paths and payloads are deliberately omitted."""
        def readable(filename: str) -> bool:
            path = self.base_dir / filename
            return path.is_file() and os.access(path, os.R_OK)

        return {
            "repository": self.base_dir.is_dir() and os.access(self.base_dir, os.R_OK),
            "signals": any(readable(name) for name in (
                "decision_snapshot.json", "signals_v3.csv", "signals.csv", "decision_debug.csv",
            )),
            "watchlist": any(readable(name) for name in (
                "decision_snapshot.json", "signals_v3.csv", "signals.csv", "decision_debug.csv",
            )),
            "research": readable("research.db"),
            "snapshot": readable("decision_snapshot.json"),
            "runtime_contract": readable("runtime_snapshot.json"),
            "runtime_ingest": (self.ingest_dir / "current.json").is_file(),
        }

    def dashboard(self) -> dict[str, Any]:
        ingested = self._ingested_bundle()
        portfolio = self._mapping(((ingested or {}).get("payload") or {}).get("runtime_snapshot", {}).get("portfolio"))
        metrics = self.stats() if not ingested else portfolio
        research = self.research()
        runtime = self._mapping(research.get("runtime_status"))
        snapshot = self._primary_snapshot() or {}
        metrics_available = bool(portfolio) if ingested else True
        return {
            "status": "ONLINE" if str((snapshot.get("freshness") or {}).get("status")) == "FRESH" else "STALE" if snapshot else "UNKNOWN",
            "updated_at": snapshot.get("generated_at") or self.updated_at(),
            "open_trades": (portfolio.get("open_trades") if ingested else len(self.open_trades())),
            "winrate": _number(metrics.get("winrate")),
            "profit_factor": _number(metrics.get("profit_factor")),
            "research_status": (research.get("research_data_integrity") or {}).get("state") or ("ON" if runtime.get("enabled") else "UNKNOWN"),
            "metrics_source": "runtime_snapshot.portfolio" if ingested else "trades.csv",
            "metrics_available": metrics_available,
            "freshness": self._freshness(snapshot.get("generated_at")),
        }

    def signal_intelligence(self, symbol: str, timeframe: str):
        from .intelligence import SignalIntelligenceService
        return SignalIntelligenceService(self).intelligence(symbol, timeframe)

    def signal_history(self, symbol: str, timeframe: str, *, page: int = 1, page_size: int = 50):
        from .intelligence import SignalIntelligenceService
        return SignalIntelligenceService(self).history(symbol, timeframe, page=page, page_size=page_size)

    def signal_changes(self, symbol: str, timeframe: str):
        from .intelligence import SignalIntelligenceService
        return SignalIntelligenceService(self).changes(symbol, timeframe)

    def signal_requirements(self, symbol: str, timeframe: str):
        from .intelligence import SignalIntelligenceService
        return SignalIntelligenceService(self).requirements(symbol, timeframe)

    def similar_setups(self, symbol: str, timeframe: str, *, source: str = "LIVE",
                       page: int = 1, page_size: int = 20):
        from .intelligence import SignalIntelligenceService
        return SignalIntelligenceService(self).similar(
            symbol, timeframe, source=source, page=page, page_size=page_size,
        )
