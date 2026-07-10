"""Evidence extraction rules for Research Consensus hypotheses."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any, Mapping

from research_consensus.consensus_metrics import safe_float


@dataclass(frozen=True)
class Hypothesis:
    """One canonical hypothesis evaluated across research modules."""

    key: str
    label: str
    group: str
    variant: str = ""


HYPOTHESES = [
    Hypothesis("momentum_filter", "Momentum Filter", "momentum"),
    Hypothesis("edge_18", "Edge >= 18", "edge", "18"),
    Hypothesis("edge_20", "Edge >= 20", "edge", "20"),
    Hypothesis("edge_22", "Edge >= 22", "edge", "22"),
    Hypothesis("atr_1", "ATR 1", "atr", "1"),
    Hypothesis("atr_1_25", "ATR 1.25", "atr", "1.25"),
    Hypothesis("atr_1_5", "ATR 1.5", "atr", "1.5"),
    Hypothesis("atr_2", "ATR 2", "atr", "2"),
    Hypothesis("trend_alignment", "Trend Alignment", "trend"),
    Hypothesis("news_filter", "News Filter", "news"),
    Hypothesis("cooldown", "Cooldown", "cooldown"),
    Hypothesis("duplicate_symbol", "Duplicate Symbol", "duplicate"),
    Hypothesis("volatility_filter", "Volatility Filter", "volatility"),
]


def evidence(
    module: str,
    stance: str,
    sample_size: int,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one normalized evidence item."""
    return {
        "module": module,
        "stance": stance,
        "sample_size": max(0, int(sample_size)),
        "reason": reason,
        "details": dict(details or {}),
    }


