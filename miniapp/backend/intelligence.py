"""Evidence-only, read-only signal intelligence projections."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Mapping, Protocol

from miniapp.shared.models import (
    EvidenceExplanation,
    PaginatedHistoryResponse,
    SignalChangesResponse,
    SignalHistoryPoint,
    SignalIntelligencePayload,
    SignalRequirement,
    SignalRequirementsResponse,
    SimilarSetup,
    SimilarSetupsResponse,
)
from telegram_ui.data import latest_rows, normalize_symbol

SIGNAL_INTELLIGENCE_READ_ONLY = True
ALLOWED_TIMEFRAMES = {"15m", "1h", "4h", "1d"}
ALLOWED_TRADE_SOURCES = {"LIVE", "LEGACY_SHADOW", "RESEARCH_LAB"}
_COMPONENTS = ("trend", "momentum", "structure", "risk")
_CHANGE_METRICS = (
    "confidence", "score", "trend_score", "momentum_score",
    "structure_score", "risk_score", "rsi", "adx", "atr", "volume_ratio",
)


def decision_snapshot_rows(
    payload: Mapping[str, Any],
    *,
    max_rows: int = 10_000,
) -> list[dict[str, Any]]:
    """Flatten persisted decision snapshots into existing read-only row contracts."""
    candidates: list[Mapping[str, Any]] = []
    history = payload.get("history")
    if isinstance(history, list):
        candidates.extend(item for item in history if isinstance(item, Mapping))
    latest = payload.get("latest")
    if isinstance(latest, Mapping) and (not candidates or dict(latest) != dict(candidates[-1])):
        candidates.append(latest)
    elif not candidates and payload.get("symbol"):
        candidates.append(payload)

    rows: list[dict[str, Any]] = []
    for source in candidates[-max(1, int(max_rows)):]:
        row = dict(source)
        if row.get("score") in (None, ""):
            row["score"] = _first(row, "final_score", "raw_score")
        if row.get("summary") in (None, ""):
            row["summary"] = row.get("reason")
        if row.get("blockers") in (None, ""):
            row["blockers"] = row.get("primary_blocker")
        if row.get("volatility_regime") in (None, ""):
            row["volatility_regime"] = row.get("volatility")
        for component in _COMPONENTS:
            values = row.get(component)
            if not isinstance(values, Mapping):
                continue
            for field in ("long", "short", "reason"):
                target = f"{component}_{field}"
                if row.get(target) in (None, ""):
                    row[target] = values.get(field)
        rows.append(row)
    return rows


class IntelligenceRepository(Protocol):
    base_dir: Path
    query_timeout_seconds: float
    max_source_rows: int
    similar_min_sample: int

    def read_csv(self, path: str | Path) -> list[dict[str, str]]: ...
    def decision_rows(self) -> list[dict[str, str]]: ...
    def signal(self, symbol_value: str, timeframe: str | None = None) -> dict[str, Any] | None: ...


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _first(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, "", "N/A", "None"):
            return value
    return None


def _items(value: Any) -> tuple[str, ...]:
    if isinstance(value, (tuple, list, set)):
        return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    if isinstance(value, Mapping):
        return tuple(str(key) for key, active in value.items() if active)
    text = str(value or "").strip()
    if not text:
        return ()
    if text.startswith(("[", "{")):
        try:
            return _items(json.loads(text))
        except (ValueError, TypeError):
            pass
    separator = " | " if " | " in text else ";" if ";" in text else ","
    return tuple(dict.fromkeys(part.strip() for part in text.split(separator) if part.strip()))


def _text(value: Any) -> str | None:
    result = str(value or "").strip()
    return result or None


def _component_score(row: Mapping[str, Any], component: str, side: str | None) -> float | None:
    direct = _number(_first(row, f"{component}_score"))
    if direct is not None:
        return direct
    direction = str(side or "").upper()
    if direction == "LONG":
        return _number(row.get(f"{component}_long"))
    if direction == "SHORT":
        return _number(row.get(f"{component}_short"))
    values = [_number(row.get(f"{component}_long")), _number(row.get(f"{component}_short"))]
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _merged_evidence(row: Mapping[str, Any], *names: str) -> tuple[str, ...]:
    output: list[str] = []
    for name in names:
        output.extend(_items(row.get(name)))
    return tuple(dict.fromkeys(output))


def build_evidence_explanation(payload: SignalIntelligencePayload) -> EvidenceExplanation:
    """Return only evidence already present on the immutable payload."""
    confirmations = tuple(dict.fromkeys(payload.confirmations))
    limitations = tuple(dict.fromkeys((*payload.warnings, *payload.failed_filters, *payload.veto_reasons)))
    blockers = tuple(dict.fromkeys(payload.blockers))
    return EvidenceExplanation(
        confirmations=confirmations,
        limitations=limitations,
        blockers=blockers,
    )


def _snapshot_payload(row: Mapping[str, Any], signal: Mapping[str, Any]) -> SignalIntelligencePayload:
    card = dict(signal.get("payload") or {})
    side = _text(_first(row, "direction", "side") or card.get("side"))
    status = _text(_first(row, "signal", "decision", "status") or card.get("status"))
    confirmations = _merged_evidence(row, "confirmations", "reasons")
    warnings = _merged_evidence(row, "warnings", "limitations")
    component_evidence = _merged_evidence(
        row, "trend_reason", "structure_reason", "momentum_reason", "risk_reason",
    )
    card_evidence = _items(card.get("confirmations")) or _items(card.get("reasons"))
    if status in {"SETUP", "HIGH PRIORITY", "READY", "OPEN"}:
        confirmations = tuple(dict.fromkeys((*confirmations, *component_evidence, *card_evidence)))
    else:
        warnings = tuple(dict.fromkeys((*warnings, *component_evidence, *card_evidence)))
    blockers = _merged_evidence(row, "blockers", "primary_blocker") or _items(card.get("blockers"))
    payload = SignalIntelligencePayload(
        symbol=str(signal["symbol"]),
        timeframe=str(signal["timeframe"]),
        side=side,
        status=status,
        strategy_id=_text(_first(row, "strategy_id", "candidate_id") or card.get("strategy_id") or "LIVE_BASELINE"),
        asset_class=_text(row.get("asset_class")),
        current_price=_number(_first(row, "current_price", "price", "close") or card.get("current_price")),
        entry=_number(card.get("entry")),
        stop_loss=_number(card.get("stop_loss")),
        take_profit=_number(card.get("take_profit")),
        risk_reward=_number(card.get("risk_reward")),
        risk_percent=_number(card.get("risk_percent")),
        target_percent=_number(card.get("target_percent")),
        confidence=_number(_first(row, "confidence") or card.get("confidence")),
        quality=_text(_first(row, "quality") or card.get("quality")),
        score=_number(_first(row, "score", "signal_score", "weighted_score") or card.get("score")),
        signal_fingerprint=_text(row.get("signal_fingerprint")),
        cycle_id=_text(row.get("cycle_id")),
        snapshot_id=_text(_first(row, "snapshot_id", "source_snapshot_id")),
        timestamp=_text(_first(row, "timestamp", "decision_timestamp")),
        trend_score=_component_score(row, "trend", side),
        trend_max_score=_number(_first(row, "trend_max_score", "trend_max")),
        momentum_score=_component_score(row, "momentum", side),
        momentum_max_score=_number(_first(row, "momentum_max_score", "momentum_max")),
        structure_score=_component_score(row, "structure", side),
        structure_max_score=_number(_first(row, "structure_max_score", "structure_max")),
        risk_score=_component_score(row, "risk", side),
        risk_max_score=_number(_first(row, "risk_max_score", "risk_max")),
        rsi=_number(row.get("rsi")), adx=_number(row.get("adx")), atr=_number(row.get("atr")),
        atr_percent=_number(_first(row, "atr_percent", "atr_pct")),
        volume=_number(row.get("volume")), volume_ratio=_number(row.get("volume_ratio")),
        spread=_number(row.get("spread")), market_regime=_text(row.get("market_regime")),
        volatility_regime=_text(_first(row, "volatility_regime", "volatility")),
        session=_text(row.get("session")),
        trend_1h=_text(_first(row, "trend_1h") or card.get("trend_1h")),
        trend_4h=_text(_first(row, "trend_4h") or card.get("trend_4h")),
        trend_1d=_text(_first(row, "trend_1d") or card.get("trend_1d")),
        trend_direction=_text(row.get("trend_direction")),
        momentum_direction=_text(row.get("momentum_direction")),
        risk_direction=_text(row.get("risk_direction")),
        confirmations=confirmations,
        warnings=warnings,
        blockers=blockers,
        veto_reasons=_items(row.get("veto_reasons")),
        failed_filters=_items(row.get("failed_filters")),
        requirements_missing=_items(row.get("requirements_missing")),
    )
    return payload.model_copy(update={"explanation": build_evidence_explanation(payload)})


def _history_point(row: Mapping[str, Any], source: str) -> SignalHistoryPoint:
    side = _text(_first(row, "direction", "side"))
    reasons = _merged_evidence(
        row, "reasons", "confirmations", "summary", "trend_reason",
        "structure_reason", "momentum_reason", "risk_reason",
    )
    return SignalHistoryPoint(
        timestamp=_text(_first(row, "timestamp", "decision_timestamp")),
        cycle_id=_text(row.get("cycle_id")),
        status=_text(_first(row, "signal", "decision", "status")),
        side=side,
        confidence=_number(row.get("confidence")), quality=_text(row.get("quality")),
        score=_number(_first(row, "score", "signal_score", "weighted_score")),
        trend_score=_component_score(row, "trend", side),
        momentum_score=_component_score(row, "momentum", side),
        structure_score=_component_score(row, "structure", side),
        risk_score=_component_score(row, "risk", side),
        current_price=_number(_first(row, "current_price", "price", "close")),
        signal_fingerprint=_text(row.get("signal_fingerprint")),
        blockers=_merged_evidence(row, "blockers", "failed_filters", "veto_reasons", "primary_blocker"),
        reasons=reasons,
        source=source,
    )


def _timestamp_key(value: str | None) -> tuple[int, str]:
    if not value:
        return 0, ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return 1, parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return 0, value


def _delta(current: Any, previous: Any) -> float | None:
    left, right = _number(current), _number(previous)
    return round(left - right, 8) if left is not None and right is not None else None


def _transition(previous: Mapping[str, Any] | None, current: Mapping[str, Any] | None,
                field: str) -> str | None:
    if not previous or not current:
        return None
    before, after = _text(previous.get(field)), _text(current.get(field))
    return f"{before} → {after}" if before and after and before != after else None


def _page(items: list[Any], page: int, page_size: int) -> tuple[list[Any], int, int]:
    safe_page, safe_size = max(1, int(page)), min(200, max(1, int(page_size)))
    start = (safe_page - 1) * safe_size
    return items[start:start + safe_size], safe_page, safe_size


class SignalIntelligenceService:
    def __init__(self, repository: IntelligenceRepository) -> None:
        self.repository = repository

    @staticmethod
    def _scope(symbol_value: str, timeframe: str) -> tuple[str, str] | None:
        symbol = normalize_symbol(symbol_value)
        selected = str(timeframe).lower()
        if not re.fullmatch(r"[A-Z0-9]{2,20}/USDT", symbol) or selected not in ALLOWED_TIMEFRAMES:
            return None
        return symbol, selected

    def _research_rows(self, symbol: str, timeframe: str, strategy_id: str | None) -> list[dict[str, Any]]:
        path = self.repository.base_dir / "research.db"
        if not path.is_file():
            return []
        deadline = time.monotonic() + self.repository.query_timeout_seconds
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"file:{path.resolve()}?mode=ro", uri=True,
                timeout=min(1.0, self.repository.query_timeout_seconds),
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
            sql = """
                SELECT cycle_id, strategy_id, timestamp, symbol, timeframe,
                       decision, status, feature_snapshot_json, signal_fingerprint,
                       block_reason, blocked_reason
                FROM strategy_runs
                WHERE symbol=? AND lower(timeframe)=?
            """
            params: list[Any] = [symbol, timeframe]
            if strategy_id:
                sql += " AND strategy_id=?"
                params.append(strategy_id)
            sql += " ORDER BY timestamp DESC LIMIT ?"
            params.append(self.repository.max_source_rows)
            output = []
            for raw in connection.execute(sql, params):
                try:
                    features = json.loads(raw["feature_snapshot_json"] or "{}")
                except (ValueError, TypeError):
                    features = {}
                row = dict(features) if isinstance(features, Mapping) else {}
                row.update({key: raw[key] for key in raw.keys() if raw[key] not in (None, "")})
                row["signal"] = raw["decision"]
                row["blockers"] = raw["blocked_reason"] or raw["block_reason"] or row.get("blockers", "")
                output.append(row)
            return output
        except (OSError, sqlite3.Error, ValueError):
            return []
        finally:
            if connection is not None:
                connection.close()

    def history_points(self, symbol_value: str, timeframe: str,
                       strategy_id: str | None = None) -> list[SignalHistoryPoint]:
        scope = self._scope(symbol_value, timeframe)
        if scope is None:
            return []
        symbol, selected = scope
        sources = (
            ("DECISION_DEBUG", self.repository.base_dir / "decision_debug.csv"),
            ("SIGNALS_V3", self.repository.base_dir / "signals_v3.csv"),
            ("SETUP_HISTORY_V3", self.repository.base_dir / "setup_history_v3.csv"),
        )
        points: list[SignalHistoryPoint] = []
        for source, path in sources:
            for row in self.repository.read_csv(path):
                if normalize_symbol(row.get("symbol")) != symbol:
                    continue
                if str(row.get("timeframe") or "1h").lower() != selected:
                    continue
                row_strategy = str(row.get("strategy_id") or "LIVE_BASELINE")
                if strategy_id and row_strategy != strategy_id:
                    continue
                points.append(_history_point(row, source))
        for row in self._research_rows(symbol, selected, strategy_id):
            points.append(_history_point(row, "RESEARCH_FEATURE_SNAPSHOT"))
        unique: dict[tuple[Any, ...], SignalHistoryPoint] = {}
        for point in points:
            key = (
                point.timestamp, point.cycle_id, point.status, point.side,
                point.signal_fingerprint,
            )
            existing = unique.get(key)
            completeness = sum(value is not None for value in (
                point.confidence, point.score, point.trend_score, point.momentum_score,
                point.structure_score, point.risk_score, point.current_price,
            )) + len(point.blockers) + len(point.reasons)
            existing_completeness = -1 if existing is None else sum(value is not None for value in (
                existing.confidence, existing.score, existing.trend_score, existing.momentum_score,
                existing.structure_score, existing.risk_score, existing.current_price,
            )) + len(existing.blockers) + len(existing.reasons)
            if completeness > existing_completeness:
                unique[key] = point
        return sorted(unique.values(), key=lambda item: _timestamp_key(item.timestamp))

    def intelligence(self, symbol_value: str, timeframe: str) -> SignalIntelligencePayload | None:
        scope = self._scope(symbol_value, timeframe)
        if scope is None:
            return None
        symbol, selected = scope
        signal = self.repository.signal(symbol, selected)
        if signal is None:
            return None
        current = latest_rows(self.repository.decision_rows()).get((symbol, selected))
        if current is None:
            return None
        payload = _snapshot_payload(current, signal)
        similar = self.similar(symbol, selected, source="LIVE", page=1, page_size=1)
        return payload.model_copy(update={
            "similar_setups_count": similar.total,
            "similar_setups_winrate": similar.winrate,
            "similar_setups_profit_factor": similar.profit_factor,
            "similar_setups_average_r": similar.average_r,
            "similar_setups_confidence": similar.confidence,
        })

    def history(self, symbol_value: str, timeframe: str, *, page: int = 1,
                page_size: int = 50) -> PaginatedHistoryResponse:
        points = self.history_points(symbol_value, timeframe)
        selected, safe_page, safe_size = _page(points, page, page_size)
        return PaginatedHistoryResponse(
            status="OK" if points else "NO_HISTORY",
            items=tuple(selected), count=len(selected), total=len(points),
            page=safe_page, page_size=safe_size,
        )

    def changes(self, symbol_value: str, timeframe: str) -> SignalChangesResponse:
        points = self.history_points(symbol_value, timeframe)
        if not points:
            return SignalChangesResponse(status="NO_HISTORY")
        dictionaries = [point.model_dump() for point in points]
        current = dictionaries[-1]
        previous = dictionaries[-2] if len(dictionaries) >= 2 else None
        three_cycles_ago = dictionaries[-4] if len(dictionaries) >= 4 else None
        comparison = previous or {}
        deltas = {name: _delta(current.get(name), comparison.get(name)) for name in _CHANGE_METRICS}
        current_blockers, previous_blockers = set(current.get("blockers", ())), set(comparison.get("blockers", ()))
        current_reasons, previous_reasons = set(current.get("reasons", ())), set(comparison.get("reasons", ()))
        series_fields = ("timestamp", "confidence", "score", "trend_score", "momentum_score",
                         "structure_score", "risk_score", "current_price")
        series = tuple({field: row.get(field) for field in series_fields} for row in dictionaries[-20:])
        return SignalChangesResponse(
            status="OK" if previous else "INSUFFICIENT_HISTORY",
            series=series, current=current, previous=previous,
            three_cycles_ago=three_cycles_ago, deltas=deltas,
            transitions={
                "status": _transition(previous, current, "status"),
                "side": _transition(previous, current, "side"),
            },
            blockers_added=tuple(sorted(current_blockers - previous_blockers)),
            blockers_removed=tuple(sorted(previous_blockers - current_blockers)),
            confirmations_added=tuple(sorted(current_reasons - previous_reasons)),
            confirmations_removed=tuple(sorted(previous_reasons - current_reasons)),
        )

    @staticmethod
    def _explicit_requirements(payload: SignalIntelligencePayload) -> list[SignalRequirement]:
        requirements: list[SignalRequirement] = []
        patterns = (
            re.compile(r"^([A-Za-z_ /-]+):?\s*(-?\d+(?:\.\d+)?)\s*(<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)$"),
            re.compile(r"^([A-Za-z_ /-]+)\s*(<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)$"),
        )
        evidence = (*payload.requirements_missing, *payload.failed_filters, *payload.veto_reasons, *payload.blockers)
        for item in evidence:
            compact = item.strip()
            match = patterns[0].match(compact)
            if match:
                metric, current, comparison, required = match.groups()
            else:
                match = patterns[1].match(compact)
                if not match:
                    continue
                metric, comparison, required = match.groups()
                current = None
            requirements.append(SignalRequirement(
                metric=metric.strip().upper(), current_value=_number(current),
                required_value=float(required), comparison=comparison,
                status="NOT_MET", source="SAVED_EVIDENCE",
            ))
        return requirements

    def requirements(self, symbol_value: str, timeframe: str) -> SignalRequirementsResponse:
        payload = self.intelligence_without_similar(symbol_value, timeframe)
        if payload is None:
            return SignalRequirementsResponse(status="SIGNAL_NOT_FOUND")
        requirements = self._explicit_requirements(payload)
        scope = self._scope(symbol_value, timeframe)
        current = latest_rows(self.repository.decision_rows()).get(scope) if scope else None
        if current:
            known_fields = (
                ("ADX", "adx", ("required_adx", "adx_required", "adx_threshold"), ">="),
                ("RISK/REWARD", "risk_reward", ("required_rr", "min_rr", "minimum_rr", "rr_threshold"), ">="),
                ("MOMENTUM", "momentum_score", ("required_momentum_score", "momentum_required", "momentum_threshold"), ">="),
                ("TREND", "trend_score", ("required_trend_score", "trend_required", "trend_threshold"), ">="),
                ("STRUCTURE", "structure_score", ("required_structure_score", "structure_required", "structure_threshold"), ">="),
                ("RISK", "risk_score", ("required_risk_score", "risk_required", "risk_threshold"), ">="),
                ("VOLUME RATIO", "volume_ratio", ("required_volume_ratio", "min_volume_ratio"), ">="),
                ("SPREAD", "spread", ("max_spread", "spread_max"), "<="),
            )
            payload_values = payload.model_dump()
            for metric, value_field, threshold_fields, comparison in known_fields:
                threshold_name = next((name for name in threshold_fields if _number(current.get(name)) is not None), None)
                if threshold_name is None:
                    continue
                value = _number(payload_values.get(value_field))
                threshold = _number(current.get(threshold_name))
                if value is None or threshold is None:
                    continue
                missing = value < threshold if comparison == ">=" else value > threshold
                if not missing:
                    continue
                requirements.append(SignalRequirement(
                    metric=metric, current_value=value, required_value=threshold,
                    comparison=comparison, status="NOT_MET",
                    source=f"SNAPSHOT_FIELD:{threshold_name}",
                ))
        deduplicated = {
            (item.metric, item.current_value, item.required_value, item.comparison): item
            for item in requirements
        }
        return SignalRequirementsResponse(
            status="OK" if deduplicated else "NO_KNOWN_REQUIREMENTS",
            items=tuple(deduplicated.values()),
        )

    def _trade_rows(self, source: str) -> list[dict[str, str]]:
        paths = {
            "LIVE": self.repository.base_dir / "trades.csv",
            "LEGACY_SHADOW": self.repository.base_dir / "candidate_shadow_trades.csv",
            "RESEARCH_LAB": self.repository.base_dir / "research_lab_shadow_history.csv",
        }
        return self.repository.read_csv(paths[source])

    @staticmethod
    def _trade_features(row: Mapping[str, Any]) -> dict[str, Any]:
        features: dict[str, Any] = {}
        raw = row.get("feature_snapshot_json") or row.get("feature_snapshot")
        if isinstance(raw, Mapping):
            features.update(raw)
        elif raw:
            try:
                loaded = json.loads(str(raw))
                if isinstance(loaded, Mapping):
                    features.update(loaded)
            except (ValueError, TypeError):
                pass
        features.update({key: value for key, value in row.items() if value not in (None, "")})
        return features

    @staticmethod
    def _similarity(payload: SignalIntelligencePayload, row: Mapping[str, Any]) -> tuple[float, tuple[str, ...]]:
        features = SignalIntelligenceService._trade_features(row)
        checks: list[tuple[str, bool | None, float]] = []
        row_symbol = normalize_symbol(features.get("symbol"))
        row_asset_class = _text(features.get("asset_class"))
        same_symbol = row_symbol == payload.symbol if row_symbol else False
        same_asset_class = bool(
            row_asset_class and payload.asset_class and
            row_asset_class.upper() == payload.asset_class.upper()
        )
        if not same_symbol and not same_asset_class:
            return 0.0, ()
        checks.append(("symbol", same_symbol, 2.0))
        checks.append(("asset_class", same_asset_class if row_asset_class and payload.asset_class else None, 1.0))
        for expected, aliases in (
            (payload.strategy_id, ("strategy_id", "candidate_id")),
            (payload.side, ("side", "direction")),
            (payload.timeframe, ("timeframe",)),
        ):
            actual = _text(_first(features, *aliases))
            if actual and expected and actual.upper() != str(expected).upper():
                return 0.0, ()
        for name, expected, aliases in (
            ("strategy_id", payload.strategy_id, ("strategy_id", "candidate_id")),
            ("side", payload.side, ("side", "direction")),
            ("timeframe", payload.timeframe, ("timeframe",)),
            ("market_regime", payload.market_regime, ("market_regime",)),
            ("trend_direction", payload.trend_direction, ("trend_direction", "trend")),
            ("volatility_regime", payload.volatility_regime, ("volatility_regime", "volatility")),
        ):
            actual = _text(_first(features, *aliases))
            checks.append((name, actual.upper() == str(expected).upper() if actual and expected else None, 1.0))
        for name, expected, tolerance in (
            ("score", payload.score, 10.0), ("confidence", payload.confidence, 10.0),
        ):
            actual = _number(features.get(name))
            checks.append((name, abs(actual - expected) <= tolerance if actual is not None and expected is not None else None, 1.0))
        available = [(name, matched, weight) for name, matched, weight in checks if matched is not None]
        if not available:
            return 0.0, ()
        matched = tuple(name for name, passed, _ in available if passed)
        score = sum(weight for _, passed, weight in available if passed) / sum(weight for _, _, weight in available)
        return round(score, 6), matched

    def similar(self, symbol_value: str, timeframe: str, *, source: str = "LIVE",
                page: int = 1, page_size: int = 20) -> SimilarSetupsResponse:
        normalized_source = str(source).upper()
        if normalized_source not in ALLOWED_TRADE_SOURCES:
            return SimilarSetupsResponse(
                status="INVALID_SOURCE", source=normalized_source, count=0, total=0,
                page=max(1, page), page_size=min(200, max(1, page_size)),
                minimum_sample=self.repository.similar_min_sample,
                statistics_available=False,
            )
        payload = self.intelligence_without_similar(symbol_value, timeframe)
        if payload is None:
            return SimilarSetupsResponse(
                status="SIGNAL_NOT_FOUND", source=normalized_source, count=0, total=0,
                page=max(1, page), page_size=min(200, max(1, page_size)),
                minimum_sample=self.repository.similar_min_sample,
                statistics_available=False,
            )
        matches: list[SimilarSetup] = []
        for row in self._trade_rows(normalized_source):
            score, matched = self._similarity(payload, row)
            if score <= 0 or not matched:
                continue
            features = self._trade_features(row)
            matches.append(SimilarSetup(
                trade_id=_text(_first(row, "trade_id", "shadow_trade_id", "id")),
                symbol=_text(row.get("symbol")), side=_text(_first(row, "side", "direction")),
                timeframe=_text(row.get("timeframe")),
                timestamp=_text(_first(row, "entry_time", "opened_at", "timestamp")),
                entry=_number(_first(row, "entry", "entry_price")),
                exit=_number(_first(row, "exit", "exit_price")), result=_text(row.get("result")),
                pnl_r=_number(row.get("pnl_r")),
                strategy_id=_text(_first(row, "strategy_id", "candidate_id")),
                source=normalized_source, similarity_score=score, matched_features=matched,
            ))
        matches.sort(key=lambda item: (item.similarity_score, _timestamp_key(item.timestamp)), reverse=True)
        selected, safe_page, safe_size = _page(matches, page, page_size)
        pnl_values = [item.pnl_r for item in matches if item.pnl_r is not None]
        available = len(pnl_values) >= self.repository.similar_min_sample
        wins = [value for value in pnl_values if value > 0]
        losses = [value for value in pnl_values if value < 0]
        gross_profit, gross_loss = sum(wins), abs(sum(losses))
        return SimilarSetupsResponse(
            status="OK" if matches else "NO_SIMILAR_SETUPS", source=normalized_source,
            items=tuple(selected), count=len(selected), total=len(matches),
            page=safe_page, page_size=safe_size,
            minimum_sample=self.repository.similar_min_sample,
            statistics_available=available,
            winrate=round(len(wins) / len(pnl_values) * 100, 4) if available else None,
            profit_factor=(round(gross_profit / gross_loss, 4) if available and gross_loss > 0 else None),
            average_r=(round(sum(pnl_values) / len(pnl_values), 6) if available else None),
            confidence=("SUFFICIENT" if available else None),
        )

    def intelligence_without_similar(self, symbol_value: str, timeframe: str) -> SignalIntelligencePayload | None:
        scope = self._scope(symbol_value, timeframe)
        if scope is None:
            return None
        symbol, selected = scope
        signal = self.repository.signal(symbol, selected)
        current = latest_rows(self.repository.decision_rows()).get((symbol, selected))
        return _snapshot_payload(current, signal) if signal is not None and current is not None else None
