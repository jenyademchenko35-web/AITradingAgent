"""Persistence helpers for Research Consensus outputs."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Mapping

from research_consensus.consensus_formatter import format_summary


BASE_DIR = Path(__file__).resolve().parents[1]
REPORT_PATH = BASE_DIR / "research_consensus_report.json"
SUMMARY_PATH = BASE_DIR / "research_consensus_summary.txt"
CSV_PATH = BASE_DIR / "research_consensus.csv"
CSV_FIELDS = [
    "hypothesis",
    "support",
    "modules",
    "confidence",
    "verdict",
    "recommendation",
]


def save_report(report: Mapping[str, Any]) -> None:
    """Write JSON, summary and compact CSV outputs."""
    with REPORT_PATH.open("w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    SUMMARY_PATH.write_text(format_summary(report), encoding="utf-8")
    with CSV_PATH.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in report.get("ranking", []):
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