def collect_evidence(
    hypothesis: Hypothesis,
    reports: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collect only relevant primary evidence for one hypothesis."""
    items = []
    for extractor in (
        strategy_lab_evidence,
        replay_evidence,
        loss_evidence,
        memory_evidence,
        news_evidence,
        regime_evidence,
    ):
        item = extractor(hypothesis, reports)
        if item:
            items.append(item)
    return items


def strategy_lab_evidence(
    hypothesis: Hypothesis,
    reports: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Evaluate Strategy Lab v2, falling back to v1 without double counting."""
    lab = reports.get("hypothesis_lab", {})
    metrics = [row for row in lab.get("metrics", []) if isinstance(row, Mapping)]
    baseline = lab.get("baseline", {}) if isinstance(lab.get("baseline"), Mapping) else {}
    module_name = "Strategy Lab v2"
    if not metrics:
        lab = reports.get("strategy_lab", {})
        metrics = [row for row in lab.get("metrics", []) if isinstance(row, Mapping)]
        baseline = next(
            (row for row in metrics if row.get("strategy") == "Current"),
            {},
        )
        module_name = "Strategy Lab v1"
    if not metrics or not baseline:
        return None

    candidates = strategy_candidates(hypothesis, metrics)
    if not candidates:
        return None
    if hypothesis.group in {"cooldown", "volatility"}:
        selected = max(
            candidates,
            key=lambda row: (
                safe_float(row.get("net_benefit")),
                safe_float(row.get("profit_factor")),
                safe_float(row.get("expectancy")),
            ),
        )
    else:
        selected = candidates[0]

    baseline_pf = safe_float(baseline.get("profit_factor"))
    baseline_expectancy = safe_float(baseline.get("expectancy"))
    pf = safe_float(selected.get("profit_factor"))
    expectancy = safe_float(selected.get("expectancy"))
    net_benefit = safe_float(selected.get("net_benefit"))
    lost_winners = int(safe_float(selected.get("lost_winners")))
    saved_losses = int(safe_float(selected.get("saved_losses")))
    kept_trades = int(safe_float(selected.get("trades")))
    opportunities = int(
        safe_float(selected.get("opportunities"), safe_float(baseline.get("trades")))
    )
    label = str(selected.get("hypothesis") or selected.get("strategy") or hypothesis.label)

    if (
        hypothesis.group == "momentum"
        and kept_trades > 0
        and safe_float(selected.get("wins")) == 0
        and lost_winners > 0
    ):
        stance = "OPPOSE"
        reason = (
            "Фильтр сохранил только убыточные сделки и потерял WIN; "
            "Net Benefit по пропускам не компенсирует слабость оставшейся выборки."
        )
    elif kept_trades < 3:
        stance = "INSUFFICIENT"
        reason = f"После фильтра осталось только {kept_trades} сделки: пороговая выборка слишком мала."
    elif pf >= baseline_pf + 0.05 and expectancy >= baseline_expectancy + 0.05:
        stance = "SUPPORT"
        reason = "Profit Factor и expectancy лучше baseline."
    elif pf <= baseline_pf - 0.05 or expectancy <= baseline_expectancy - 0.1:
        stance = "OPPOSE"
        reason = "Profit Factor или expectancy хуже baseline."
    elif net_benefit > 0 and lost_winners == 0 and pf >= baseline_pf:
        stance = "SUPPORT"
        reason = "Есть положительный Net Benefit без потерянных WIN и без ухудшения PF."
    elif net_benefit > 0 and saved_losses > lost_winners:
        stance = "NEUTRAL"
        reason = "LOSS сохранены, но итоговые метрики не дают устойчивого улучшения."
    else:
        stance = "NEUTRAL"
        reason = "Результат практически совпадает с baseline."
    return evidence(
        module_name,
        stance,
        opportunities,
        reason,
        {
            "variant": label,
            "kept_trades": kept_trades,
            "profit_factor": pf,
            "baseline_profit_factor": baseline_pf,
            "expectancy": expectancy,
            "baseline_expectancy": baseline_expectancy,
            "saved_losses": saved_losses,
            "lost_winners": lost_winners,
            "net_benefit": net_benefit,
        },
    )


def strategy_candidates(
    hypothesis: Hypothesis,
    metrics: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Find Strategy Lab rows corresponding to a canonical hypothesis."""
    exact_names = {
        "momentum_filter": {"Momentum Confirmation", "Momentum+"},
        "edge_18": {"Edge >= 18"},
        "edge_20": {"Edge >= 20"},
        "edge_22": {"Edge >= 22"},
        "atr_1": {"ATR Stop 1", "ATR 1"},
        "atr_1_25": {"ATR Stop 1.25", "ATR 1.25"},
        "atr_1_5": {"ATR Stop 1.5", "ATR 1.5"},
        "atr_2": {"ATR Stop 2", "ATR 2"},
        "trend_alignment": {"Trend Alignment"},
        "news_filter": {"News Veto", "News Filter"},
        "duplicate_symbol": {"Duplicate Symbol Filter"},
    }
    if hypothesis.group == "cooldown":
        return [row for row in metrics if row.get("group") == "cooldown"]
    if hypothesis.group == "volatility":
        return [row for row in metrics if row.get("group") == "volatility"]
    names = exact_names.get(hypothesis.key, set())
    return [
        row for row in metrics
        if str(row.get("hypothesis") or row.get("strategy")) in names
    ]


def replay_evidence(
    hypothesis: Hypothesis,
    reports: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Extract direct findings from Trade Replay Lab."""
    report = reports.get("trade_replay", {})
    if not report:
        return None
    closed = int(safe_float(report.get("sample", {}).get("closed_trades")))
    summary = report.get("summary", {})
    improvements = {
        str(row.get("name")): int(safe_float(row.get("count")))
        for row in summary.get("top_improvements", [])
        if isinstance(row, Mapping)
    }
    direct_improvements = [
        item
        for trade in report.get("trades", [])
        if isinstance(trade, Mapping)
        for item in trade.get("improvements", [])
        if isinstance(item, Mapping)
    ]
    patterns = {
        str(row.get("reason")): row
        for row in report.get("patterns", [])
        if isinstance(row, Mapping)
    }
    if hypothesis.group == "momentum":
        direct_count = sum(
            1 for item in direct_improvements
            if item.get("family") == "MOMENTUM_FILTER"
        )
        count = max(improvements.get("Momentum Filter", 0), direct_count)
        pattern = patterns.get("MOMENTUM_FAIL", {})
        relevant = max(count, int(safe_float(pattern.get("count"))))
        stance = "SUPPORT" if relevant >= 5 else "INSUFFICIENT"
        return evidence(
            "Trade Replay",
            stance,
            relevant,
            f"Momentum Filter улучшал replay в {count} случаях; Momentum FAIL найден в {pattern.get('count', 0)} сделках.",
            {"improved_cases": count, "pattern": pattern},
        )
    if hypothesis.group == "atr":
        name = f"ATR {hypothesis.variant}"
        direct_count = sum(
            1 for item in direct_improvements
            if item.get("family") == "ATR_STOP" and item.get("label") == name
        )
        count = max(improvements.get(name, 0), direct_count)
        available = int(round(closed * safe_float(report.get("sample", {}).get("ohlcv_coverage")) / 100))
        threshold = max(3, ceil(available * 0.15)) if available else 3
        stance = "SUPPORT" if count >= threshold else "NEUTRAL"
        return evidence(
            "Trade Replay",
            stance,
            available,
            f"{name} был лучшим улучшением в {count} из {available} OHLCV-покрытых сделок.",
            {"variant": name, "improved_cases": count, "available_cases": available},
        )
    if hypothesis.group == "trend":
        pattern = patterns.get("TREND_CONTINUATION", {})
        count = int(safe_float(pattern.get("count")))
        stance = "SUPPORT" if count >= 3 and int(safe_float(pattern.get("losses"))) == 0 else "NEUTRAL"
        return evidence(
            "Trade Replay",
            stance,
            count,
            f"Trend Continuation встречался в {count} WIN и {pattern.get('losses', 0)} LOSS.",
            {"pattern": pattern},
        )
    if hypothesis.group == "news":
        direct_count = sum(
            1 for item in direct_improvements
            if item.get("family") == "NEWS_VETO"
        )
        count = max(improvements.get("News Veto", 0), direct_count)
        available = int(safe_float(summary.get("availability", {}).get("Новости")))
        stance = "SUPPORT" if available >= 5 and count > 0 else "INSUFFICIENT"
        return evidence(
            "Trade Replay",
            stance,
            available,
            f"News Veto помог в {count} случае при {available} доступном news-контексте.",
            {"improved_cases": count, "available_cases": available},
        )
    return None


def loss_evidence(
    hypothesis: Hypothesis,
    reports: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Extract loss-pattern evidence without extrapolating exact variants."""
    report = reports.get("trade_loss", {})
    if not report:
        return None
    sample = report.get("sample", {})
    losses = int(safe_float(sample.get("loss_trades")))
    patterns = {
        str(row.get("pattern")): row
        for row in report.get("patterns", [])
        if isinstance(row, Mapping)
    }
    if hypothesis.group == "momentum":
        pattern = patterns.get("momentum_fail", {})
        count = int(safe_float(pattern.get("loss_count")))
        return evidence(
            "Trade Loss Analyzer",
            "SUPPORT" if count >= 5 else "INSUFFICIENT",
            losses,
            f"Momentum FAIL присутствует в {count} из {losses} LOSS.",
            {"pattern": pattern},
        )
    if hypothesis.group == "edge":
        stats = report.get("quality_edge_confidence", {}).get("edge_gt_18", {})
        trades = int(safe_float(stats.get("trades")))
        if hypothesis.variant == "18":
            stance = "SUPPORT" if trades >= 5 and safe_float(stats.get("profit_factor")) > 0.4 else "INSUFFICIENT"
            reason = "Группа Edge >18 лучше общей выборки, но остаётся убыточной."
        elif hypothesis.variant == "20":
            stance = "NEUTRAL"
            reason = "Есть только широкая группа Edge >18; точный порог 20 не изолирован."
        else:
            stance = "INSUFFICIENT"
            reason = "Loss Analyzer не содержит отдельной выборки для Edge >=22."
        return evidence("Trade Loss Analyzer", stance, trades, reason, {"edge_gt_18": stats})
    if hypothesis.group == "atr":
        check = report.get("post_sl_tp_check", {}).get("24h", {})
        checked = int(safe_float(check.get("checked")))
        return evidence(
            "Trade Loss Analyzer",
            "NEUTRAL" if checked else "INSUFFICIENT",
            checked,
            "Часть сделок достигала TP после SL, но точный ATR-множитель не изолирован.",
            {"post_sl_24h": check},
        )
    return None


def memory_evidence(
    hypothesis: Hypothesis,
    reports: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Use Trade Memory only when its current target matches the hypothesis."""
    if hypothesis.group != "momentum":
        return None
    report = reports.get("trade_memory", {})
    if not report:
        return None
    target = report.get("target", {})
    stats = report.get("stats", {})
    matches = int(safe_float(report.get("matches_count")))
    if str(target.get("momentum", "")).upper() != "FAIL":
        return evidence(
            "Trade Memory",
            "NEUTRAL",
            matches,
            "Текущая memory-цель не относится к Momentum FAIL.",
        )
    pf = safe_float(stats.get("profit_factor"))
    stance = "SUPPORT" if matches >= 5 and pf < 1 else "INSUFFICIENT"
    return evidence(
        "Trade Memory",
        stance,
        matches,
        f"Похожие Momentum FAIL ситуации: {matches}, PF {pf}, Winrate {stats.get('winrate', 0)}%.",
        {"target": target, "stats": stats},
    )


def news_evidence(
    hypothesis: Hypothesis,
    reports: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Evaluate News Filter from the dedicated news statistics report."""
    if hypothesis.group != "news":
        return None
    report = reports.get("news_statistics", {})
    if not report:
        return None
    conflict = next(
        (
            row for row in report.get("groups", [])
            if row.get("group_type") == "news_status" and row.get("group") == "NEWS_CONFLICT"
        ),
        {},
    )
    trades = int(safe_float(conflict.get("trades")))
    stance = (
        "SUPPORT"
        if trades >= 5 and safe_float(conflict.get("loss_rate")) >= 70
        else "INSUFFICIENT"
    )
    return evidence(
        "News Statistics",
        stance,
        trades,
        f"NEWS_CONFLICT: {trades} сделок, LOSS rate {conflict.get('loss_rate', 0)}%.",
        {"group": conflict},
    )


def regime_evidence(
    hypothesis: Hypothesis,
    reports: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Use regime report as broad context for Trend/Volatility hypotheses."""
    report = reports.get("market_regime", {})
    if not report or hypothesis.group not in {"trend", "volatility"}:
        return None
    regimes = report.get("trades_by_regime", {})
    total = sum(
        int(safe_float(row.get("trades")))
        for row in regimes.values()
        if isinstance(row, Mapping)
    )
    if hypothesis.group == "trend":
        return evidence(
            "Market Regime Advisor",
            "NEUTRAL" if total else "INSUFFICIENT",
            total,
            "Результаты различаются по режимам, но exact 1H/4H/1D alignment не изолирован.",
            {"trades_by_regime": regimes},
        )
    high_vol = regimes.get("HIGH_VOLATILITY", {})
    high_vol_trades = int(safe_float(high_vol.get("trades")))
    return evidence(
        "Market Regime Advisor",
        "NEUTRAL" if high_vol_trades >= 5 else "INSUFFICIENT",
        total,
        f"HIGH_VOLATILITY содержит {high_vol_trades} сделок; точный volatility filter не подтверждён.",
        {"trades_by_regime": regimes},
    )
