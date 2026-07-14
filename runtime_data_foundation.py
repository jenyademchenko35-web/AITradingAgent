"""Diagnostic report for Runtime Data Foundation v1.

The utility performs header-only checks against existing hot CSV files. It
never appends to or rewrites trading data during a normal diagnostic run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from report_metadata import build_report_metadata
from research_consensus.consensus_loader import ConsensusLoader
from runtime_csv import (
    ensure_header,
    get_runtime_csv_stats,
    read_header,
    reset_runtime_csv_state,
)


BASE_DIR = Path(__file__).resolve().parent
REPORT_PATH = BASE_DIR / "runtime_data_foundation_report.json"
SUMMARY_PATH = BASE_DIR / "runtime_data_foundation_summary.txt"
HOT_CSV_NAMES = (
    "signals_v3.csv",
    "decision_debug.csv",
    "decision_diagnostics.csv",
    "decision_explanations.csv",
    "setup_history_v3.csv",
)


def utc_now() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def inspect_hot_csv_files() -> list[dict[str, Any]]:
    """Exercise the header cache twice without touching any data rows."""
    reset_runtime_csv_state()
    rows = []
    for name in HOT_CSV_NAMES:
        path = BASE_DIR / name
        header = read_header(path, track_metrics=False)
        if header:
            first = ensure_header(path, header, allow_migration=False)
            second = ensure_header(path, header, allow_migration=False)
            check_status = "OK"
            cache_hit = second.cached
        else:
            first = None
            check_status = "MISSING_OR_EMPTY"
            cache_hit = False
        rows.append({
            "file": name,
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else 0,
            "header_columns": len(header),
            "header_status": check_status,
            "first_check_cached": bool(first and first.cached),
            "second_check_cached": cache_hit,
        })
    return rows


def run_tests() -> dict[str, Any]:
    """Run the focused unittest suite and capture a compact result."""
    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-v",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "ERROR",
            "return_code": -1,
            "summary": f"Тесты не завершены: {type(exc).__name__}",
        }
    combined = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    tail = combined.splitlines()[-12:]
    return {
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "return_code": completed.returncode,
        "summary": next(
            (line for line in reversed(tail) if line.startswith("Ran ")),
            "Результат unittest доступен в output_tail",
        ),
        "output_tail": tail,
    }


def build_report(*, execute_tests: bool = True) -> dict[str, Any]:
    """Build and persist the runtime-data diagnostic report."""
    schema_started = time.perf_counter()
    hot_files = inspect_hot_csv_files()
    schema_elapsed_ms = round((time.perf_counter() - schema_started) * 1000, 3)
    csv_stats = get_runtime_csv_stats()
    loader = ConsensusLoader(BASE_DIR)
    report_groups = loader.report_groups()
    test_results = run_tests() if execute_tests else {
        "status": "NOT_RUN",
        "return_code": None,
        "summary": "Тесты пропущены параметром --skip-tests",
    }
    generated_at = utc_now()
    source_paths = [
        BASE_DIR / row["file"]
        for row in hot_files
        if row["exists"]
    ]
    legacy_full_read_estimate = sum(
        row["size_bytes"] for row in hot_files
    ) * 2
    total_bytes_saved_estimate = max(
        legacy_full_read_estimate - csv_stats.get("bytes_read_for_schema", 0),
        0,
    )
    status = "OK"
    if test_results["status"] == "FAIL":
        status = "ERROR"
    elif test_results["status"] in {"ERROR", "NOT_RUN"}:
        status = "WARNING"

    report = {
        "generated_at": generated_at,
        "metadata": build_report_metadata(
            generator="runtime_data_foundation",
            metric_unit="BYTES",
            source_files=source_paths,
            base_dir=BASE_DIR,
            closed_trades_total=loader.canonical_metrics.get("closed_trades", 0),
            complete_metrics_total=loader.canonical_metrics.get("metrics_trades", 0),
            generated_at=generated_at,
        ),
        "status": status,
        "mode": "read-only diagnostics",
        "hot_csv_files": hot_files,
        "schema_runtime": csv_stats,
        "io_savings": {
            "schema_check_elapsed_ms": schema_elapsed_ms,
            "bytes_read_for_schema": csv_stats.get("bytes_read_for_schema", 0),
            "cache_hit_bytes_saved_estimate": csv_stats.get(
                "bytes_saved_estimate", 0
            ),
            "legacy_full_read_estimate": legacy_full_read_estimate,
            "total_bytes_saved_estimate": total_bytes_saved_estimate,
            "note": (
                "Legacy estimate моделирует два полных schema-read для каждого "
                "горячего CSV; новые проверки читают header один раз и затем cache."
            ),
        },
        "research_reports": report_groups,
        "incompatible_metric_units": loader.incompatible_metric_units(),
        "tests": test_results,
        "restrictions": [
            "Торговая логика и DecisionEngine не вызывались.",
            "Runtime CSV проверялись только по header.",
            "Активные сделки и торговые данные не изменялись.",
        ],
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    SUMMARY_PATH.write_text(format_summary(report), encoding="utf-8")
    return report


def format_summary(report: Mapping[str, Any]) -> str:
    """Format a concise Russian diagnostic summary."""
    runtime = report.get("schema_runtime", {})
    research = report.get("research_reports", {})
    savings = report.get("io_savings", {})
    lines = [
        "====================================",
        "Runtime Data Foundation v1",
        "====================================",
        f"Статус: {report.get('status', 'N/A')}",
        f"Горячих CSV: {len(report.get('hot_csv_files', []))}",
        f"Schema checks: {runtime.get('schema_checks', 0)}",
        f"Schema cache hits: {runtime.get('schema_cache_hits', 0)}",
        f"Schema migrations: {runtime.get('schema_migrations', 0)}",
        f"Прочитано для schema: {runtime.get('bytes_read_for_schema', 0)} байт",
        f"Время schema smoke: {savings.get('schema_check_elapsed_ms', 0)} мс",
        f"Legacy read estimate: {savings.get('legacy_full_read_estimate', 0)} байт",
        f"Оценка сохранённого чтения: {savings.get('total_bytes_saved_estimate', 0)} байт",
        "",
        "Research freshness:",
        f"Принято: {len(research.get('accepted_reports', []))}",
        f"STALE: {len(research.get('stale_reports', []))}",
        f"Несовместимо: {len(research.get('incompatible_reports', []))}",
        f"Отсутствует: {len(research.get('missing_reports', []))}",
        f"Несовместимых metric units: {len(report.get('incompatible_metric_units', []))}",
        "",
        f"Тесты: {report.get('tests', {}).get('status', 'NOT_RUN')}",
        str(report.get("tests", {}).get("summary", "")),
        "",
        "Live-логика не изменялась.",
    ]
    return "\n".join(lines)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Не запускать unittest при построении отчёта.",
    )
    args = parser.parse_args()
    report = build_report(execute_tests=not args.skip_tests)
    print(format_summary(report))


if __name__ == "__main__":
    main()
