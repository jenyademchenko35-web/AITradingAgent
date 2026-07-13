"""Test fixtures for Research Orchestrator reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from report_metadata import build_report_metadata


def write_report(
    root: Path,
    filename: str,
    *,
    generated_at: str | None = None,
    metric_unit: str = "R",
    closed: int = 30,
    complete: int = 30,
    payload: dict[str, Any] | None = None,
) -> Path:
    source = root / "source.csv"
    if not source.exists():
        source.write_text("id\n1\n", encoding="utf-8")
    report = dict(payload or {})
    report["generated_at"] = generated_at or datetime.now(timezone.utc).isoformat()
    report["metadata"] = build_report_metadata(
        generator="tests.fixture",
        metric_unit=metric_unit,
        source_files=[source],
        base_dir=root,
        generated_at=report["generated_at"],
        closed_trades_total=closed,
        complete_metrics_total=complete,
    )
    path = root / filename
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
