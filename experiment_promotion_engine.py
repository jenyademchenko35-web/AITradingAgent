"""Read-only promotion gate for research hypotheses.

The engine ranks existing Strategy Lab hypotheses using evidence already produced
by Shadow Replay, Adaptive Research, and Research Orchestrator.  It never runs
those systems and never changes live trading configuration.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from report_metadata import build_report_metadata, metadata_age_hours, utc_now


PROJECT_ROOT = Path(__file__).resolve().parent
REPORT_PATH = PROJECT_ROOT / "experiment_promotion_report.json"
SUMMARY_PATH = PROJECT_ROOT / "experiment_promotion_summary.txt"
MIN_CLOSED_TRADES = 50
SCHEMA_VERSION = "1.0"
GENERATOR_VERSION = "1.0"

SOURCE_FILES = {
    "strategy_lab": "hypothesis_report.json",
    "shadow_replay": "shadow_replay_report.json",
    "adaptive_research": "adaptive_research_report.json",
    "research_orchestrator": "research_orchestrator_report.json",
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _hypothesis_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("atr stop", "atr").replace("momentum confirmation", "momentum")
    text = text.replace("edge threshold", "edge")
    return re.sub(r"[^a-z0-9.]+", "", text)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ExperimentPromotionEngine:
    """Rank research hypotheses behind a conservative A/B promotion gate."""

    def __init__(
        self,
        base_dir: Path | str = PROJECT_ROOT,
        minimum_closed_trades: int = MIN_CLOSED_TRADES,
        now: datetime | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.minimum_closed_trades = max(1, int(minimum_closed_trades))
        self.now = now or datetime.now(timezone.utc)

    def _load_source(self, key: str, filename: str) -> tuple[dict[str, Any], dict[str, Any]]:
        path = self.base_dir / filename
        state: dict[str, Any] = {
            "key": key,
            "path": filename,
            "status": "MISSING",
            "accepted": False,
            "age_hours": None,
            "reasons": [],
        }
        if not path.exists() or path.stat().st_size == 0:
            state["reasons"].append("Отчёт ещё не создан.")
            return {}, state
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            state["status"] = "ERROR"
            state["reasons"].append(f"Ошибка чтения: {type(exc).__name__}")
            return {}, state
        if not isinstance(payload, dict):
            state["status"] = "INCOMPATIBLE"
            state["reasons"].append("Корень отчёта должен быть JSON-объектом.")
            return {}, state

        metadata = payload.get("metadata")
        if not isinstance(metadata, Mapping):
            state["status"] = "INCOMPATIBLE"
            state["reasons"].append("Отсутствует metadata.")
            return payload, state
        schema = str(metadata.get("schema_version") or "")
        unit = str(metadata.get("metric_unit") or "").strip().upper()
        age = metadata_age_hours(metadata, now=self.now)
        ttl = _number(metadata.get("freshness_ttl_hours"), 24.0)
        state.update({
            "schema_version": schema,
            "metric_unit": unit,
            "age_hours": round(age, 3) if age is not None else None,
            "closed_trades_total": _integer(metadata.get("closed_trades_total")),
        })
        if not schema:
            state["status"] = "INCOMPATIBLE"
            state["reasons"].append("Не указана schema_version.")
        elif unit != "R":
            state["status"] = "INCOMPATIBLE"
            state["reasons"].append(f"Несовместимая metric_unit: {unit or 'не указана'}.")
        elif age is None:
            state["status"] = "INCOMPATIBLE"
            state["reasons"].append("Не указано корректное время генерации.")
        elif age < -1:
            state["status"] = "INCOMPATIBLE"
            state["reasons"].append("Время отчёта находится в будущем.")
        elif age > ttl:
            state["status"] = "STALE"
            state["reasons"].append(f"Возраст отчёта превышает TTL {ttl:g} ч.")
        else:
            state["status"] = "CURRENT"
            state["accepted"] = True
        return payload, state

    @staticmethod
    def _orchestrator_rows(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        rows = report.get("hypotheses", [])
        return {
            _hypothesis_key(row.get("hypothesis")): row
            for row in rows
            if isinstance(row, Mapping) and row.get("hypothesis")
        }

    @staticmethod
    def _adaptive_matches(report: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
        target = _hypothesis_key(name)
        recommendation = report.get("recommendation", {})
        if not isinstance(recommendation, Mapping):
            return []
        matches: list[Mapping[str, Any]] = []
        leader = recommendation.get("leader")
        if leader and _hypothesis_key(leader) == target:
            matches.append(recommendation)
        for finding in recommendation.get("findings", []) or []:
            if not isinstance(finding, Mapping):
                continue
            candidate = finding.get("candidate") or finding.get("hypothesis")
            if candidate and _hypothesis_key(candidate) == target:
                matches.append(finding)
        return matches

    def _rank_candidate(
        self,
        metric: Mapping[str, Any],
        baseline: Mapping[str, Any],
        orchestrator_row: Mapping[str, Any],
        adaptive_matches: list[Mapping[str, Any]],
        replay: Mapping[str, Any],
        source_states: Mapping[str, Mapping[str, Any]],
        closed_trades: int,
        reports_consistent: bool,
    ) -> dict[str, Any]:
        name = str(metric.get("hypothesis") or "Неизвестная гипотеза")
        trades = _integer(metric.get("trades"))
        pf = _number(metric.get("profit_factor"))
        net_r = _number(metric.get("net_r", metric.get("roi")))
        drawdown = _number(metric.get("max_drawdown_r", metric.get("max_drawdown")))
        baseline_pf = _number(baseline.get("profit_factor"))
        baseline_net = _number(baseline.get("net_r", baseline.get("roi")))
        baseline_drawdown = _number(
            baseline.get("max_drawdown_r", baseline.get("max_drawdown"))
        )
        saved_losses = _integer(metric.get("saved_losses"))
        lost_winners = _integer(metric.get("lost_winners"))
        verdict = str(metric.get("verdict") or "").upper()
        overfiltered = trades == 0 or verdict in {"NO_TRADES", "OVERFILTERED"}
        reasons_for: list[str] = []
        reasons_against: list[str] = []
        support_modules: set[str] = set()
        contradiction_modules: set[str] = set()
        score = 30.0

        if overfiltered:
            score -= 35
            reasons_against.append("Гипотеза отфильтровала все сделки.")
        elif trades >= 10:
            score += 5
        else:
            score -= 12
            reasons_against.append("Меньше 10 shadow-сделок после фильтра.")
        if pf > baseline_pf:
            score += 15
            reasons_for.append(f"Profit Factor выше baseline: {pf:.3f} против {baseline_pf:.3f}.")
        else:
            score -= 8
            reasons_against.append("Profit Factor не превосходит baseline.")
        if net_r > baseline_net:
            score += 10
            reasons_for.append(f"Net R улучшен: {net_r:.3f} против {baseline_net:.3f}.")
        else:
            score -= 6
            reasons_against.append("Net R не улучшает baseline.")
        if pf > baseline_pf and net_r > baseline_net and not overfiltered:
            support_modules.add("Strategy Lab")
        if baseline_drawdown > 0 and drawdown < baseline_drawdown:
            score += 8
            reasons_for.append("Max Drawdown ниже baseline.")
        elif drawdown > baseline_drawdown > 0:
            score -= 5
            reasons_against.append("Max Drawdown выше baseline.")
        if saved_losses > lost_winners:
            score += 8
            reasons_for.append(
                f"Сохранено LOSS больше, чем потеряно WIN: {saved_losses}/{lost_winners}."
            )
        elif lost_winners > saved_losses:
            score -= 10
            reasons_against.append(
                f"Потеряно WIN больше, чем сохранено LOSS: {lost_winners}/{saved_losses}."
            )

        orchestrator_status = str(orchestrator_row.get("status") or "").upper()
        supporting = list(orchestrator_row.get("supporting_sources", []) or [])
        contradicting = list(orchestrator_row.get("contradicting_sources", []) or [])
        if orchestrator_row:
            if orchestrator_status in {"STRONG", "PROMISING", "CANDIDATE", "OBSERVATION_ONLY"}:
                score += 8 if orchestrator_status != "OBSERVATION_ONLY" else 3
                support_modules.add("Research Orchestrator")
            elif orchestrator_status in {"REJECTED", "NEGATIVE", "OVERFILTERED"}:
                score -= 15
                contradiction_modules.add("Research Orchestrator")
                reasons_against.append(f"Research Orchestrator: {orchestrator_status}.")
            reason_target = (
                reasons_against
                if orchestrator_status in {"REJECTED", "NEGATIVE", "OVERFILTERED"}
                else reasons_for
            )
            for reason in orchestrator_row.get("reasons", []) or []:
                if reason and reason not in reason_target:
                    reason_target.append(str(reason))
            for warning in orchestrator_row.get("warnings", []) or []:
                if warning and warning not in reasons_against:
                    reasons_against.append(str(warning))
        if supporting:
            score += min(8, len(supporting) * 3)
        if contradicting:
            score -= min(12, len(contradicting) * 5)
            contradiction_modules.update(str(item) for item in contradicting)

        if adaptive_matches:
            confidence_values = [
                _number(row.get("confidence"), _number(row.get("global_confidence")))
                for row in adaptive_matches
            ]
            adaptive_confidence = max(confidence_values or [0.0])
            if adaptive_confidence > 1:
                adaptive_confidence /= 100
            score += 5 + min(5, adaptive_confidence * 5)
            support_modules.add("Adaptive Research")
            reasons_for.append("Adaptive Research независимо выделяет эту гипотезу.")

        replay_metrics = replay.get("replay_metrics", {})
        real_pf = _number(replay_metrics.get("real_profit_factor"))
        ideal_pf = _number(replay_metrics.get("ideal_profit_factor"))
        if real_pf and ideal_pf and real_pf < ideal_pf * 0.9:
            score -= 6
            reasons_against.append("Shadow Replay показывает заметный execution drag.")
        elif real_pf >= 1.0:
            score += 4
            support_modules.add("Shadow Replay")
            reasons_for.append("Shadow Replay сохраняет Profit Factor выше 1 после издержек.")

        current_sources = sum(
            state.get("accepted") is True for state in source_states.values()
        )
        if current_sources < 3:
            reasons_against.append("Недостаточно свежих совместимых исследовательских отчётов.")
        if not reports_consistent:
            reasons_against.append("Отчёты используют разное число закрытых сделок.")
        score = max(0.0, min(100.0, score))
        fresh_ratio = current_sources / max(1, len(source_states))
        confidence = round(score * (0.65 + 0.35 * fresh_ratio), 1)

        if (
            overfiltered
            or trades < 10
            or len(contradiction_modules) > len(support_modules)
            or (real_pf and real_pf < 1.0)
            or current_sources < 2
        ):
            risk = "HIGH"
        elif score >= 75 and real_pf >= 1.0 and current_sources == len(source_states):
            risk = "LOW"
        else:
            risk = "MEDIUM"

        eligible = all((
            closed_trades >= self.minimum_closed_trades,
            reports_consistent,
            current_sources >= 3,
            not overfiltered,
            trades >= 10,
            pf > baseline_pf,
            net_r > baseline_net,
            score >= 70,
            confidence >= 65,
            len(support_modules) >= 2,
            risk != "HIGH",
        ))
        if closed_trades < self.minimum_closed_trades:
            promotion_status = "INSUFFICIENT_DATA"
        elif overfiltered:
            promotion_status = "DO_NOT_PROMOTE"
        elif eligible:
            promotion_status = "CANDIDATE_FOR_AB_TEST"
        else:
            promotion_status = "CONTINUE_RESEARCH"

        return {
            "candidate": name,
            "group": str(metric.get("group") or ""),
            "promotion_status": promotion_status,
            "eligible_for_ab_test": eligible,
            "confidence": confidence,
            "risk": risk,
            "rating": round(score, 1),
            "sample_size": trades,
            "support_modules": sorted(support_modules),
            "support_count": len(support_modules),
            "contradiction_modules": sorted(contradiction_modules),
            "contradictions_count": len(contradiction_modules),
            "metrics": {
                "trades": trades,
                "profit_factor": round(pf, 6),
                "net_r": round(net_r, 6),
                "max_drawdown_r": round(drawdown, 6),
                "saved_losses": saved_losses,
                "lost_winners": lost_winners,
            },
            "reasons_for": reasons_for[:8] or ["Подтверждённых преимуществ пока нет."],
            "reasons_against": reasons_against[:8] or ["Существенных возражений не найдено."],
            "apply_automatically": False,
        }

    def build_report(self) -> dict[str, Any]:
        """Read source reports and build the promotion recommendation."""
        reports: dict[str, dict[str, Any]] = {}
        source_states: dict[str, dict[str, Any]] = {}
        for key, filename in SOURCE_FILES.items():
            reports[key], source_states[key] = self._load_source(key, filename)

        counts = [
            _integer(state.get("closed_trades_total"))
            for state in source_states.values()
            if _integer(state.get("closed_trades_total")) > 0
        ]
        closed_trades = min(counts) if counts else 0
        reports_consistent = len(set(counts)) <= 1
        lab = reports.get("strategy_lab", {})
        baseline = lab.get("baseline", {}) if isinstance(lab, Mapping) else {}
        metrics = lab.get("metrics", []) if isinstance(lab, Mapping) else []
        orchestrator = reports.get("research_orchestrator", {})
        orchestrator_rows = self._orchestrator_rows(orchestrator)
        adaptive = reports.get("adaptive_research", {})
        replay = reports.get("shadow_replay", {})

        candidates: list[dict[str, Any]] = []
        for metric in metrics:
            if not isinstance(metric, Mapping):
                continue
            name = str(metric.get("hypothesis") or "")
            if not name or _hypothesis_key(name) == "baseline":
                continue
            candidates.append(self._rank_candidate(
                metric,
                baseline if isinstance(baseline, Mapping) else {},
                orchestrator_rows.get(_hypothesis_key(name), {}),
                self._adaptive_matches(adaptive, name),
                replay,
                source_states,
                closed_trades,
                reports_consistent,
            ))
        candidates.sort(
            key=lambda row: (
                row["eligible_for_ab_test"],
                row["rating"],
                row["confidence"],
                row["sample_size"],
                row["candidate"],
            ),
            reverse=True,
        )

        promoted = [row for row in candidates if row["eligible_for_ab_test"]]
        current_sources = sum(
            state.get("accepted") is True for state in source_states.values()
        )
        if closed_trades < self.minimum_closed_trades:
            status = "INSUFFICIENT_DATA"
            recommendation = (
                f"Накопить ещё {self.minimum_closed_trades - closed_trades} закрытых сделок "
                "и продолжить Shadow Research."
            )
        elif not reports_consistent or current_sources < 3:
            status = "DATA_NOT_READY"
            recommendation = "Обновить несовместимые или устаревшие исследовательские отчёты."
        elif promoted:
            status = "CANDIDATES_AVAILABLE"
            recommendation = "Кандидаты готовы только к отдельному ручному A/B-тесту."
        else:
            status = "OBSERVE_ONLY"
            recommendation = "Ни одна гипотеза пока не прошла promotion gate."

        generated_at = utc_now()
        source_paths = [self.base_dir / name for name in SOURCE_FILES.values()]
        return {
            "generated_at": generated_at,
            "metadata": {
                **build_report_metadata(
                    generator="experiment_promotion_engine.ExperimentPromotionEngine",
                    generator_version=GENERATOR_VERSION,
                    schema_version=SCHEMA_VERSION,
                    metric_unit="R",
                    source_files=source_paths,
                    base_dir=self.base_dir,
                    closed_trades_total=closed_trades,
                    complete_metrics_total=_integer(baseline.get("trades")) if isinstance(baseline, Mapping) else 0,
                    generated_at=generated_at,
                ),
                "promotion_threshold_closed_trades": self.minimum_closed_trades,
            },
            "mode": "READ_ONLY_RESEARCH",
            "status": status,
            "closed_trades_total": closed_trades,
            "minimum_closed_trades": self.minimum_closed_trades,
            "remaining_closed_trades": max(0, self.minimum_closed_trades - closed_trades),
            "reports_consistent": reports_consistent,
            "source_reports": source_states,
            "candidates": candidates,
            "promotion_candidates": promoted,
            "summary": {
                "hypotheses_total": len(candidates),
                "promotion_candidates_total": len(promoted),
                "current_sources": current_sources,
                "top_observation": candidates[0]["candidate"] if candidates else "Нет данных",
            },
            "recommendation": recommendation,
            "apply_automatically": False,
            "restrictions": [
                "Рекомендации не применяются автоматически.",
                "DecisionEngine, config.py и LIVE не изменяются.",
                "Promotion означает только допуск к отдельному A/B-тестированию.",
            ],
        }

    def save_report(self, report: Mapping[str, Any]) -> None:
        """Write the engine's own JSON and text artifacts atomically."""
        _atomic_write(
            self.base_dir / REPORT_PATH.name,
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_write(self.base_dir / SUMMARY_PATH.name, format_summary(report) + "\n")

    def run(self) -> dict[str, Any]:
        report = self.build_report()
        self.save_report(report)
        return report


def format_summary(report: Mapping[str, Any]) -> str:
    """Format a concise Russian terminal and artifact summary."""
    lines = [
        "====================================",
        "Experiment Promotion Engine v1",
        "====================================",
        f"Статус: {report.get('status', 'INSUFFICIENT_DATA')}",
        f"Закрытых сделок: {report.get('closed_trades_total', 0)} / {report.get('minimum_closed_trades', MIN_CLOSED_TRADES)}",
        f"Гипотез оценено: {report.get('summary', {}).get('hypotheses_total', 0)}",
        f"Кандидатов на A/B-тест: {report.get('summary', {}).get('promotion_candidates_total', 0)}",
        "",
        "Рейтинг наблюдений:",
    ]
    candidates = list(report.get("candidates", []) or [])
    if not candidates:
        lines.append("Нет совместимых гипотез.")
    for index, candidate in enumerate(candidates[:5], start=1):
        lines.append(
            f"{index}. {candidate.get('candidate')} | "
            f"Confidence {candidate.get('confidence', 0)}% | "
            f"Risk {candidate.get('risk')} | {candidate.get('promotion_status')}"
        )
    lines.extend([
        "",
        f"Рекомендация: {report.get('recommendation', 'Продолжить сбор данных.')}",
        "Автоматическое применение: запрещено",
    ])
    return "\n".join(lines)


def print_report(report: Mapping[str, Any]) -> None:
    print(format_summary(report))


def main() -> None:
    report = ExperimentPromotionEngine().run()
    print_report(report)


if __name__ == "__main__":
    main()
