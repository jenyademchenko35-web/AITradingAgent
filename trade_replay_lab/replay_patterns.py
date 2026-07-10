"""Verdict and recurring-pattern logic for Trade Replay Lab."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping

from market_intelligence_utils import safe_float
from trade_replay_lab.replay_metrics import percent, rounded_mean, wilson_lower_bound


REASON_LABELS = {
    "STOP_TOO_TIGHT": "Возможный слишком тесный Stop Loss",
    "LATE_ENTRY": "Поздний вход",
    "MOMENTUM_EXHAUSTION": "Momentum Exhaustion",
    "IMMEDIATE_REVERSAL": "Разворот сразу после входа",
    "AGAINST_TREND": "Вход против старшего Trend",
    "NEWS_CONFLICT": "NEWS_CONFLICT",
    "STRUCTURE_WEAK": "Structure Weak",
    "MOMENTUM_FAIL": "Momentum FAIL",
    "RISK_WEAK": "Risk Weak",
    "FALSE_BREAKOUT": "Ложный пробой",
    "TREND_CONTINUATION": "Продолжение Trend",
    "MOMENTUM_CONFIRMATION": "Momentum подтверждён",
    "GOOD_RISK_REWARD": "Рабочий Risk/Reward",
    "NEWS_SUPPORTIVE": "NEWS_SUPPORTIVE",
    "UNCLASSIFIED_LOSS": "LOSS без устойчивого паттерна",
    "UNCLASSIFIED_WIN": "WIN без устойчивого паттерна",
}


def build_verdict(replay: Mapping[str, Any]) -> dict[str, Any]:
    """Build an evidence-ordered explanation for one replayed trade."""
    result = str(replay.get("result", ""))
    path = replay.get("scenarios", {}).get("path", {})
    trend = replay.get("trend", {})
    engines = replay.get("engines", {})
    news = replay.get("news", {})
    improvements = replay.get("improvements", [])
    reasons: list[str] = []

    if result == "LOSS":
        if any(item.get("family") == "ATR_STOP" for item in improvements):
            reasons.append("STOP_TOO_TIGHT")
        if path.get("late_entry"):
            reasons.append("LATE_ENTRY")
        if path.get("momentum_phase") == "END":
            reasons.append("MOMENTUM_EXHAUSTION")
        if path.get("immediate_reversal"):
            reasons.append("IMMEDIATE_REVERSAL")
        if trend.get("alignment") == "AGAINST_TREND":
            reasons.append("AGAINST_TREND")
        if news.get("status") == "NEWS_CONFLICT":
            reasons.append("NEWS_CONFLICT")
        if str(engines.get("structure", "")).upper() == "FAIL":
            reasons.append("STRUCTURE_WEAK")
        if str(engines.get("momentum", "")).upper() == "FAIL":
            reasons.append("MOMENTUM_FAIL")
        if str(engines.get("risk", "")).upper() == "FAIL":
            reasons.append("RISK_WEAK")
        if path.get("false_breakout"):
            reasons.append("FALSE_BREAKOUT")
        if not reasons:
            reasons.append("UNCLASSIFIED_LOSS")
    else:
        if trend.get("alignment") == "WITH_TREND":
            reasons.append("TREND_CONTINUATION")
        if str(engines.get("momentum", "")).upper() == "PASS":
            reasons.append("MOMENTUM_CONFIRMATION")
        if news.get("status") == "NEWS_SUPPORTIVE":
            reasons.append("NEWS_SUPPORTIVE")
        if safe_float(replay.get("baseline_r")) >= 1:
            reasons.append("GOOD_RISK_REWARD")
        if not reasons:
            reasons.append("UNCLASSIFIED_WIN")

    unique = list(dict.fromkeys(reasons))
    return {
        "main_reason": unique[0],
        "main_reason_label": REASON_LABELS.get(unique[0], unique[0]),
        "reasons": unique,
        "reason_labels": [REASON_LABELS.get(reason, reason) for reason in unique],
    }


def aggregate_patterns(replays: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate verdict reasons into replay_patterns.csv rows."""
    rows = list(replays)
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for replay in rows:
        for reason in replay.get("verdict", {}).get("reasons", []):
            grouped[str(reason)].append(replay)
    patterns = []
    for reason, items in grouped.items():
        wins = sum(1 for item in items if item.get("result") == "WIN")
        losses = sum(1 for item in items if item.get("result") == "LOSS")
        patterns.append({
            "reason": reason,
            "reason_label": REASON_LABELS.get(reason, reason),
            "count": len(items),
            "wins": wins,
            "losses": losses,
            "improvement": rounded_mean(
                safe_float(item.get("improvement_score")) for item in items
            ),
            "confidence": wilson_lower_bound(max(wins, losses), len(items)),
            "support_pct": percent(len(items), len(rows)),
        })
    return sorted(
        patterns,
        key=lambda row: (row["count"], row["confidence"]),
        reverse=True,
    )


def hypothesis_summary(replays: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize which scenario families helped or had no measured benefit."""
    rows = list(replays)
    helped = Counter()
    helped_families = Counter()
    available = Counter()
    family_names = {
        "ATR_STOP": "ATR Stop",
        "TAKE_PROFIT": "Take Profit",
        "NO_TP": "Без Take Profit",
        "TRAILING": "Trailing",
        "NEWS_VETO": "Новости",
        "MOMENTUM_FILTER": "Momentum Filter",
        "TREND_ALIGNMENT": "Trend Alignment",
    }
    for replay in rows:
        scenarios = replay.get("scenarios", {})
        for item in scenarios.get("atr_stop_variants", []):
            if item.get("available"):
                available["ATR Stop"] += 1
        for item in scenarios.get("take_profit_variants", []):
            if item.get("available"):
                available["Take Profit"] += 1
        if scenarios.get("no_take_profit", {}).get("available"):
            available["Без Take Profit"] += 1
        if scenarios.get("trailing_stop", {}).get("available"):
            available["Trailing"] += 1
        if replay.get("news", {}).get("available"):
            available["Новости"] += 1
        if replay.get("decision_available"):
            available["Confidence/Quality"] += 1
        for item in replay.get("improvements", []):
            helped[str(item.get("label") or item.get("family"))] += 1
            family = str(item.get("family", ""))
            if family:
                helped_families[family_names.get(family, family)] += 1
    no_help = [
        {"hypothesis": name, "observations": count}
        for name, count in available.items()
        if count > 0 and helped_families.get(name, 0) == 0
    ]
    return {
        "top_improvements": [
            {"name": name, "count": count}
            for name, count in helped.most_common(10)
        ],
        "no_measured_benefit": sorted(
            no_help, key=lambda item: item["observations"], reverse=True
        )[:10],
        "availability": dict(available),
    }
