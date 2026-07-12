"""Report writers for Strategy Lab v2 hypothesis testing."""

from __future__ import annotations

from typing import Any, Mapping

from market_intelligence_utils import BASE_DIR, write_csv, write_json
from strategy_lab.hypothesis_metrics import HYPOTHESIS_FIELDS


HYPOTHESIS_REPORT_JSON = BASE_DIR / "hypothesis_report.json"
HYPOTHESIS_SUMMARY_TXT = BASE_DIR / "hypothesis_summary.txt"
HYPOTHESIS_COMPARISON_CSV = BASE_DIR / "hypothesis_comparison.csv"
HYPOTHESIS_SHADOW_TRADES_CSV = BASE_DIR / "hypothesis_shadow_trades.csv"

HYPOTHESIS_SHADOW_FIELDS = [
    "timestamp",
    "hypothesis",
    "group",
    "symbol",
    "direction",
    "result",
    "r",
    "rr",
    "duration",
    "reason",
    "baseline_result",
    "confidence",
    "score",
    "edge",
    "quality",
    "news_status",
    "momentum",
    "atr_pct",
]


def _number(value: Any) -> float:
    """Convert report values to a number for deterministic ranking."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _baseline(metrics: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the baseline row from a metrics collection."""
    for row in metrics:
        if row.get("hypothesis") == "baseline":
            return dict(row)
    return {}


def is_overfiltered(row: Mapping[str, Any]) -> bool:
    """Return True when a hypothesis removed every observed trade."""
    return (
        int(_number(row.get("trades"))) == 0
        and int(_number(row.get("wins"))) == 0
        and int(_number(row.get("lost_winners"))) > 0
    )


