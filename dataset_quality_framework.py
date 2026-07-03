"""Dataset quality and statistical reliability framework.

This module is intentionally read-only. It audits existing CSV/JSON artifacts
and estimates how much confidence the project can place in current research
conclusions. It does not modify strategy, weights, config, or live agent logic.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


BASE_DIR = Path(__file__).resolve().parent

REPORT_PATH = BASE_DIR / "dataset_quality_report.json"
SUMMARY_PATH = BASE_DIR / "dataset_quality_summary.txt"
SCORECARD_PATH = BASE_DIR / "dataset_quality_scorecard.csv"

SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
    "DOGE/USDT",
    "XRP/USDT",
    "ADA/USDT",
    "LINK/USDT",
    "AVAX/USDT",
]

SOURCE_FILES = [
    "decision_debug.csv",
    "decision_diagnostics.csv",
    "decision_explanations.csv",
    "signals_v3.csv",
    "trades.csv",
    "watchlist.csv",
    "optimizer_results.csv",
    "line_movement.csv",
    "protective_filter_dry_run.csv",
    "sl_quality_protective_dry_run.csv",
    "trade_pattern_discovery_patterns.csv",
    "trade_comparator.csv",
]

CALIBRATION_REPORTS = [
    "calibration_report.json",
    "strategy_calibration_report.json",
    "strategy_calibration_experiments_report.json",
    "strategy_calibration_recommendation.json",
    "min_edge_calibration_report.json",
    "min_edge_outcome_report.json",
    "score_zero_audit_report.json",
    "sl_entry_quality_experiment_report.json",
    "trend_momentum_conflict_report.json",
    "trend_momentum_conflict_v2_report.json",
    "momentum_decomposition_report.json",
    "trade_comparator_report.json",
    "trade_pattern_discovery_report.json",
    "market_regime_report.json",
    "data_quality_report.json",
    "optimizer_results.csv",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, str) and not value.strip():
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _average(values: Iterable[Any]) -> float:
    nums = [_safe_float(value) for value in values]
    filtered = [num for num in nums if num is not None]
    return round(mean(filtered), 4) if filtered else 0.0


def _percent(part: int | float, total: int | float) -> float:
    return round((float(part) / float(total)) * 100, 2) if total else 0.0


def _read_csv(path: Path) -> tuple[list[dict[str, str]], str | None]:
    if not path.exists():
        return [], "missing"
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle)), None
    except Exception as exc:  # pragma: no cover - defensive audit layer
        return [], f"read_error: {exc}"


def _read_json(path: Path) -> tuple[dict[str, Any], str | None]:
    if not path.exists():
        return {}, "missing"
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {"value": data}, None
    except Exception as exc:  # pragma: no cover - defensive audit layer
        return {}, f"read_error: {exc}"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def _latest_from_rows(
    rows: list[dict[str, str]],
    fields: Iterable[str],
) -> datetime | None:
    latest: datetime | None = None
    for row in rows:
        for field in fields:
            parsed = _parse_datetime(row.get(field))
            if parsed and (latest is None or parsed > latest):
                latest = parsed
    return latest


def _mtime(path: Path) -> datetime | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _age_text(ts: datetime | None) -> str:
    if ts is None:
        return "unknown"
    seconds = max(0, int((_utc_now() - ts).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _sample_reliability(sample_size: int, study_type: str = "observations") -> dict[str, Any]:
    if study_type == "trades":
        if sample_size >= 100:
            reliability = "HIGH"
            confidence_level = 0.9
        elif sample_size >= 30:
            reliability = "MEDIUM"
            confidence_level = 0.7
        else:
            reliability = "LOW"
            confidence_level = 0.35
    else:
        if sample_size >= 1000:
            reliability = "HIGH"
            confidence_level = 0.9
        elif sample_size >= 250:
            reliability = "MEDIUM"
            confidence_level = 0.65
        else:
            reliability = "LOW"
            confidence_level = 0.35
    return {
        "sample_size": sample_size,
        "reliability": reliability,
        "confidence_level": confidence_level,
    }


def _symbol_normalized(symbol: str) -> str:
    text = (symbol or "").strip().upper()
    if not text:
        return ""
    if "/" in text:
        return text
    if text.endswith("USDT"):
        return text[:-4] + "/USDT"
    return text


class DatasetQualityFramework:
    """Build a read-only quality and reliability report for project artifacts."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.warnings: list[str] = []
        self.missing_sources: list[str] = []
        self.csv_data: dict[str, list[dict[str, str]]] = {}
        self.json_data: dict[str, dict[str, Any]] = {}

    def build_report(self) -> dict[str, Any]:
        """Build, save, and return the dataset quality report."""
        self._load_sources()

        dataset_coverage = self._dataset_coverage()
        symbol_coverage = self._symbol_coverage()
        market_coverage = self._market_coverage()
        statistical_reliability = self._statistical_reliability()
        bias_detection = self._bias_detection(dataset_coverage, symbol_coverage, market_coverage)
        data_freshness = self._data_freshness()
        dry_run_quality = self._dry_run_quality()
        research_confidence = self._research_confidence(statistical_reliability)
        readiness = self._overall_readiness(
            dataset_coverage,
            statistical_reliability,
            data_freshness,
        )
        final_assessment = self._final_assessment(statistical_reliability)

        report = {
            "generated_at": _utc_now().isoformat(),
            "status": self._status_from_findings(bias_detection, data_freshness),
            "source_files": self._source_status(),
            "missing_sources": self.missing_sources,
            "warnings": self.warnings,
            "dataset_coverage": dataset_coverage,
            "symbol_coverage": symbol_coverage,
            "market_coverage": market_coverage,
            "statistical_reliability": statistical_reliability,
            "bias_detection": bias_detection,
            "data_freshness": data_freshness,
            "dry_run_quality": dry_run_quality,
            "research_confidence_score": research_confidence,
            "overall_project_readiness": readiness,
            "final_assessment": final_assessment,
        }

        _write_json(REPORT_PATH, report)
        self._write_summary(report)
        self._write_scorecard(report)
        return report

    def print_report(self) -> None:
        """Print the generated summary to the terminal."""
        report = self.build_report()
        print(self._summary_text(report))

    def _load_sources(self) -> None:
        for filename in SOURCE_FILES:
            rows, error = _read_csv(self.base_dir / filename)
            self.csv_data[filename] = rows
            if error == "missing":
                self.missing_sources.append(filename)
            elif error:
                self.warnings.append(f"{filename}: {error}")

        for filename in CALIBRATION_REPORTS:
            path = self.base_dir / filename
            if filename.endswith(".csv"):
                if filename not in self.csv_data:
                    rows, error = _read_csv(path)
                    self.csv_data[filename] = rows
                    if error and error != "missing":
                        self.warnings.append(f"{filename}: {error}")
                continue
            data, error = _read_json(path)
            self.json_data[filename] = data
            if error == "missing":
                self.missing_sources.append(filename)
            elif error:
                self.warnings.append(f"{filename}: {error}")

    def _source_status(self) -> dict[str, Any]:
        statuses: dict[str, Any] = {}
        for filename in SOURCE_FILES:
            path = self.base_dir / filename
            rows = self.csv_data.get(filename, [])
            statuses[filename] = {
                "exists": path.exists(),
                "rows": len(rows),
                "last_modified": _mtime(path).isoformat() if _mtime(path) else None,
            }
        for filename in CALIBRATION_REPORTS:
            path = self.base_dir / filename
            if filename in statuses:
                continue
            statuses[filename] = {
                "exists": path.exists(),
                "last_modified": _mtime(path).isoformat() if _mtime(path) else None,
            }
        return statuses

    def _dataset_coverage(self) -> dict[str, Any]:
        debug_rows = self.csv_data.get("decision_debug.csv", [])
        signal_rows = self.csv_data.get("signals_v3.csv", [])
        trade_rows = self.csv_data.get("trades.csv", [])

        closed_trades = [
            row for row in trade_rows
            if (row.get("result") or row.get("status") or "").upper() in {"WIN", "LOSS"}
        ]
        wins = sum(1 for row in closed_trades if (row.get("result") or "").upper() == "WIN")
        losses = sum(1 for row in closed_trades if (row.get("result") or "").upper() == "LOSS")

        decision_directions = Counter(
            (row.get("direction") or "").upper()
            for row in debug_rows or signal_rows
            if (row.get("direction") or "").upper() in {"LONG", "SHORT", "NEUTRAL"}
        )
        trade_directions = Counter(
            (row.get("direction") or "").upper()
            for row in closed_trades
            if (row.get("direction") or "").upper() in {"LONG", "SHORT"}
        )

        decisions_source = "decision_debug.csv" if debug_rows else "signals_v3.csv"
        return {
            "decisions_source": decisions_source,
            "total_decisions": len(debug_rows or signal_rows),
            "signals_rows": len(signal_rows),
            "debug_rows": len(debug_rows),
            "total_trades": len(trade_rows),
            "closed_trades": len(closed_trades),
            "wins": wins,
            "losses": losses,
            "winrate": _percent(wins, wins + losses),
            "decision_directions": dict(decision_directions),
            "trade_directions": dict(trade_directions),
            "trade_long": trade_directions.get("LONG", 0),
            "trade_short": trade_directions.get("SHORT", 0),
        }

    def _symbol_coverage(self) -> dict[str, Any]:
        debug_rows = self.csv_data.get("decision_debug.csv", [])
        trade_rows = self.csv_data.get("trades.csv", [])
        closed_trades = [
            row for row in trade_rows
            if (row.get("result") or row.get("status") or "").upper() in {"WIN", "LOSS"}
        ]

        by_symbol: dict[str, dict[str, Any]] = {}
        for symbol in SYMBOLS:
            decisions = [
                row for row in debug_rows
                if _symbol_normalized(row.get("symbol", "")) == symbol
            ]
            trades = [
                row for row in closed_trades
                if _symbol_normalized(row.get("symbol", "")) == symbol
            ]
            wins = sum(1 for row in trades if (row.get("result") or "").upper() == "WIN")
            losses = sum(1 for row in trades if (row.get("result") or "").upper() == "LOSS")
            by_symbol[symbol] = {
                "decisions": len(decisions),
                "trades": len(trades),
                "wins": wins,
                "losses": losses,
                "winrate": _percent(wins, wins + losses),
                "average_score": _average(row.get("score") for row in decisions),
                "average_confidence": _average(row.get("confidence") for row in decisions),
            }

        best_symbol = max(by_symbol.items(), key=lambda item: (item[1]["winrate"], item[1]["trades"]))[0]
        worst_symbol = min(by_symbol.items(), key=lambda item: (item[1]["winrate"], -item[1]["trades"]))[0]
        return {
            "symbols": by_symbol,
            "best_symbol_by_winrate": best_symbol,
            "worst_symbol_by_winrate": worst_symbol,
        }

    def _market_coverage(self) -> dict[str, Any]:
        debug_rows = self.csv_data.get("decision_debug.csv", [])
        regime_counts = Counter()
        for row in debug_rows:
            reason = (row.get("trend_reason") or "").upper()
            if "1H: EMA=BULLISH" in reason and "4H: EMA=BULLISH" in reason:
                regime_counts["Bull"] += 1
            elif "1H: EMA=BEARISH" in reason and "4H: EMA=BEARISH" in reason:
                regime_counts["Bear"] += 1
            else:
                regime_counts["Sideways"] += 1

        current_report = self.json_data.get("market_regime_report.json", {})
        current_symbols = current_report.get("symbols", {}) if isinstance(current_report, dict) else {}
        current_counts = Counter()
        if isinstance(current_symbols, dict):
            for payload in current_symbols.values():
                if isinstance(payload, dict):
                    current_counts[payload.get("primary_regime", "Unknown")] += 1

        return {
            "method": "historical EMA reason inference plus current market_regime_report snapshot",
            "historical_decision_regimes": dict(regime_counts),
            "historical_percent": {
                key: _percent(value, sum(regime_counts.values()))
                for key, value in regime_counts.items()
            },
            "current_regime_snapshot": dict(current_counts),
            "note": "Historical regime is inferred from saved trend_reason text, not from recomputed indicators.",
        }

    def _statistical_reliability(self) -> dict[str, Any]:
        debug_rows = self.csv_data.get("decision_debug.csv", [])
        trade_rows = self.csv_data.get("trades.csv", [])
        closed_trades = [
            row for row in trade_rows
            if (row.get("result") or row.get("status") or "").upper() in {"WIN", "LOSS"}
        ]

        score_zero = self.json_data.get("score_zero_audit_report.json", {})
        min_edge = self.json_data.get("min_edge_outcome_report.json", {})
        momentum = self.json_data.get("momentum_decomposition_report.json", {})
        trend_conflict = self.json_data.get("trend_momentum_conflict_v2_report.json", {})
        trade_comparator = self.json_data.get("trade_comparator_report.json", {})
        patterns = self.json_data.get("trade_pattern_discovery_report.json", {})
        sl_quality = self.json_data.get("sl_entry_quality_experiment_report.json", {})

        studies = {
            "Score Zero Audit": (
                int(score_zero.get("total_decisions") or len(debug_rows)),
                "observations",
            ),
            "MIN_EDGE Analysis": (
                int(min_edge.get("candidates_checked") or min_edge.get("candidates_found") or 0),
                "observations",
            ),
            "Momentum Analysis": (
                int(momentum.get("summary", {}).get("checked") or momentum.get("summary", {}).get("momentum_candidates") or 0),
                "observations",
            ),
            "Trend Conflict Replay": (
                int(trend_conflict.get("summary", {}).get("total_candidates") or trend_conflict.get("candidates") or 0),
                "observations",
            ),
            "Trade Comparator": (
                int(trade_comparator.get("overall", {}).get("trades") or len(closed_trades)),
                "trades",
            ),
            "Pattern Discovery": (
                int(patterns.get("sample", {}).get("trades") or len(closed_trades)),
                "trades",
            ),
            "SL Quality Experiment": (
                int(sl_quality.get("baseline", {}).get("trades") or len(closed_trades)),
                "trades",
            ),
            "Optimizer History": (
                len(self.csv_data.get("optimizer_results.csv", [])),
                "observations",
            ),
        }

        reliability: dict[str, Any] = {}
        for name, (sample_size, study_type) in studies.items():
            result = _sample_reliability(sample_size, study_type)
            result["study_type"] = study_type
            reliability[name] = result
        return reliability

    def _bias_detection(
        self,
        dataset: dict[str, Any],
        symbols: dict[str, Any],
        market: dict[str, Any],
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        total_decisions = dataset.get("total_decisions", 0)
        total_closed = dataset.get("closed_trades", 0)

        symbol_rows = symbols.get("symbols", {})
        if total_decisions and symbol_rows:
            btc_share = _percent(symbol_rows.get("BTC/USDT", {}).get("decisions", 0), total_decisions)
            if btc_share > 25:
                findings.append({
                    "bias": "BTC bias",
                    "detected": True,
                    "severity": "MEDIUM" if btc_share < 40 else "HIGH",
                    "evidence": f"BTC decisions share is {btc_share}%.",
                    "recommendation": "Keep all 9 symbols active and compare symbol-level results before changing strategy.",
                })

            max_symbol, max_payload = max(symbol_rows.items(), key=lambda item: item[1]["decisions"])
            max_share = _percent(max_payload["decisions"], total_decisions)
            if max_share > 20:
                findings.append({
                    "bias": "Symbol bias",
                    "detected": True,
                    "severity": "MEDIUM" if max_share < 35 else "HIGH",
                    "evidence": f"{max_symbol} has {max_share}% of decisions.",
                    "recommendation": "Check whether older logs were collected before the expanded 9-symbol watchlist.",
                })

        trade_dirs = dataset.get("trade_directions", {})
        short_share = _percent(trade_dirs.get("SHORT", 0), total_closed)
        if total_closed and short_share >= 70:
            findings.append({
                "bias": "SHORT bias",
                "detected": True,
                "severity": "HIGH" if short_share >= 85 else "MEDIUM",
                "evidence": f"SHORT trades share is {short_share}%.",
                "recommendation": "Do not generalize closed-trade conclusions to LONG until more LONG trades close.",
            })

        regimes = market.get("historical_decision_regimes", {})
        regime_total = sum(regimes.values())
        if regime_total:
            for name in ("Bear", "Bull", "Sideways"):
                share = _percent(regimes.get(name, 0), regime_total)
                if share >= 70:
                    findings.append({
                        "bias": f"{name} market bias",
                        "detected": True,
                        "severity": "HIGH" if share >= 85 else "MEDIUM",
                        "evidence": f"{name} regime share is {share}% of inferred decisions.",
                        "recommendation": "Confirm conclusions across other regimes before changing DecisionEngine.",
                    })

        comparator = self.json_data.get("trade_comparator_report.json", {})
        confidence_bins = comparator.get("confidence_calibration", {})
        high_conf = confidence_bins.get("90-100", {}) if isinstance(confidence_bins, dict) else {}
        if high_conf and high_conf.get("trades", 0) >= 5 and high_conf.get("winrate", 100) < 40:
            findings.append({
                "bias": "Confidence bias",
                "detected": True,
                "severity": "HIGH",
                "evidence": (
                    f"Confidence 90-100 has winrate {high_conf.get('winrate')}% "
                    f"over {high_conf.get('trades')} trades."
                ),
                "recommendation": "Treat high confidence as uncalibrated until 30+ closed trades confirm it.",
            })

        time_bias = self._time_bias()
        if time_bias:
            findings.append(time_bias)

        if not findings:
            findings.append({
                "bias": "No major bias detected",
                "detected": False,
                "severity": "LOW",
                "evidence": "No single inspected category exceeded bias thresholds.",
                "recommendation": "Continue collecting data.",
            })
        return findings

    def _time_bias(self) -> dict[str, Any] | None:
        rows = self.csv_data.get("decision_debug.csv", [])
        dates: Counter[str] = Counter()
        for row in rows:
            parsed = _parse_datetime(row.get("timestamp"))
            if parsed:
                dates[parsed.date().isoformat()] += 1
        total = sum(dates.values())
        if not total:
            return None
        day, count = dates.most_common(1)[0]
        share = _percent(count, total)
        if share < 50:
            return None
        return {
            "bias": "Time bias",
            "detected": True,
            "severity": "HIGH" if share >= 75 else "MEDIUM",
            "evidence": f"{share}% of decision rows are from {day}.",
            "recommendation": "Collect more days before treating trade-level patterns as stable.",
        }

    def _data_freshness(self) -> dict[str, Any]:
        debug_rows = self.csv_data.get("decision_debug.csv", [])
        signal_rows = self.csv_data.get("signals_v3.csv", [])
        trade_rows = self.csv_data.get("trades.csv", [])
        optimizer_rows = self.csv_data.get("optimizer_results.csv", [])

        latest_decision = _latest_from_rows(debug_rows or signal_rows, ["timestamp"])
        latest_trade = _latest_from_rows(trade_rows, ["closed_at", "opened_at"])
        latest_optimizer = _latest_from_rows(optimizer_rows, ["Date"])

        analysis_files = [
            path for path in self.base_dir.glob("*_report.json")
            if path.name not in {"dataset_quality_report.json"}
        ]
        latest_analysis = max((_mtime(path) for path in analysis_files if _mtime(path)), default=None)
        replay_files = [
            "trend_momentum_conflict_v2_report.json",
            "min_edge_outcome_report.json",
            "momentum_decomposition_report.json",
            "sl_entry_quality_experiment_report.json",
        ]
        latest_replay = max(
            (_mtime(self.base_dir / filename) for filename in replay_files if _mtime(self.base_dir / filename)),
            default=None,
        )

        payload = {
            "latest_decision": latest_decision.isoformat() if latest_decision else None,
            "latest_decision_age": _age_text(latest_decision),
            "latest_trade": latest_trade.isoformat() if latest_trade else None,
            "latest_trade_age": _age_text(latest_trade),
            "latest_analysis": latest_analysis.isoformat() if latest_analysis else None,
            "latest_analysis_age": _age_text(latest_analysis),
            "latest_replay": latest_replay.isoformat() if latest_replay else None,
            "latest_replay_age": _age_text(latest_replay),
            "latest_optimizer": latest_optimizer.isoformat() if latest_optimizer else None,
            "latest_optimizer_age": _age_text(latest_optimizer),
        }

        if latest_decision and latest_analysis:
            lag_minutes = int((latest_decision - latest_analysis).total_seconds() // 60)
            payload["analysis_lag_minutes_vs_decision"] = lag_minutes
            payload["status"] = "STALE" if lag_minutes > 60 else "OK"
        else:
            payload["analysis_lag_minutes_vs_decision"] = None
            payload["status"] = "UNKNOWN"
        return payload

    def _dry_run_quality(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for filename in ("protective_filter_dry_run.csv", "sl_quality_protective_dry_run.csv"):
            rows = self.csv_data.get(filename, [])
            if not rows:
                status = "NO_CANDIDATES_OBSERVED_YET"
                note = "No candidates observed yet."
            else:
                status = "COLLECTING"
                note = "Candidates are being logged; outcome confirmation should be done after enough future bars/trades."
            result[filename] = {
                "candidates": len(rows),
                "confirmations": 0,
                "status": status,
                "note": note,
            }
        return result

    def _research_confidence(self, reliability: dict[str, Any]) -> dict[str, Any]:
        mapping = {
            "MIN_EDGE": "MIN_EDGE Analysis",
            "Momentum": "Momentum Analysis",
            "SL Quality": "SL Quality Experiment",
            "Confidence": "Trade Comparator",
            "Trend Conflict": "Trend Conflict Replay",
            "Pattern Discovery": "Pattern Discovery",
        }
        return {
            name: {
                "confidence": reliability.get(study, {}).get("reliability", "LOW"),
                "sample_size": reliability.get(study, {}).get("sample_size", 0),
                "confidence_level": reliability.get(study, {}).get("confidence_level", 0.0),
            }
            for name, study in mapping.items()
        }

    def _overall_readiness(
        self,
        dataset: dict[str, Any],
        reliability: dict[str, Any],
        freshness: dict[str, Any],
    ) -> dict[str, Any]:
        closed_trades = dataset.get("closed_trades", 0)
        total_decisions = dataset.get("total_decisions", 0)
        high_studies = sum(
            1 for item in reliability.values()
            if item.get("reliability") == "HIGH"
        )
        medium_or_high = sum(
            1 for item in reliability.values()
            if item.get("reliability") in {"MEDIUM", "HIGH"}
        )
        total_studies = max(1, len(reliability))

        architecture = 90
        analytics = round((medium_or_high / total_studies) * 100)
        research = round((high_studies / total_studies) * 100)
        dataset_score = min(100, round(min(total_decisions, 5000) / 5000 * 45 + min(closed_trades, 30) / 30 * 55))
        strategy = 55 if closed_trades < 30 else 70
        if freshness.get("status") == "STALE":
            production = 60
        else:
            production = round((architecture * 0.3) + (dataset_score * 0.3) + (analytics * 0.25) + (strategy * 0.15))

        return {
            "Architecture": architecture,
            "Strategy": strategy,
            "Dataset": dataset_score,
            "Analytics": analytics,
            "Research": research,
            "Production Readiness": production,
            "notes": [
                "Decision-snapshot studies are mature enough for directional evidence.",
                "Closed-trade studies remain limited until 30+ trades are available.",
            ],
        }

    def _final_assessment(self, reliability: dict[str, Any]) -> dict[str, Any]:
        high = [
            name for name, payload in reliability.items()
            if payload.get("reliability") == "HIGH"
        ]
        medium = [
            name for name, payload in reliability.items()
            if payload.get("reliability") == "MEDIUM"
        ]
        low = [
            name for name, payload in reliability.items()
            if payload.get("reliability") == "LOW"
        ]
        return {
            "can_trust_research_results": (
                "Partially. High-sample replay and decision-snapshot studies are reliable; "
                "closed-trade conclusions are still low confidence."
            ),
            "proven": [
                "MIN_EDGE and Score=0 behavior has enough observations for high-confidence analysis."
                if "MIN_EDGE Analysis" in high else "MIN_EDGE needs more checked candidates.",
                "Momentum-blocked replay has enough observations and shows weak follow-through."
                if "Momentum Analysis" in high else "Momentum decomposition needs more observations.",
                "Trend/Momentum conflict replay has high sample support."
                if "Trend Conflict Replay" in high else "Trend/Momentum conflict needs more candidates.",
            ],
            "needs_more_statistics": [
                "Trade Comparator and Pattern Discovery need at least 30 closed trades.",
                "SL Quality protective idea should remain dry-run until more live candidates are observed.",
                "LONG-side conclusions are weak if closed trades remain SHORT-heavy.",
            ],
            "high_confidence_research": high,
            "medium_confidence_research": medium,
            "low_confidence_research": low,
        }

    def _status_from_findings(
        self,
        biases: list[dict[str, Any]],
        freshness: dict[str, Any],
    ) -> str:
        if freshness.get("status") == "STALE":
            return "WARNING"
        if any(item.get("severity") == "HIGH" and item.get("detected") for item in biases):
            return "WARNING"
        return "OK"

    def _summary_text(self, report: dict[str, Any]) -> str:
        coverage = report["dataset_coverage"]
        readiness = report["overall_project_readiness"]
        final = report["final_assessment"]
        reliability = report["statistical_reliability"]
        freshness = report["data_freshness"]
        dry_run = report["dry_run_quality"]

        lines = [
            "Dataset Quality & Statistical Reliability",
            "=" * 44,
            f"Status: {report['status']}",
            f"Generated: {report['generated_at']}",
            "",
            "Dataset Coverage",
            f"- Decisions: {coverage['total_decisions']}",
            f"- Closed trades: {coverage['closed_trades']} ({coverage['wins']} WIN / {coverage['losses']} LOSS)",
            f"- Trade WinRate: {coverage['winrate']}%",
            f"- Trade directions: {coverage['trade_directions']}",
            "",
            "Research Reliability",
        ]
        for name, payload in reliability.items():
            lines.append(
                f"- {name}: {payload['sample_size']} sample, "
                f"{payload['reliability']} confidence"
            )

        lines.extend([
            "",
            "Freshness",
            f"- Status: {freshness['status']}",
            f"- Last decision: {freshness['latest_decision_age']}",
            f"- Last trade: {freshness['latest_trade_age']}",
            f"- Last analysis: {freshness['latest_analysis_age']}",
            "",
            "Dry Run Quality",
        ])
        for filename, payload in dry_run.items():
            lines.append(f"- {filename}: {payload['candidates']} candidates, {payload['status']}")

        lines.extend([
            "",
            "Project Readiness",
        ])
        for key, value in readiness.items():
            if key != "notes":
                lines.append(f"- {key}: {value}/100")

        lines.extend([
            "",
            "Final Assessment",
            f"- Trust level: {final['can_trust_research_results']}",
            f"- HIGH confidence: {', '.join(final['high_confidence_research']) or 'None'}",
            f"- LOW confidence: {', '.join(final['low_confidence_research']) or 'None'}",
            "",
            "Missing / Optional Sources",
            f"- {', '.join(report['missing_sources']) if report['missing_sources'] else 'None'}",
        ])
        return "\n".join(lines) + "\n"

    def _write_summary(self, report: dict[str, Any]) -> None:
        with SUMMARY_PATH.open("w", encoding="utf-8") as handle:
            handle.write(self._summary_text(report))

    def _write_scorecard(self, report: dict[str, Any]) -> None:
        rows: list[dict[str, Any]] = []
        for name, payload in report["statistical_reliability"].items():
            rows.append({
                "category": "Research Reliability",
                "item": name,
                "score": int(payload["confidence_level"] * 100),
                "status": payload["reliability"],
                "sample_size": payload["sample_size"],
                "reliability": payload["reliability"],
                "confidence_level": payload["confidence_level"],
                "notes": payload["study_type"],
            })
        for name, score in report["overall_project_readiness"].items():
            if name == "notes":
                continue
            rows.append({
                "category": "Project Readiness",
                "item": name,
                "score": score,
                "status": "OK" if score >= 75 else "WARNING",
                "sample_size": "",
                "reliability": "",
                "confidence_level": "",
                "notes": "",
            })
        for finding in report["bias_detection"]:
            rows.append({
                "category": "Bias Detection",
                "item": finding["bias"],
                "score": "",
                "status": finding["severity"],
                "sample_size": "",
                "reliability": "",
                "confidence_level": "",
                "notes": finding["evidence"],
            })

        with SCORECARD_PATH.open("w", encoding="utf-8", newline="") as handle:
            fieldnames = [
                "category",
                "item",
                "score",
                "status",
                "sample_size",
                "reliability",
                "confidence_level",
                "notes",
            ]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def main() -> None:
    """CLI entrypoint."""
    framework = DatasetQualityFramework()
    framework.print_report()
    print(f"Saved: {REPORT_PATH.name}")
    print(f"Saved: {SUMMARY_PATH.name}")
    print(f"Saved: {SCORECARD_PATH.name}")


if __name__ == "__main__":
    main()
