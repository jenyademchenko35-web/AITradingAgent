"""Read-only confidence calibration experiment.

The experiment checks whether very high confidence signals should be blocked or
downgraded when combined with suspicious features such as Momentum disagreement,
SL quality D, or SHORT-side bias. It does not modify DecisionEngine, config,
weights, live agent behavior, or any trading logic.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


BASE_DIR = Path(__file__).resolve().parent
PATTERN_REPORT_PATH = BASE_DIR / "trade_pattern_discovery_report.json"
COMPARATOR_CSV_PATH = BASE_DIR / "trade_comparator.csv"

RESULTS_CSV_PATH = BASE_DIR / "confidence_calibration_experiment_results.csv"
REPORT_JSON_PATH = BASE_DIR / "confidence_calibration_experiment_report.json"
SUMMARY_TXT_PATH = BASE_DIR / "confidence_calibration_experiment_summary.txt"

MIN_CLOSED_TRADES_FOR_RECOMMENDATION = 30
HIGH_CONFIDENCE_THRESHOLD = 90.0


TradeProfile = dict[str, Any]
Predicate = Callable[[TradeProfile], bool]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def _profit_factor(trades: list[TradeProfile]) -> float:
    gross_profit = sum(max(_safe_float(trade.get("pnl")), 0.0) for trade in trades)
    gross_loss = abs(sum(min(_safe_float(trade.get("pnl")), 0.0) for trade in trades))
    if gross_loss == 0:
        return round(gross_profit, 4) if gross_profit else 0.0
    return round(gross_profit / gross_loss, 4)


def _metrics(trades: list[TradeProfile]) -> dict[str, Any]:
    wins = [trade for trade in trades if str(trade.get("result", "")).upper() == "WIN"]
    losses = [trade for trade in trades if str(trade.get("result", "")).upper() == "LOSS"]
    net_pnl = round(sum(_safe_float(trade.get("pnl")) for trade in trades), 8)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round((len(wins) / len(trades)) * 100, 2) if trades else 0.0,
        "profit_factor": _profit_factor(trades),
        "net_pnl": net_pnl,
        "average_pnl": round(net_pnl / len(trades), 8) if trades else 0.0,
    }


def _normalize_profile(raw: dict[str, Any]) -> TradeProfile:
    features = raw.get("features", [])
    if isinstance(features, str):
        features = [part.strip() for part in features.split(";") if part.strip()]
    if not isinstance(features, list):
        features = []

    loss_reasons = raw.get("loss_reasons", [])
    if isinstance(loss_reasons, str):
        loss_reasons = [
            part.strip()
            for separator in (";", "|")
            for part in loss_reasons.split(separator)
            if part.strip()
        ]
    if not isinstance(loss_reasons, list):
        loss_reasons = []

    return {
        "symbol": raw.get("symbol", ""),
        "direction": str(raw.get("direction", "")).upper(),
        "result": str(raw.get("result", "")).upper(),
        "pnl": _safe_float(raw.get("pnl")),
        "decision": raw.get("decision", raw.get("signal", "")),
        "quality": raw.get("quality", "UNKNOWN"),
        "score": _safe_float(raw.get("score")),
        "confidence": _safe_float(raw.get("confidence")),
        "weighted_score": _safe_float(raw.get("weighted_score")),
        "potential_score": _safe_float(raw.get("potential_score")),
        "trend": raw.get("trend", raw.get("trend_status", "UNKNOWN")),
        "structure": raw.get("structure", raw.get("structure_status", "UNKNOWN")),
        "momentum": raw.get("momentum", raw.get("momentum_status", "UNKNOWN")),
        "risk": raw.get("risk", raw.get("risk_status", "UNKNOWN")),
        "entry_quality": raw.get("entry_quality", "UNKNOWN"),
        "sl_quality": raw.get("sl_quality", _extract_feature_value(features, "sl_quality")),
        "momentum_disagreement": bool(raw.get("momentum_disagreement"))
        or "Momentum disagreement" in loss_reasons
        or "momentum_disagreement" in features,
        "local_trend": raw.get("local_trend", "UNKNOWN"),
        "age_minutes": _safe_float(raw.get("age_minutes", raw.get("signal_age_minutes"))),
        "loss_reasons": loss_reasons,
        "features": features,
    }


def _extract_feature_value(features: list[str], prefix: str) -> str:
    marker = f"{prefix}="
    for feature in features:
        if feature.startswith(marker):
            return feature.split("=", 1)[1]
    return "UNKNOWN"


def _load_profiles() -> tuple[list[TradeProfile], str]:
    report = _load_json(PATTERN_REPORT_PATH)
    profiles = report.get("profiles", [])
    if isinstance(profiles, list) and profiles:
        return [_normalize_profile(profile) for profile in profiles], str(PATTERN_REPORT_PATH.name)

    rows = _read_csv(COMPARATOR_CSV_PATH)
    if rows:
        return [_normalize_profile(row) for row in rows], str(COMPARATOR_CSV_PATH.name)
    return [], "none"


def _is_high_confidence(trade: TradeProfile) -> bool:
    return _safe_float(trade.get("confidence")) > HIGH_CONFIDENCE_THRESHOLD


def _has_momentum_disagreement(trade: TradeProfile) -> bool:
    return bool(trade.get("momentum_disagreement")) or trade.get("momentum") == "FAIL"


def _has_sl_quality_d(trade: TradeProfile) -> bool:
    return str(trade.get("sl_quality", "")).upper() == "D"


def _has_short_bias_feature(trade: TradeProfile) -> bool:
    return str(trade.get("direction", "")).upper() == "SHORT"


def _scenario_predicates() -> dict[str, tuple[str, Predicate]]:
    return {
        "block_conf_gt90": (
            "Block all trades with Confidence > 90.",
            lambda trade: _is_high_confidence(trade),
        ),
        "block_conf_gt90_momentum_disagreement": (
            "Block Confidence > 90 when Momentum disagreement is present.",
            lambda trade: _is_high_confidence(trade) and _has_momentum_disagreement(trade),
        ),
        "block_conf_gt90_sl_quality_D": (
            "Block Confidence > 90 when SL quality is D.",
            lambda trade: _is_high_confidence(trade) and _has_sl_quality_d(trade),
        ),
        "block_conf_gt90_short_bias": (
            "Block Confidence > 90 when direction is SHORT.",
            lambda trade: _is_high_confidence(trade) and _has_short_bias_feature(trade),
        ),
        "block_conf_gt90_momentum_or_sl_D": (
            "Block Confidence > 90 when Momentum disagrees or SL quality is D.",
            lambda trade: _is_high_confidence(trade)
            and (_has_momentum_disagreement(trade) or _has_sl_quality_d(trade)),
        ),
        "block_conf_gt90_short_momentum": (
            "Block Confidence > 90 for SHORT trades with Momentum disagreement.",
            lambda trade: _is_high_confidence(trade)
            and _has_short_bias_feature(trade)
            and _has_momentum_disagreement(trade),
        ),
        "block_conf_gt90_short_sl_quality_D": (
            "Block Confidence > 90 for SHORT trades with SL quality D.",
            lambda trade: _is_high_confidence(trade)
            and _has_short_bias_feature(trade)
            and _has_sl_quality_d(trade),
        ),
        "block_conf_gt90_any_risk_feature": (
            "Block Confidence > 90 with Momentum disagreement, SL quality D, or SHORT direction.",
            lambda trade: _is_high_confidence(trade)
            and (
                _has_momentum_disagreement(trade)
                or _has_sl_quality_d(trade)
                or _has_short_bias_feature(trade)
            ),
        ),
    }


def _simulate_block(
    trades: list[TradeProfile],
    predicate: Predicate,
    description: str,
    scenario: str,
) -> dict[str, Any]:
    prevented = [trade for trade in trades if predicate(trade)]
    remaining = [trade for trade in trades if not predicate(trade)]
    prevented_losses = [
        trade for trade in prevented
        if str(trade.get("result", "")).upper() == "LOSS"
    ]
    lost_wins = [
        trade for trade in prevented
        if str(trade.get("result", "")).upper() == "WIN"
    ]
    payload = _metrics(remaining)
    payload.update({
        "scenario": scenario,
        "mode": "BLOCK",
        "description": description,
        "prevented_trades": len(prevented),
        "prevented_losses": len(prevented_losses),
        "lost_wins": len(lost_wins),
        "prevented_loss_pnl": round(sum(_safe_float(trade.get("pnl")) for trade in prevented_losses), 8),
        "lost_win_pnl": round(sum(_safe_float(trade.get("pnl")) for trade in lost_wins), 8),
        "prevented_symbols": _counter(prevented, "symbol"),
        "prevented_directions": _counter(prevented, "direction"),
    })
    return payload


def _simulate_downgrade(
    trades: list[TradeProfile],
    predicate: Predicate,
    description: str,
    scenario: str,
) -> dict[str, Any]:
    """Simulate lowering only SETUP signals to WATCH.

    HIGH PRIORITY would become SETUP and remain tradable in this conservative
    closed-trade approximation. SETUP becomes WATCH and is treated as prevented.
    """
    def prevented_by_downgrade(trade: TradeProfile) -> bool:
        return predicate(trade) and str(trade.get("decision", "")).upper() == "SETUP"

    payload = _simulate_block(trades, prevented_by_downgrade, description, scenario)
    payload["mode"] = "DOWNGRADE"
    return payload


def _counter(trades: list[TradeProfile], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trade in trades:
        key = str(trade.get(field, "") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _rank_scenarios(
    baseline: dict[str, Any],
    scenarios: list[dict[str, Any]],
    sample_size: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    baseline_pf = _safe_float(baseline.get("profit_factor"))
    baseline_wr = _safe_float(baseline.get("winrate"))
    candidates = []
    for scenario in scenarios:
        if scenario["scenario"] == "baseline":
            continue
        if scenario["prevented_losses"] <= scenario["lost_wins"]:
            continue
        if _safe_float(scenario["profit_factor"]) < baseline_pf:
            continue
        if _safe_float(scenario["winrate"]) < max(0.0, baseline_wr - 3.0):
            continue
        candidates.append(scenario)

    if candidates:
        best = max(
            candidates,
            key=lambda item: (
                _safe_float(item["profit_factor"]),
                _safe_float(item["net_pnl"]),
                item["prevented_losses"] - item["lost_wins"],
            ),
        )
    else:
        best = {}

    if sample_size < MIN_CLOSED_TRADES_FOR_RECOMMENDATION:
        recommendation = {
            "status": "INSUFFICIENT_DATA",
            "reason": (
                f"Only {sample_size} closed trades are available; "
                f"minimum is {MIN_CLOSED_TRADES_FOR_RECOMMENDATION}."
            ),
            "best_candidate": best.get("scenario") if best else None,
            "apply_automatically": False,
        }
    elif best:
        recommendation = {
            "status": "CANDIDATE_FOR_DRY_RUN",
            "reason": "Best scenario improved PF/WinRate and prevented more losses than wins.",
            "best_candidate": best["scenario"],
            "apply_automatically": False,
        }
    else:
        recommendation = {
            "status": "NO_RELIABLE_CANDIDATE",
            "reason": "No scenario passed safety criteria.",
            "best_candidate": None,
            "apply_automatically": False,
        }
    return best, recommendation


def build_report() -> dict[str, Any]:
    """Build the confidence calibration experiment report."""
    trades, source = _load_profiles()
    baseline = _metrics(trades)
    baseline.update({
        "scenario": "baseline",
        "mode": "BASELINE",
        "description": "Current closed-trade baseline.",
        "prevented_trades": 0,
        "prevented_losses": 0,
        "lost_wins": 0,
        "prevented_loss_pnl": 0.0,
        "lost_win_pnl": 0.0,
    })

    scenarios = [baseline]
    for name, (description, predicate) in _scenario_predicates().items():
        scenarios.append(_simulate_block(trades, predicate, description, name))
        scenarios.append(_simulate_downgrade(trades, predicate, description, f"downgrade_{name}"))

    for scenario in scenarios:
        scenario["net_pnl_delta"] = round(_safe_float(scenario["net_pnl"]) - _safe_float(baseline["net_pnl"]), 8)
        scenario["profit_factor_delta"] = round(_safe_float(scenario["profit_factor"]) - _safe_float(baseline["profit_factor"]), 4)
        scenario["winrate_delta"] = round(_safe_float(scenario["winrate"]) - _safe_float(baseline["winrate"]), 2)

    best, recommendation = _rank_scenarios(baseline, scenarios, len(trades))
    high_conf_trades = [trade for trade in trades if _is_high_confidence(trade)]

    report = {
        "generated_at": _utc_now(),
        "status": "OK" if trades else "NO_DATA",
        "source": source,
        "rules": {
            "high_confidence_threshold": HIGH_CONFIDENCE_THRESHOLD,
            "minimum_closed_trades_for_recommendation": MIN_CLOSED_TRADES_FOR_RECOMMENDATION,
            "mode": "read-only historical closed-trade replay",
            "apply_automatically": False,
        },
        "sample": {
            "closed_trades": len(trades),
            "high_confidence_trades": len(high_conf_trades),
            "high_confidence_losses": sum(1 for trade in high_conf_trades if trade["result"] == "LOSS"),
            "high_confidence_wins": sum(1 for trade in high_conf_trades if trade["result"] == "WIN"),
        },
        "baseline": baseline,
        "scenarios": {scenario["scenario"]: scenario for scenario in scenarios},
        "best_candidate": best,
        "recommendation": recommendation,
        "warnings": [
            "This experiment is read-only and uses closed historical trades only.",
            "Do not change DecisionEngine from this report while sample size is below 30 closed trades.",
        ],
    }
    _write_outputs(report, scenarios)
    return report


def _write_outputs(report: dict[str, Any], scenarios: list[dict[str, Any]]) -> None:
    with REPORT_JSON_PATH.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)

    with RESULTS_CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "scenario",
            "mode",
            "trades",
            "wins",
            "losses",
            "winrate",
            "profit_factor",
            "net_pnl",
            "average_pnl",
            "prevented_trades",
            "prevented_losses",
            "lost_wins",
            "net_pnl_delta",
            "profit_factor_delta",
            "winrate_delta",
            "description",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for scenario in scenarios:
            writer.writerow({key: scenario.get(key, "") for key in fieldnames})

    with SUMMARY_TXT_PATH.open("w", encoding="utf-8") as handle:
        handle.write(_summary_text(report))


def _summary_text(report: dict[str, Any]) -> str:
    baseline = report["baseline"]
    best = report.get("best_candidate") or {}
    recommendation = report["recommendation"]
    sample = report["sample"]

    lines = [
        "Confidence Calibration Experiment",
        "=================================",
        f"Generated: {report['generated_at']}",
        f"Status: {report['status']}",
        f"Source: {report['source']}",
        "",
        "Sample:",
        f"- Closed trades: {sample['closed_trades']}",
        f"- Confidence > 90 trades: {sample['high_confidence_trades']}",
        f"- High-confidence WIN/LOSS: {sample['high_confidence_wins']} / {sample['high_confidence_losses']}",
        "",
        "Baseline:",
        (
            f"- Trades: {baseline['trades']} | WIN: {baseline['wins']} | "
            f"LOSS: {baseline['losses']} | WR: {baseline['winrate']}% | "
            f"PF: {baseline['profit_factor']} | Net PnL: {baseline['net_pnl']}"
        ),
        "",
        "Best candidate:",
    ]
    if best:
        lines.extend([
            f"- Scenario: {best['scenario']}",
            f"- Mode: {best['mode']}",
            f"- Trades after filter: {best['trades']}",
            f"- WR: {best['winrate']}% | PF: {best['profit_factor']} | Net PnL: {best['net_pnl']}",
            f"- Prevented LOSS: {best['prevented_losses']}",
            f"- Lost WIN: {best['lost_wins']}",
            f"- Net PnL delta: {best['net_pnl_delta']}",
        ])
    else:
        lines.append("- None")

    lines.extend([
        "",
        "Recommendation:",
        f"- Status: {recommendation['status']}",
        f"- Reason: {recommendation['reason']}",
        f"- Apply automatically: {recommendation['apply_automatically']}",
        "",
        "Important:",
        "- DecisionEngine, config, weights, and live agent were not changed.",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    """Run the experiment from CLI."""
    report = build_report()
    print(_summary_text(report))
    print(f"Saved: {REPORT_JSON_PATH.name}")
    print(f"Saved: {RESULTS_CSV_PATH.name}")
    print(f"Saved: {SUMMARY_TXT_PATH.name}")


if __name__ == "__main__":
    main()