def is_leader_eligible(
    row: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> bool:
    """Apply the minimum evidence gates required for Strategy Lab leadership."""
    baseline_trades = int(_number(baseline.get("trades")))
    baseline_wins = int(_number(baseline.get("wins")))
    return (
        baseline_trades >= 30
        and int(_number(row.get("trades"))) >= 10
        and _number(row.get("profit_factor")) > _number(baseline.get("profit_factor"))
        and _number(row.get("roi")) > _number(baseline.get("roi"))
        and baseline_wins > 0
        and int(_number(row.get("lost_winners"))) < baseline_wins
        and not is_overfiltered(row)
    )


def ranked_metrics(metrics: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return metrics sorted by research usefulness."""
    items = [dict(row) for row in metrics if row.get("hypothesis") != "baseline"]
    baseline = _baseline(metrics)
    enough_shadow_data = int(_number(baseline.get("trades"))) >= 30
    verdict_rank = {
        "STRONG": 5,
        "PROMISING": 4,
        "NEUTRAL": 3,
        "INSUFFICIENT_DATA": 2,
        "NEGATIVE": 1,
        "NO_TRADES": 0,
        "OVERFILTERED": 0,
    }

    if not enough_shadow_data:
        return sorted(
            items,
            key=lambda row: (
                is_overfiltered(row) or int(_number(row.get("trades"))) == 0,
                -int(_number(row.get("trades"))),
                -_number(row.get("profit_factor")),
                -_number(row.get("roi")),
                str(row.get("hypothesis", "")),
            ),
        )

    return sorted(
        items,
        key=lambda row: (
            not is_leader_eligible(row, baseline),
            is_overfiltered(row) or int(_number(row.get("trades"))) == 0,
            -verdict_rank.get(str(row.get("verdict")), 0),
            -_number(row.get("profit_factor")),
            -_number(row.get("roi")),
            -int(_number(row.get("net_benefit"))),
            -int(_number(row.get("trades"))),
            str(row.get("hypothesis", "")),
        ),
    )


def eligible_leader(
    metrics: list[Mapping[str, Any]],
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a leader only after all minimum evidence gates pass."""
    baseline_row = dict(baseline or _baseline(metrics))
    if int(_number(baseline_row.get("trades"))) < 30:
        return None
    for row in ranked_metrics(metrics):
        if is_leader_eligible(row, baseline_row):
            return row
    return None


def save_hypothesis_reports(report: Mapping[str, Any]) -> None:
    """Save all Strategy Lab v2 artifacts."""
    write_json(HYPOTHESIS_REPORT_JSON, report)
    write_csv(
        HYPOTHESIS_COMPARISON_CSV,
        report.get("metrics", []),
        HYPOTHESIS_FIELDS,
    )
    write_csv(
        HYPOTHESIS_SHADOW_TRADES_CSV,
        report.get("shadow_trades", []),
        HYPOTHESIS_SHADOW_FIELDS,
    )
    HYPOTHESIS_SUMMARY_TXT.write_text(format_hypothesis_summary(report), encoding="utf-8")


def format_hypothesis_summary(report: Mapping[str, Any]) -> str:
    """Format concise Russian Strategy Lab v2 summary."""
    baseline = report.get("baseline", {})
    ranking = ranked_metrics(list(report.get("metrics", [])))
    baseline_trades = int(_number(baseline.get("trades")))
    overfiltered = [row for row in ranking if is_overfiltered(row)]
    observations = [row for row in ranking if not is_overfiltered(row)]
    leader = eligible_leader(list(report.get("metrics", [])), baseline)
    lines = [
        "Strategy Lab v2",
        "================",
        "Hypothesis Testing Framework",
        f"Mode: {report.get('mode', 'Shadow Research')}",
        f"Closed trades: {baseline.get('trades', 0)}",
        f"Baseline Winrate: {baseline.get('winrate', 0)}%",
        f"Baseline PF: {baseline.get('profit_factor', 0)}",
        "",
    ]
    if baseline_trades < 30:
        lines.extend(
            [
                "Статус ранжирования: OBSERVATION_ONLY",
                "До 30 shadow-сделок лидер не определяется.",
                "Net Benefit показан только как наблюдение.",
                "",
                "Наблюдаемые гипотезы:",
            ]
        )
    elif leader:
        lines.extend(
            [
                "Статус ранжирования: EVIDENCE_GATED",
                f"Лидер: {leader.get('hypothesis')}",
                "",
                "Рейтинг гипотез:",
            ]
        )
    else:
        lines.extend(
            [
                "Статус ранжирования: NO_ELIGIBLE_LEADER",
                "Ни одна гипотеза не прошла минимальные условия лидерства.",
                "",
                "Наблюдаемые гипотезы:",
            ]
        )
    if not observations:
        lines.append("Нет данных для наблюдения.")
    for index, row in enumerate(observations[:8], start=1):
        lines.extend(
            [
                f"{index}. {row.get('hypothesis')}",
                f"Trades: {row.get('trades', 0)}",
                f"PF: {row.get('profit_factor', 0)}",
                f"Winrate: {row.get('winrate', 0)}%",
                f"Saved Losses: {row.get('saved_losses', 0)}",
                f"Lost Winners: {row.get('lost_winners', 0)}",
                f"Net Benefit: {row.get('net_benefit', 0)}",
                f"Verdict: {row.get('verdict', 'N/A')}",
                "----------------",
            ]
        )
    lines.extend(["", "Overfiltered hypotheses:"])
    if overfiltered:
        for row in overfiltered:
            lines.extend(
                [
                    f"- {row.get('hypothesis')}",
                    "  Причина: Отфильтрованы все сделки, включая все WIN.",
                ]
            )
    else:
        lines.append("- Нет полностью отфильтрованных гипотез.")
    lines.extend(
        [
            "Рекомендация:",
            str(report.get("recommendation", "Статистика пока недостаточна.")),
            "",
            "Важно:",
            "Это исследовательские shadow-результаты.",
            "Live-стратегия, DecisionEngine, config.py, SL/TP и RR не менялись.",
        ]
    )
    return "\n".join(lines).rstrip()
