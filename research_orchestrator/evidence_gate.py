"""Evidence normalization and conservative hypothesis quality gates."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Iterable, Mapping

from research_orchestrator.models import Evidence, HypothesisAssessment


CATEGORIES = frozenset({
    "PERFORMANCE",
    "RISK",
    "ENTRY_QUALITY",
    "EXIT_QUALITY",
    "MOMENTUM",
    "TREND",
    "STRUCTURE",
    "NEWS",
    "MEMORY",
    "REPLAY",
    "DATA_QUALITY",
    "EXECUTION",
    "LINEAGE",
})


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a report value to float without accepting NaN-like failures."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def confidence_level(sample_size: int, penalties: int = 0) -> str:
    """Return sample confidence with conservative quality downgrades."""
    if sample_size < 10:
        index = 0
    elif sample_size < 20:
        index = 1
    elif sample_size < 50:
        index = 2
    else:
        index = 3
    levels = ("VERY_LOW", "LOW", "MEDIUM", "HIGH")
    return levels[max(index - max(penalties, 0), 0)]


class EvidenceBuilder:
    """Extract comparable evidence only from already validated payloads."""

    def __init__(self) -> None:
        self._counter = 0

    def build(
        self,
        payloads: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[Evidence], dict[str, dict[str, Any]], dict[str, Any]]:
        """Return evidence rows, candidate metrics and canonical baseline."""
        evidence: list[Evidence] = []
        candidates: dict[str, dict[str, Any]] = {}
        audit = payloads.get("trade_metrics_audit", {})
        baseline = dict(audit.get("metrics", {})) if isinstance(
            audit.get("metrics"), Mapping
        ) else {}
        if baseline:
            evidence.append(self._make(
                source="trade_metrics_audit",
                category="PERFORMANCE",
                hypothesis="Current Strategy",
                metric="canonical_metrics",
                value={
                    "profit_factor": baseline.get("profit_factor"),
                    "net_r": baseline.get("net_r"),
                    "max_drawdown_r": baseline.get("max_drawdown_r"),
                },
                sample_size=safe_int(baseline.get("metrics_trades")),
                direction="NEUTRAL",
                notes="Канонический baseline в единицах R.",
            ))

        for source in ("hypothesis_report", "strategy_lab_report"):
            report = payloads.get(source, {})
            rows = report.get("metrics", [])
            if not isinstance(rows, list):
                continue
            source_baseline = self._baseline_from_rows(rows)
            comparison = source_baseline or baseline
            for raw_row in rows:
                if not isinstance(raw_row, Mapping):
                    continue
                hypothesis = str(
                    raw_row.get("hypothesis") or raw_row.get("strategy") or ""
                ).strip()
                if not hypothesis or hypothesis.lower() in {"baseline", "current"}:
                    continue
                hypothesis = self._canonical_hypothesis(hypothesis)
                row = dict(raw_row)
                candidates[hypothesis] = self._merge_candidate(
                    candidates.get(hypothesis, {}), row
                )
                direction = self._performance_direction(row, comparison)
                evidence.append(self._make(
                    source=source,
                    category=self._category_for(hypothesis),
                    hypothesis=hypothesis,
                    metric="performance_bundle",
                    value={
                        "profit_factor": row.get("profit_factor"),
                        "net_r": row.get("net_r", row.get("roi")),
                        "max_drawdown_r": row.get(
                            "max_drawdown_r", row.get("max_drawdown")
                        ),
                    },
                    sample_size=safe_int(row.get("trades")),
                    direction=direction,
                    notes=(
                        f"trades={safe_int(row.get('trades'))}; "
                        f"lost_winners={safe_int(row.get('lost_winners'))}; "
                        f"verdict={row.get('verdict') or row.get('sample_status', '')}"
                    ),
                ))

        replay = payloads.get("trade_replay_report", {})
        replay_sample = replay.get("sample", {})
        replay_size = safe_int(
            replay_sample.get("closed_trades")
            if isinstance(replay_sample, Mapping)
            else 0
        )
        summary = replay.get("summary", {})
        improvements = summary.get("top_improvements", []) if isinstance(
            summary, Mapping
        ) else []
        for row in improvements if isinstance(improvements, list) else []:
            if not isinstance(row, Mapping):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            name = self._canonical_hypothesis(name)
            evidence.append(self._make(
                source="trade_replay_report",
                category="REPLAY",
                hypothesis=name,
                metric="observed_improvement_count",
                value=safe_int(row.get("count")),
                sample_size=replay_size,
                direction="SUPPORTS" if safe_int(row.get("count")) else "NEUTRAL",
                notes="Replay-наблюдение; не является LIVE-рекомендацией.",
            ))

        shadow_replay = payloads.get("shadow_replay_report", {})
        shadow_sample = shadow_replay.get("sample", {})
        shadow_metrics = shadow_replay.get("metrics", {})
        if isinstance(shadow_metrics, Mapping) and shadow_metrics:
            ideal = shadow_metrics.get("ideal_all", {})
            effective = shadow_metrics.get("effective_portfolio", {})
            impact = shadow_metrics.get("impact", {})
            if isinstance(ideal, Mapping) and isinstance(effective, Mapping):
                ideal_pf = safe_float(ideal.get("profit_factor"))
                effective_pf = safe_float(effective.get("profit_factor"))
                ideal_net = safe_float(ideal.get("net_r"))
                effective_net = safe_float(effective.get("net_r"))
                if effective_pf < ideal_pf or effective_net < ideal_net:
                    direction = "CONTRADICTS"
                elif effective_pf > ideal_pf and effective_net > ideal_net:
                    direction = "SUPPORTS"
                else:
                    direction = "NEUTRAL"
                sample_size = safe_int(
                    shadow_sample.get("complete_metrics_total")
                    if isinstance(shadow_sample, Mapping)
                    else effective.get("trades")
                )
                evidence.append(self._make(
                    source="shadow_replay_report",
                    category="EXECUTION",
                    hypothesis="Execution Realism",
                    metric="ideal_vs_effective_r",
                    value={
                        "ideal_profit_factor": ideal_pf,
                        "effective_profit_factor": effective_pf,
                        "ideal_net_r": ideal_net,
                        "effective_net_r": effective_net,
                        "execution_impact_r": (
                            impact.get("total_execution_impact_r")
                            if isinstance(impact, Mapping)
                            else None
                        ),
                    },
                    sample_size=sample_size,
                    direction=direction,
                    notes=(
                        "Shadow Replay v2: комиссии, slippage, funding, "
                        "latency и portfolio capacity; только research."
                    ),
                ))

        consensus = payloads.get("research_consensus_report", {})
        ranking = consensus.get("ranking", [])
        for row in ranking if isinstance(ranking, list) else []:
            if not isinstance(row, Mapping):
                continue
            hypothesis = str(row.get("hypothesis") or "").strip()
            if not hypothesis:
                continue
            hypothesis = self._canonical_hypothesis(hypothesis)
            verdict = str(row.get("verdict") or "").upper()
            if verdict in {"SUPPORTED", "LIKELY"}:
                direction = "SUPPORTS"
            elif verdict in {"REJECTED", "WEAK"}:
                direction = "CONTRADICTS"
            elif verdict == "INSUFFICIENT_DATA":
                direction = "INSUFFICIENT"
            else:
                direction = "NEUTRAL"
            evidence.append(self._make(
                source="research_consensus_report",
                category=self._category_for(hypothesis),
                hypothesis=hypothesis,
                metric="consensus_support_percent",
                value=row.get("support_percent", 0),
                sample_size=safe_int(consensus.get("closed_trades")),
                direction=direction,
                notes=f"Consensus verdict={verdict or 'N/A'}.",
            ))

        memory = payloads.get("trade_memory_report", {})
        memory_stats = memory.get("stats", {})
        if isinstance(memory_stats, Mapping) and memory_stats:
            target = memory.get("target", {})
            edge = safe_int(target.get("edge")) if isinstance(target, Mapping) else 0
            pf = safe_float(memory_stats.get("profit_factor"))
            net_r = safe_float(memory_stats.get("net_r"))
            direction = "SUPPORTS" if pf > 1 and net_r > 0 else "CONTRADICTS"
            hypotheses = ["Current Strategy"]
            hypotheses.extend(
                f"Edge >= {threshold}"
                for threshold in (15, 16, 17, 18, 20, 22, 24)
                if edge >= threshold
            )
            for hypothesis in hypotheses:
                evidence.append(self._make(
                    source="trade_memory_report",
                    category="MEMORY",
                    hypothesis=hypothesis,
                    metric="similar_trade_performance",
                    value={"profit_factor": pf, "net_r": net_r},
                    sample_size=safe_int(memory_stats.get("metrics_complete_total")),
                    direction=direction,
                    notes="Метрики только по найденной похожей выборке.",
                ))

        foundation = payloads.get("runtime_data_foundation_report", {})
        test_status = str(
            foundation.get("tests", {}).get("status", "")
            if isinstance(foundation.get("tests"), Mapping)
            else ""
        ).upper()
        if foundation:
            evidence.append(self._make(
                source="runtime_data_foundation_report",
                category="DATA_QUALITY",
                hypothesis="Research Data Quality",
                metric="regression_tests",
                value=test_status or "UNKNOWN",
                sample_size=safe_int(baseline.get("metrics_trades")),
                direction="SUPPORTS" if test_status == "PASS" else "CONTRADICTS",
                notes=(
                    "Регрессионная защита пройдена."
                    if test_status == "PASS"
                    else "CRITICAL: regression tests не подтверждены."
                ),
            ))

        return evidence, candidates, baseline

    def _make(
        self,
        *,
        source: str,
        category: str,
        hypothesis: str,
        metric: str,
        value: Any,
        sample_size: int,
        direction: str,
        notes: str,
    ) -> Evidence:
        self._counter += 1
        digest = hashlib.sha1(
            f"{source}|{hypothesis}|{metric}|{self._counter}".encode("utf-8")
        ).hexdigest()[:12]
        return Evidence(
            evidence_id=f"EV-{digest}",
            source=source,
            category=category if category in CATEGORIES else "PERFORMANCE",
            hypothesis=hypothesis,
            metric=metric,
            value=value,
            sample_size=sample_size,
            confidence=confidence_level(sample_size),
            freshness_status="VALID",
            quality_status="VALID",
            direction=direction,
            notes=notes,
        )

    @staticmethod
    def _baseline_from_rows(rows: list[Any]) -> dict[str, Any]:
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            name = str(row.get("hypothesis") or row.get("strategy") or "").lower()
            if name in {"baseline", "current"}:
                return dict(row)
        return {}

    @staticmethod
    def _merge_candidate(
        current: Mapping[str, Any],
        new: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not current:
            return dict(new)
        current_trades = safe_int(current.get("trades"))
        new_trades = safe_int(new.get("trades"))
        return dict(new if new_trades >= current_trades else current)

    @staticmethod
    def _performance_direction(
        row: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> str:
        if not baseline:
            return "INSUFFICIENT"
        pf = safe_float(row.get("profit_factor"))
        net_r = safe_float(row.get("net_r", row.get("roi")))
        baseline_pf = safe_float(baseline.get("profit_factor"))
        baseline_net = safe_float(baseline.get("net_r", baseline.get("roi")))
        if pf > baseline_pf and net_r > baseline_net:
            return "SUPPORTS"
        if pf < baseline_pf and net_r < baseline_net:
            return "CONTRADICTS"
        return "NEUTRAL"

    @staticmethod
    def _category_for(hypothesis: str) -> str:
        text = hypothesis.lower()
        if "momentum" in text:
            return "MOMENTUM"
        if "trend" in text:
            return "TREND"
        if "news" in text:
            return "NEWS"
        if "atr" in text or "stop" in text:
            return "RISK"
        if "structure" in text:
            return "STRUCTURE"
        return "PERFORMANCE"

    @staticmethod
    def _canonical_hypothesis(value: str) -> str:
        """Join aliases emitted by independent research modules."""
        text = " ".join(str(value).strip().split())
        lowered = text.lower()
        if lowered in {"momentum+", "momentum confirmation", "momentum filter"}:
            return "Momentum Filter"
        if lowered in {"news veto", "news filter"}:
            return "News Filter"
        if lowered.startswith("atr stop "):
            return f"ATR {text.split()[-1]}"
        return text


class EvidenceGate:
    """Apply minimum samples and multi-source performance requirements."""

    def __init__(self, drawdown_tolerance_r: float = 1.0) -> None:
        self.drawdown_tolerance_r = max(float(drawdown_tolerance_r), 0.0)

    def assess_all(
        self,
        candidates: Mapping[str, Mapping[str, Any]],
        baseline: Mapping[str, Any],
        evidence: Iterable[Evidence],
        *,
        critical_data_quality: bool = False,
    ) -> list[HypothesisAssessment]:
        by_hypothesis: dict[str, list[Evidence]] = defaultdict(list)
        for item in evidence:
            by_hypothesis[item.hypothesis].append(item)
        assessments = []
        for hypothesis, metrics in candidates.items():
            assessments.append(self.assess(
                hypothesis,
                metrics,
                baseline,
                by_hypothesis.get(hypothesis, []),
                critical_data_quality=critical_data_quality,
            ))
        return assessments

    def assess(
        self,
        hypothesis: str,
        metrics: Mapping[str, Any],
        baseline: Mapping[str, Any],
        evidence: Iterable[Evidence],
        *,
        critical_data_quality: bool = False,
    ) -> HypothesisAssessment:
        rows = list(evidence)
        sample = safe_int(metrics.get("trades"))
        wins = safe_int(metrics.get("wins"))
        lost_winners = safe_int(metrics.get("lost_winners"))
        baseline_wins = safe_int(baseline.get("wins"))
        support = sorted({
            item.source for item in rows
            if item.direction == "SUPPORTS"
            and item.freshness_status == "VALID"
            and item.quality_status == "VALID"
        })
        contradict = sorted({
            item.source for item in rows
            if item.direction == "CONTRADICTS"
            and item.freshness_status == "VALID"
            and item.quality_status == "VALID"
        })
        warnings: list[str] = []
        reasons: list[str] = []
        overfiltered = (
            sample == 0
            or bool(metrics.get("overfiltered"))
            or (
                baseline_wins > 0
                and lost_winners >= baseline_wins
                and wins == 0
            )
        )

        if critical_data_quality:
            status = "DATA_QUALITY_BLOCKED"
            reasons.append("Обнаружено CRITICAL предупреждение качества данных.")
        elif overfiltered:
            status = "OVERFILTERED"
            reasons.append("Гипотеза отфильтровала все сделки или все baseline WIN.")
        elif support and contradict:
            status = "CONFLICTED"
            reasons.append("Свежие независимые источники противоречат друг другу.")
        elif sample < 15:
            status = "INSUFFICIENT_DATA"
            reasons.append("Для кандидата требуется минимум 15 полных сделок.")
        elif self._strong(metrics, support):
            status = "STRONG_CANDIDATE"
            reasons.append("Выполнены строгие multi-source и performance gates.")
        elif self._promising(metrics, baseline, support, lost_winners, baseline_wins):
            status = "PROMISING_CANDIDATE"
            reasons.append("Кандидат улучшает PF и Net R при допустимой просадке.")
        elif self._worse_than_baseline(metrics, baseline):
            status = "REJECTED"
            reasons.append("PF и Net R не улучшают baseline.")
        else:
            status = "OBSERVATION_ONLY"
            reasons.append("Эффект наблюдается, но evidence gate ещё не пройден.")

        penalties = 0
        if safe_int(metrics.get("incomplete_metrics")):
            warnings.append("Есть incomplete metrics.")
            penalties += 1
        if not bool(metrics.get("commission_included", False)):
            warnings.append("Комиссия/slippage не подтверждены.")
            penalties += 1
        if contradict:
            penalties += 1
        return HypothesisAssessment(
            hypothesis=hypothesis,
            status=status,
            confidence=confidence_level(sample, penalties),
            metrics=dict(metrics),
            supporting_sources=support,
            contradicting_sources=contradict,
            evidence_count=len(rows),
            reasons=reasons,
            warnings=warnings,
        )

    def _promising(
        self,
        metrics: Mapping[str, Any],
        baseline: Mapping[str, Any],
        support: list[str],
        lost_winners: int,
        baseline_wins: int,
    ) -> bool:
        sample = safe_int(metrics.get("trades"))
        pf = safe_float(metrics.get("profit_factor"))
        net_r = safe_float(metrics.get("net_r", metrics.get("roi")))
        drawdown = safe_float(
            metrics.get("max_drawdown_r", metrics.get("max_drawdown"))
        )
        baseline_pf = safe_float(baseline.get("profit_factor"))
        baseline_net = safe_float(baseline.get("net_r", baseline.get("roi")))
        baseline_dd = safe_float(
            baseline.get("max_drawdown_r", baseline.get("max_drawdown"))
        )
        return (
            sample >= 15
            and pf > baseline_pf
            and net_r > baseline_net
            and drawdown <= baseline_dd + self.drawdown_tolerance_r
            and len(support) >= 2
            and not (baseline_wins > 0 and lost_winners >= baseline_wins)
        )

    @staticmethod
    def _strong(metrics: Mapping[str, Any], support: list[str]) -> bool:
        bootstrap_ok = metrics.get("bootstrap_ci_good")
        bootstrap_pass = bootstrap_ok is not False
        prospective = bool(
            metrics.get("prospective_dry_run")
            or metrics.get("dry_run_available")
        )
        return (
            safe_int(metrics.get("trades")) >= 30
            and safe_float(metrics.get("profit_factor")) > 1
            and safe_float(metrics.get("net_r", metrics.get("roi"))) > 0
            and len(support) >= 3
            and prospective
            and bootstrap_pass
        )

    @staticmethod
    def _worse_than_baseline(
        metrics: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> bool:
        return (
            safe_float(metrics.get("profit_factor"))
            <= safe_float(baseline.get("profit_factor"))
            and safe_float(metrics.get("net_r", metrics.get("roi")))
            <= safe_float(baseline.get("net_r", baseline.get("roi")))
        )
