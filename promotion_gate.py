"""Read-only VPS promotion gate for the current strategy evidence."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from trade_registry import TradeRegistry

BASE_DIR = Path(__file__).resolve().parent
REPORT_PATH = Path("reports/promotion_gate.json")
HISTORY_PATH = Path("reports/promotion_gate_history.json")

TARGETS = {"profit_factor": 1.0, "winrate": 35.0, "net_r": 0.0,
           "max_drawdown_r": 20.0, "coverage_pct": 90.0,
           "stability_score": 60.0, "complete_trades": 100}


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _points(value: float, target: float, maximum: int, *, inverse: bool = False) -> int:
    ratio = target / value if inverse and value > 0 else (1.0 if inverse and value <= target else value / target)
    return max(0, min(maximum, round(maximum * ratio)))


def build_report(*, base_dir: str | Path = BASE_DIR) -> dict[str, Any]:
    root = Path(base_dir)
    trades_path = root / "trades.csv"
    replay = _read(root / "shadow_replay_report.json")
    walk = _read(root / "reports/walk_forward.json")
    quality = _read(root / "reports/data_quality.json")
    registry = TradeRegistry(root / "trades.csv").get_statistics()
    metrics = replay.get("metrics", {}).get("effective_portfolio", {})
    best = walk.get("best", {})
    pf = float(metrics.get("profit_factor", 0) or 0)
    winrate = float(metrics.get("winrate", 0) or 0)
    net_r = float(metrics.get("net_r", 0) or 0)
    drawdown = float(metrics.get("max_drawdown_r", 0) or 0)
    coverage = float(quality.get("coverage", {}).get("coverage_pct", 0) or 0)
    stability = float(best.get("stability_score", 0) or 0)
    sample = int(registry.get("complete_trades", 0) or 0)
    walk_pass = best.get("verdict") == "READY_FOR_AB"
    replay_pass = replay.get("status") != "INSUFFICIENT_DATA" and pf >= 1 and net_r > 0

    components = {
        "profit_factor": {"score": _points(pf, 1, 30), "max": 30, "value": pf},
        "winrate": {"score": _points(winrate, 35, 20), "max": 20, "value": winrate},
        "drawdown": {"score": _points(drawdown, 20, 20, inverse=True), "max": 20, "value": drawdown},
        "data_coverage": {"score": _points(coverage, 90, 10), "max": 10, "value": coverage},
        "replay_stability": {"score": _points(stability, 60, 10), "max": 10, "value": stability},
        "walk_forward": {"score": 5 if walk_pass else 0, "max": 5, "value": best.get("verdict", "MISSING")},
        "sample_size": {"score": _points(sample, 100, 5), "max": 5, "value": sample},
    }
    checks = {
        "profit_factor": pf >= 1, "winrate": winrate >= 35, "net_r": net_r > 0,
        "max_drawdown": drawdown <= 20, "data_coverage": coverage >= 90,
        "walk_forward": walk_pass, "replay": replay_pass,
        "stability": stability >= 60, "sample_size": sample >= 100,
    }
    reasons = []
    labels = {
        "profit_factor": ("PF below target", ">= 1.0", pf),
        "winrate": ("Winrate below target", ">= 35%", winrate),
        "net_r": ("Net R is not positive", "> 0R", net_r),
        "max_drawdown": ("Drawdown above target", "<= 20R", drawdown),
        "data_coverage": ("Data coverage below target", ">= 90%", coverage),
        "walk_forward": ("Walk Forward not passed", "READY_FOR_AB", best.get("verdict", "MISSING")),
        "replay": ("Replay not passed", "positive realistic PF and Net R with sufficient data", replay.get("status", "MISSING")),
        "stability": ("Stability below target", ">= 60/100", stability),
        "sample_size": ("Sample size below target", ">= 100 COMPLETE trades", sample),
    }
    for key, passed in checks.items():
        if not passed:
            reason, need, current = labels[key]
            reasons.append({"check": key, "reason": reason, "need": need, "current": current})
    score = sum(item["score"] for item in components.values())
    source_modified = (datetime.fromtimestamp(trades_path.stat().st_mtime, tz=timezone.utc).isoformat()
                       if trades_path.exists() else "")
    return {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_trades_modified": source_modified,
            "mode": "READ_ONLY_PROMOTION_GATE", "status": "READY_FOR_VPS" if all(checks.values()) else "NOT_READY",
            "shadow_score": score, "shadow_score_max": 100, "components": components,
            "checks": checks, "reasons": reasons,
            "metrics": {"profit_factor": pf, "winrate": winrate, "net_r": net_r,
                        "max_drawdown_r": drawdown, "data_coverage_pct": coverage,
                        "walk_forward": best.get("verdict", "MISSING"), "replay": replay.get("status", "MISSING"),
                        "stability_score": stability, "complete_trades": sample},
            "targets": TARGETS, "restrictions": {"live_unchanged": True, "automatic_promotion": False}}


def save_report(report: Mapping[str, Any], *, base_dir: str | Path = BASE_DIR) -> None:
    root = Path(base_dir); _write(root / REPORT_PATH, report)
    history_path = root / HISTORY_PATH; history = _read(history_path).get("history", [])
    if not isinstance(history, list): history = []
    entry = {"generated_at": report["generated_at"], "status": report["status"],
             "shadow_score": report["shadow_score"], "metrics": report["metrics"]}
    history.append(entry); _write(history_path, {"schema_version": 1, "history": history[-1000:]})


def format_telegram(report: Mapping[str, Any]) -> str:
    metrics = report.get("metrics", {}); checks = report.get("checks", {})
    lines = ["🚦 Strategy Status", str(report.get("status", "NOT_READY")),
             f"Score: {report.get('shadow_score', 0)}/100", ""]
    for reason in report.get("reasons", []):
        lines.append(f"Need: {reason.get('need')} (current: {reason.get('current')})")
    lines.extend(["", f"Coverage: {metrics.get('data_coverage_pct', 0):.1f}%",
                  f"Drawdown: {'PASS' if checks.get('max_drawdown') else 'FAIL'}",
                  f"Replay: {'PASS' if checks.get('replay') else 'FAIL'}",
                  f"Walk Forward: {'PASS' if checks.get('walk_forward') else 'FAIL'}"])
    return "\n".join(lines)


def run(*, base_dir: str | Path = BASE_DIR) -> dict[str, Any]:
    report = build_report(base_dir=base_dir); save_report(report, base_dir=base_dir); return report


if __name__ == "__main__":
    print(format_telegram(run()))
