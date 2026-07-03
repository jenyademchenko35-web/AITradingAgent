"""Repair legacy data issues in signals_v3.csv with backups."""

from __future__ import annotations

import csv
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List


BASE_DIR = Path(__file__).resolve().parent
BACKUPS_DIR = BASE_DIR / "backups"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
REJECTED_FILE = BASE_DIR / "rejected_rows.csv"

EXPECTED_HEADER = [
    "timestamp", "symbol", "direction", "signal", "score", "confidence",
    "quality", "trend_long", "trend_short", "structure_long",
    "structure_short", "momentum_long", "momentum_short", "risk_long",
    "risk_short", "long_total", "short_total", "summary",
]

QUALITY_BY_SIGNAL = {
    "HIGH PRIORITY": "A",
    "SETUP": "B",
    "WATCH": "C",
    "WAIT": "D",
    "NO TRADE": "E",
}


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def create_backups() -> None:
    """Create timestamped backups before repairing data."""
    BACKUPS_DIR.mkdir(exist_ok=True)
    stamp = utc_stamp()
    shutil.copy2(SIGNALS_FILE, BACKUPS_DIR / f"signals_v3_backup_{stamp}.csv")
    shutil.copy2(DEBUG_FILE, BACKUPS_DIR / f"decision_debug_backup_{stamp}.csv")


def normalize_quality(signal: str, quality: str) -> str:
    """Return a valid quality value."""
    if quality in {"A", "B", "C", "D", "E"}:
        return quality
    return QUALITY_BY_SIGNAL.get(signal, "E")


def repair_signals() -> dict:
    """Repair signals_v3.csv and move broken rows to rejected_rows.csv."""
    with SIGNALS_FILE.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))

    repaired_rows: List[List[str]] = [EXPECTED_HEADER]
    rejected_rows: List[List[str]] = []
    repaired_quality = 0
    broken_rows = 0

    for line_no, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        if row == EXPECTED_HEADER or (row and row[0] == "timestamp"):
            broken_rows += 1
            rejected_rows.append(["signals_v3.csv", str(line_no), "embedded_header", *row])
            continue
        if len(row) != len(EXPECTED_HEADER):
            broken_rows += 1
            rejected_rows.append(["signals_v3.csv", str(line_no), "broken_length", *row])
            continue

        quality = row[6]
        normalized = normalize_quality(row[3], quality)
        if normalized != quality:
            row[6] = normalized
            repaired_quality += 1
        repaired_rows.append(row)

    with SIGNALS_FILE.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerows(repaired_rows)

    write_mode = "a" if REJECTED_FILE.exists() and REJECTED_FILE.stat().st_size > 0 else "w"
    with REJECTED_FILE.open(write_mode, newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        if write_mode == "w":
            writer.writerow(["source_file", "line_no", "reason", "raw_1", "raw_2", "raw_3", "raw_4", "raw_5", "raw_6", "raw_7", "raw_8", "raw_9", "raw_10", "raw_11", "raw_12", "raw_13", "raw_14", "raw_15", "raw_16", "raw_17", "raw_18"])
        writer.writerows(rejected_rows)

    return {
        "broken_rows_moved": broken_rows,
        "quality_fixed": repaired_quality,
        "kept_rows": len(repaired_rows) - 1,
    }


def explain_count_mismatch() -> dict:
    """Explain signals/debug mismatch using overlap window."""
    with SIGNALS_FILE.open("r", newline="", encoding="utf-8") as file:
        signals = [row for row in csv.DictReader(file) if row and any(row.values()) and row.get("timestamp") != "timestamp"]
    with DEBUG_FILE.open("r", newline="", encoding="utf-8") as file:
        debug = [row for row in csv.DictReader(file) if row and any(row.values()) and row.get("timestamp") != "timestamp"]

    first_debug_ts = debug[0]["timestamp"] if debug else ""
    overlap_signals = [row for row in signals if row.get("timestamp", "") >= first_debug_ts] if first_debug_ts else signals
    return {
        "signals_total": len(signals),
        "decision_debug_total": len(debug),
        "first_debug_timestamp": first_debug_ts,
        "signals_from_debug_start": len(overlap_signals),
        "difference_in_overlap_window": len(overlap_signals) - len(debug),
    }


def main() -> None:
    create_backups()
    repair_stats = repair_signals()
    mismatch = explain_count_mismatch()
    print("Data Repair")
    print(f"Broken rows moved : {repair_stats['broken_rows_moved']}")
    print(f"Quality fixed     : {repair_stats['quality_fixed']}")
    print(f"Signals kept      : {repair_stats['kept_rows']}")
    print(f"Signals total     : {mismatch['signals_total']}")
    print(f"Debug total       : {mismatch['decision_debug_total']}")
    print(f"Overlap diff      : {mismatch['difference_in_overlap_window']}")


if __name__ == "__main__":
    main()
