"""Read-only loss attribution and counterfactual failure analysis."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trade_registry import TradeRegistry

BASE_DIR = Path(__file__).resolve().parent
REPORT_PATH = BASE_DIR / "reports/loss_attribution.json"
SUMMARY_PATH = BASE_DIR / "reports/loss_attribution_summary.txt"
HISTORY_DIR = BASE_DIR / "reports/loss_attribution_history"
MIN_FILTER_SAMPLE = 5
RECOMMENDATIONS = {"KEEP_IN_SHADOW", "INVESTIGATE", "INSUFFICIENT_DATA"}
DIMENSIONS = (
    "symbol", "direction", "timeframe", "hour_of_day", "weekday",
    "volatility", "trend_alignment", "confidence", "score", "quality",
    "primary_blocker", "exit_type", "consecutive_losses", "market_regime",
)
COMBINATIONS = (
    ("symbol", "direction"), ("symbol", "confidence"),
    ("direction", "volatility"), ("trend_alignment", "confidence"),
    ("symbol", "market_regime"), ("direction", "primary_blocker"),
    ("quality", "score"),
)
ACTIONABLE_DIMENSIONS = (
    "symbol", "direction", "timeframe", "hour_of_day", "weekday",
    "volatility", "trend_alignment", "confidence", "score", "quality",
    "primary_blocker", "market_regime",
)
PARETO_FIELDS = ("symbol", "direction", "volatility", "trend_alignment", "confidence")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [_num(row.get("R", row.get("r", row.get("pnl_r")))) for row in rows]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    loss_total = abs(sum(losses))
    pf = sum(wins) / loss_total if loss_total else (999.0 if wins else 0.0)
    return {
        "trades": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "winrate_pct": round(len(wins) / len(values) * 100, 4) if values else 0.0,
        "profit_factor": round(pf, 6),
        "net_r": round(sum(values), 6),
        "average_r": round(sum(values) / len(values), 6) if values else 0.0,
        "gross_loss_r": round(loss_total, 6),
    }


def _bucket(value: Any, boundaries: Sequence[float], labels: Sequence[str]) -> str:
    if value in (None, "", "UNKNOWN"):
        return "UNKNOWN"
    number = _num(value, float("nan"))
    if not math.isfinite(number):
        return "UNKNOWN"
    for boundary, label in zip(boundaries, labels):
        if number < boundary:
            return label
    return labels[-1]


class LossAttribution:
    """Attribute negative R without changing any source or strategy state."""

    def __init__(
        self, *, registry: TradeRegistry | None = None,
        base_dir: str | Path = BASE_DIR,
        context_rows: Iterable[Mapping[str, Any]] | None = None,
        report_path: str | Path = REPORT_PATH,
        summary_path: str | Path = SUMMARY_PATH,
        history_dir: str | Path = HISTORY_DIR,
        minimum_filter_sample: int = MIN_FILTER_SAMPLE,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.registry = registry or TradeRegistry(self.base_dir / "trades.csv")
        self.context_rows = ([dict(row) for row in context_rows] if context_rows is not None
                             else _read_csv(self.base_dir / "trade_market_context.csv"))
        self.report_path, self.summary_path = Path(report_path), Path(summary_path)
        self.history_dir = Path(history_dir)
        self.minimum_filter_sample = minimum_filter_sample

    @staticmethod
    def _join_key(row: Mapping[str, Any]) -> tuple[str, str]:
        symbol = str(row.get("symbol", "")).upper()
        opened = str(row.get("opened_at", row.get("timestamp", "")))
        parsed = _timestamp(opened)
        return symbol, parsed.isoformat() if parsed else opened

    def _enrich(self, trades: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        contexts = {self._join_key(row): row for row in self.context_rows}
        enriched: list[dict[str, Any]] = []
        streak = 0
        for source in sorted(trades, key=lambda row: str(row.get("opened_at", ""))):
            row = deepcopy(dict(source))
            context = contexts.get(self._join_key(row), {})
            for field in ("timeframe", "confidence", "score", "quality", "primary_blocker", "market_regime", "volatility", "trend"):
                if row.get(field) in (None, "") and context.get(field) not in (None, ""):
                    row[field] = context[field]
            opened = _timestamp(row.get("opened_at"))
            row["hour_of_day"] = f"{opened.hour:02d}:00" if opened else "UNKNOWN"
            row["weekday"] = opened.strftime("%A") if opened else "UNKNOWN"
            row["timeframe"] = str(row.get("timeframe") or "UNKNOWN")
            row["confidence"] = _bucket(row.get("confidence"), (60, 70, 80, 90, float("inf")), ("<60", "60-69", "70-79", "80-89", "90+"))
            row["score"] = _bucket(row.get("score"), (20, 23, 25, 27, float("inf")), ("<20", "20-22", "23-24", "25-26", "27+"))
            volatility = row.get("volatility")
            row["volatility"] = _bucket(volatility, (.5, 1.0, 2.0, float("inf")), ("LOW_<0.5%", "NORMAL_0.5-1%", "HIGH_1-2%", "EXTREME_2%+"))
            trend = str(row.get("trend") or "UNKNOWN").upper()
            direction = str(row.get("direction") or "UNKNOWN").upper()
            row["trend_alignment"] = ("UNKNOWN" if trend == "UNKNOWN" else
                                      ("ALIGNED" if trend == direction else "COUNTER_TREND"))
            row["quality"] = str(row.get("quality") or "UNKNOWN").upper()
            row["primary_blocker"] = str(row.get("primary_blocker") or "NONE").upper()
            row["market_regime"] = str(row.get("market_regime") or "UNKNOWN").upper()
            result = str(row.get("result") or "").upper()
            row["exit_type"] = ("STOP_LOSS" if result == "LOSS" else
                                ("TAKE_PROFIT" if result == "WIN" else result or "OTHER"))
            # Only losses known before this trade are valid as a filter input.
            row["loss_streak"] = streak
            row["consecutive_losses"] = "0" if not streak else ("1" if streak == 1 else ("2-3" if streak <= 3 else "4+"))
            if _num(row.get("R")) < 0:
                streak += 1
            else:
                streak = 0
            enriched.append(row)
        return enriched

    @staticmethod
    def _segment(rows: Sequence[Mapping[str, Any]], fields: Sequence[str], total_loss: float) -> list[dict[str, Any]]:
        groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[tuple(str(row.get(field, "UNKNOWN")) for field in fields)].append(row)
        result = []
        for values, members in groups.items():
            metrics = _metrics(members)
            negative = abs(min(0.0, metrics["net_r"]))
            result.append({
                "segment": " + ".join(f"{field.upper()}={value}" for field, value in zip(fields, values)),
                "fields": dict(zip(fields, values)), **metrics,
                "loss_contribution_pct": round(negative / total_loss * 100, 4) if total_loss else 0.0,
                "sample_share_pct": round(len(members) / len(rows) * 100, 4) if rows else 0.0,
                "trade_ids": [row.get("trade_id") for row in members],
            })
        return sorted(result, key=lambda item: (item["net_r"], -item["trades"]))

    def _counterfactual(self, rows: Sequence[Mapping[str, Any]], candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        baseline = _metrics(rows)
        result = []
        for segment in candidates:
            ids = set(segment["trade_ids"])
            removed = [row for row in rows if row.get("trade_id") in ids]
            remaining = [row for row in rows if row.get("trade_id") not in ids]
            after = _metrics(remaining)
            values = tuple(str(value).upper() for value in segment.get("fields", {}).values())
            sufficient = (len(removed) >= self.minimum_filter_sample and
                          len(remaining) >= self.minimum_filter_sample and
                          not any(value in {"UNKNOWN", "NONE", ""} for value in values))
            recommendation = "INSUFFICIENT_DATA"
            if sufficient:
                recommendation = ("INVESTIGATE" if segment["net_r"] < 0 and
                                  after["net_r"] > baseline["net_r"] and
                                  after["profit_factor"] >= baseline["profit_factor"]
                                  else "KEEP_IN_SHADOW")
            result.append({
                "filter": segment["segment"], "sample": len(removed),
                "removed_losing_trades": sum(_num(row.get("R")) < 0 for row in removed),
                "lost_winning_trades": sum(_num(row.get("R")) > 0 for row in removed),
                "removed_net_r": segment["net_r"],
                "baseline_pf": baseline["profit_factor"], "counterfactual_pf": after["profit_factor"],
                "pf_change": round(after["profit_factor"] - baseline["profit_factor"], 6),
                "baseline_net_r": baseline["net_r"], "counterfactual_net_r": after["net_r"],
                "net_r_change": round(after["net_r"] - baseline["net_r"], 6),
                "minimum_sample": self.minimum_filter_sample,
                "minimum_sample_passed": sufficient,
                "remaining_trades": len(remaining),
                "recommendation": recommendation,
            })
        return sorted(result, key=lambda item: (item["recommendation"] != "INVESTIGATE", -item["net_r_change"]))

    @staticmethod
    def _pareto(combinations: Sequence[Mapping[str, Any]], total_loss: float) -> dict[str, Any]:
        negative = sorted((item for item in combinations if item["gross_loss_r"] > 0), key=lambda item: item["gross_loss_r"], reverse=True)
        target, cumulative, selected = total_loss * .8, 0.0, []
        for item in negative:
            cumulative += item["gross_loss_r"]
            selected.append({key: item[key] for key in ("segment", "trades", "net_r", "gross_loss_r", "loss_contribution_pct")})
            if cumulative >= target:
                break
        top_twenty_count = max(1, math.ceil(len(negative) * .2)) if negative else 0
        top_twenty_loss = sum(item["gross_loss_r"] for item in negative[:top_twenty_count])
        return {
            "negative_scenarios": len(negative), "target_loss_r": round(target, 6),
            "scenarios_to_reach_80pct": selected,
            "scenario_share_to_reach_80pct": round(len(selected) / len(negative) * 100, 4) if negative else 0.0,
            "top_20pct_scenario_count": top_twenty_count,
            "loss_share_from_top_20pct_pct": round(top_twenty_loss / total_loss * 100, 4) if total_loss else 0.0,
            "pareto_20_80_confirmed": bool(total_loss and top_twenty_loss >= target),
        }

    def build_report(self) -> dict[str, Any]:
        source = self.registry.get_complete_trades()
        rows = self._enrich(source)
        baseline = _metrics(rows)
        total_loss = baseline["gross_loss_r"]
        dimensions = {field: self._segment(rows, (field,), total_loss) for field in DIMENSIONS}
        combinations = []
        for fields in COMBINATIONS:
            combinations.extend(self._segment(rows, fields, total_loss))
        combinations.sort(key=lambda item: (item["net_r"], -item["trades"]))
        actionable_segments = sum((dimensions[field] for field in ACTIONABLE_DIMENSIONS), [])
        candidates = [item for item in combinations + actionable_segments if item["net_r"] < 0]
        # Deduplicate equivalent trade sets so a factor label cannot dominate recommendations.
        unique, seen = [], set()
        for item in candidates:
            key = tuple(sorted(str(value) for value in item["trade_ids"]))
            if key not in seen:
                seen.add(key); unique.append(item)
        counterfactuals = self._counterfactual(rows, unique)
        overall = "INSUFFICIENT_DATA" if len(rows) < self.minimum_filter_sample else ("INVESTIGATE" if baseline["net_r"] < 0 else "KEEP_IN_SHADOW")
        fingerprint_data = [{"trade_id": row.get("trade_id"), "R": row.get("R"), **{field: row.get(field) for field in DIMENSIONS}} for row in rows]
        fingerprint = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True, default=str).encode()).hexdigest()
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(), "mode": "SHADOW_READ_ONLY",
            "status": overall, "dataset_fingerprint": fingerprint,
            "sample": {"complete_trades": len(rows), "minimum_filter_sample": self.minimum_filter_sample},
            "baseline": baseline, "dimensions": dimensions,
            "combinations": combinations,
            "pareto": self._pareto(self._segment(rows, PARETO_FIELDS, total_loss), total_loss),
            "counterfactual_filters": counterfactuals,
            "top_loss_scenarios": combinations[:10],
            "data_coverage": {field: round(sum(row.get(field) not in (None, "", "UNKNOWN") for row in rows) / len(rows) * 100, 2) if rows else 0 for field in DIMENSIONS},
            "allowed_recommendations": sorted(RECOMMENDATIONS),
            "restrictions": ["READ_ONLY", "SHADOW_ONLY", "NO_AUTOMATIC_FILTER_APPLICATION", "LIVE_AND_TRADING_LOGIC_UNCHANGED"],
        }

    def write_reports(self) -> tuple[dict[str, Any], bool]:
        report = self.build_report()
        _atomic_write(self.report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(self.summary_path, format_loss_analysis(report) + "\n")
        self.history_dir.mkdir(parents=True, exist_ok=True)
        duplicate = any(_read_report(path).get("dataset_fingerprint") == report["dataset_fingerprint"] for path in self.history_dir.glob("*.json"))
        if not duplicate:
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
            _atomic_write(self.history_dir / f"{stamp}.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report, not duplicate


def _read_report(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_loss_report(path: str | Path = REPORT_PATH) -> dict[str, Any]:
    return _read_report(Path(path))


def format_loss_analysis(report: Mapping[str, Any], view: str = "") -> str:
    baseline = report.get("baseline", {})
    if view == "filters":
        lines = ["🔎 Loss Analysis — Filters"]
        for item in report.get("counterfactual_filters", [])[:5]:
            lines.append(f"{item['filter']}: ΔNet R {item['net_r_change']:+.2f}, lost winners {item['lost_winning_trades']} — {item['recommendation']}")
        return "\n".join(lines + ["Shadow analysis only."])
    top = report.get("top_loss_scenarios", [])
    lines = ["🔎 Loss Attribution", f"Complete trades: {report.get('sample', {}).get('complete_trades', 0)}", f"PF: {_num(baseline.get('profit_factor')):.3f}", f"Net R: {_num(baseline.get('net_r')):.3f}", f"Gross Loss: {_num(baseline.get('gross_loss_r')):.3f}R"]
    if top:
        lines.extend(["Top loss scenario:", str(top[0]["segment"]), f"Net R: {_num(top[0]['net_r']):.3f}", f"Loss contribution: {_num(top[0]['loss_contribution_pct']):.1f}%"])
    if view == "top":
        lines.append("Top scenarios:")
        lines.extend(f"• {item['segment']}: {item['net_r']:+.2f}R ({item['trades']} trades)" for item in top[:5])
    lines.extend([f"Recommendation: {report.get('status', 'INSUFFICIENT_DATA')}", "LIVE remains unchanged."])
    return "\n".join(lines)


def main() -> None:
    report, snapshot = LossAttribution().write_reports()
    print(format_loss_analysis(report, "top"))
    print(f"History snapshot: {'created' if snapshot else 'deduplicated'}")


if __name__ == "__main__":
    main()
