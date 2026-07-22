"""Data Quality Check for AITradingAgent v1.0."""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from research_data_quality import ResearchDataQuality


BASE_DIR = Path(__file__).resolve().parent

FILES = {
    "signals": BASE_DIR / "signals_v3.csv",
    "decision_debug": BASE_DIR / "decision_debug.csv",
    "decision_diagnostics": BASE_DIR / "decision_diagnostics.csv",
    "decision_explanations": BASE_DIR / "decision_explanations.csv",
    "trades": BASE_DIR / "trades.csv",
    "setup_history": BASE_DIR / "setup_history_v3.csv",
    "stats": BASE_DIR / "agent_v3_stats.json",
    "strategy_weights": BASE_DIR / "strategy_weights.json",
    "candidate_weights": BASE_DIR / "candidate_weights.json",
    "auto_learning": BASE_DIR / "auto_learning_recommendation.json",
}

CSV_HEADERS = {
    "signals": [
        "timestamp", "symbol", "direction", "signal", "score", "confidence",
        "quality", "trend_long", "trend_short", "structure_long",
        "structure_short", "momentum_long", "momentum_short", "risk_long",
        "risk_short", "long_total", "short_total", "summary",
    ],
    "decision_debug": [
        "timestamp", "symbol", "direction", "signal", "score", "confidence",
        "quality", "trend_long", "trend_short", "structure_long",
        "structure_short", "momentum_long", "momentum_short", "risk_long",
        "risk_short", "long_total", "short_total", "diff", "winner",
        "trend_reason", "structure_reason", "momentum_reason", "risk_reason",
        "summary",
    ],
    "decision_diagnostics": [
        "timestamp", "symbol", "decision", "trend", "structure", "momentum",
        "risk", "primary_blocker", "lost_score", "potential_score",
    ],
    "decision_explanations": [
        "timestamp", "symbol", "decision", "quality", "score", "confidence",
        "passed", "failed", "reasons", "summary",
    ],
    "trades": [
        "symbol", "direction", "entry", "stop_loss", "take_profit", "status",
        "result", "opened_at", "closed_at", "exit_price", "pnl",
    ],
    "setup_history": [
        "timestamp", "symbol", "direction", "signal", "score", "confidence",
        "quality", "long_total", "short_total", "summary",
    ],
}

IMPORTANT_COLUMNS = {
    "signals": ["timestamp", "symbol", "direction", "signal", "score", "confidence"],
    "decision_debug": ["timestamp", "symbol", "direction", "signal", "score", "confidence"],
    "decision_diagnostics": ["timestamp", "symbol", "decision", "primary_blocker"],
    "decision_explanations": ["timestamp", "symbol", "decision", "summary"],
    "trades": ["symbol", "direction", "status", "opened_at"],
    "setup_history": ["timestamp", "symbol", "direction", "signal"],
}

VALID_SIGNALS = {"NO TRADE", "WAIT", "WATCH", "SETUP", "HIGH PRIORITY"}
VALID_DIRECTIONS = {"LONG", "SHORT", "NEUTRAL", "WAIT"}
VALID_QUALITIES = {"A", "B", "C", "D", "E", ""}

JSON_OUTPUT = BASE_DIR / "data_quality_report.json"
TEXT_OUTPUT = BASE_DIR / "data_quality_summary.txt"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def read_csv_raw(path: Path) -> List[List[str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        return list(csv.reader(file))


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    clean = []
    for row in rows:
        if not row or not any(row.values()):
            continue
        if row.get("timestamp") == "timestamp":
            continue
        clean.append(row)
    return clean


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def add_issue(issues: List[Dict[str, str]], severity: str, file_key: str, message: str) -> None:
    issues.append({"severity": severity, "file": file_key, "message": message})


def check_file_presence(issues: List[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
    file_status: Dict[str, Dict[str, Any]] = {}
    for key, path in FILES.items():
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        file_status[key] = {"path": str(path), "exists": exists, "size": size}
        if not exists:
            add_issue(issues, "ERROR", key, "Файл отсутствует.")
        elif size == 0:
            add_issue(issues, "ERROR", key, "Файл пустой.")
    return file_status


def check_csv_schema(file_key: str, issues: List[Dict[str, str]]) -> Dict[str, Any]:
    path = FILES[file_key]
    raw = read_csv_raw(path)
    rows = read_csv_rows(path)
    expected = CSV_HEADERS[file_key]
    result = {
        "row_count": len(rows),
        "header_ok": False,
        "broken_rows": 0,
        "missing_required": {},
    }
    if not raw:
        return result
    header = raw[0]
    result["header_ok"] = header == expected
    if header != expected:
        add_issue(
            issues,
            "WARNING",
            file_key,
            f"Заголовок отличается от ожидаемого: {header}",
        )

    expected_len = len(header)
    broken_rows = 0
    for row in raw[1:]:
        if row and len(row) != expected_len:
            broken_rows += 1
    result["broken_rows"] = broken_rows
    if broken_rows:
        add_issue(
            issues,
            "WARNING",
            file_key,
            f"Найдено битых строк: {broken_rows}.",
        )

    for column in IMPORTANT_COLUMNS[file_key]:
        missing = sum(1 for row in rows if not str(row.get(column, "")).strip())
        result["missing_required"][column] = missing
        if missing:
            add_issue(
                issues,
                "WARNING",
                file_key,
                f"Пропуски в важной колонке {column}: {missing}.",
            )
    return result


def check_signal_values(rows: Sequence[Mapping[str, str]], file_key: str, issues: List[Dict[str, str]]) -> Dict[str, Any]:
    invalid_signals = sum(1 for row in rows if row.get("signal") not in VALID_SIGNALS)
    invalid_directions = sum(1 for row in rows if row.get("direction") not in VALID_DIRECTIONS)
    invalid_qualities = sum(1 for row in rows if row.get("quality", "") not in VALID_QUALITIES)
    invalid_score = sum(1 for row in rows if safe_float(row.get("score"), -1) < 0)
    invalid_confidence = sum(
        1 for row in rows
        if safe_float(row.get("confidence"), -1) < 0 or safe_float(row.get("confidence"), 101) > 100
    )
    duplicates = len(rows) - len({
        (
            row.get("timestamp", ""),
            row.get("symbol", ""),
            row.get("direction", ""),
            row.get("signal", ""),
        )
        for row in rows
    })
    if invalid_signals:
        add_issue(issues, "ERROR", file_key, f"Некорректные signal: {invalid_signals}.")
    if invalid_directions:
        add_issue(issues, "ERROR", file_key, f"Некорректные direction: {invalid_directions}.")
    if invalid_qualities:
        add_issue(issues, "WARNING", file_key, f"Некорректные quality: {invalid_qualities}.")
    if invalid_score:
        add_issue(issues, "ERROR", file_key, f"Отрицательные или невозможные score: {invalid_score}.")
    if invalid_confidence:
        add_issue(issues, "ERROR", file_key, f"Некорректные confidence: {invalid_confidence}.")
    if duplicates:
        add_issue(issues, "WARNING", file_key, f"Дубли сигналов: {duplicates}.")
    return {
        "invalid_signals": invalid_signals,
        "invalid_directions": invalid_directions,
        "invalid_qualities": invalid_qualities,
        "invalid_score": invalid_score,
        "invalid_confidence": invalid_confidence,
        "duplicate_count": duplicates,
    }


def check_weights_payload(file_key: str, issues: List[Dict[str, str]]) -> Dict[str, Any]:
    payload = read_json(FILES[file_key])
    result: Dict[str, Any] = {"entries": {}}
    for name, weights in payload.items():
        if name == "metadata" or not isinstance(weights, Mapping):
            continue
        total = round(sum(safe_float(weights.get(k)) for k in ("trend", "structure", "momentum", "risk")), 6)
        status = "VALID" if total == 1.0 else "INVALID"
        result["entries"][name] = {"sum": total, "status": status}
        if status != "VALID":
            add_issue(issues, "ERROR", file_key, f"Невалидная сумма весов для {name}: {total}.")
    return result


def check_json_presence(file_key: str, issues: List[Dict[str, str]]) -> Dict[str, Any]:
    payload = read_json(FILES[file_key])
    if not payload:
        add_issue(issues, "ERROR", file_key, "JSON пустой или повреждён.")
    return {"keys": sorted(payload.keys()) if payload else []}


def build_report() -> Dict[str, Any]:
    issues: List[Dict[str, str]] = []
    file_status = check_file_presence(issues)

    csv_checks: Dict[str, Any] = {}
    for file_key in CSV_HEADERS:
        csv_checks[file_key] = check_csv_schema(file_key, issues)

    signals_rows = read_csv_rows(FILES["signals"])
    debug_rows = read_csv_rows(FILES["decision_debug"])
    diagnostics_rows = read_csv_rows(FILES["decision_diagnostics"])
    explanations_rows = read_csv_rows(FILES["decision_explanations"])
    setup_rows = read_csv_rows(FILES["setup_history"])

    value_checks = {
        "signals": check_signal_values(signals_rows, "signals", issues),
        "decision_debug": check_signal_values(debug_rows, "decision_debug", issues),
        "setup_history": check_signal_values(setup_rows, "setup_history", issues),
    }

    counts = {
        "signals": len(signals_rows),
        "decision_debug": len(debug_rows),
        "decision_diagnostics": len(diagnostics_rows),
        "decision_explanations": len(explanations_rows),
    }

    overlap_counts = {
        "signals_from_debug_start": 0,
        "decision_debug": counts["decision_debug"],
        "difference": 0,
    }
    if debug_rows:
        first_debug_time = parse_time(debug_rows[0].get("timestamp", ""))
        overlap_signals = [
            row for row in signals_rows
            if first_debug_time is not None
            and (parse_time(row.get("timestamp", "")) or first_debug_time) >= first_debug_time
        ]
        overlap_counts["signals_from_debug_start"] = len(overlap_signals)
        overlap_counts["difference"] = len(overlap_signals) - counts["decision_debug"]
        if abs(overlap_counts["difference"]) > 1:
            add_issue(
                issues,
                "WARNING",
                "signals/decision_debug",
                "Количество решений не совпадает в общем окне "
                f"debug-логирования: signals={len(overlap_signals)} "
                f"vs debug={counts['decision_debug']}.",
            )
    if counts["decision_diagnostics"] > counts["signals"]:
        add_issue(
            issues,
            "WARNING",
            "decision_diagnostics",
            "diagnostics больше, чем signals, что выглядит подозрительно.",
        )
    if counts["decision_explanations"] > counts["signals"]:
        add_issue(
            issues,
            "WARNING",
            "decision_explanations",
            "explanations больше, чем signals, что выглядит подозрительно.",
        )

    if diagnostics_rows:
        blockers = Counter(row.get("primary_blocker", "") for row in diagnostics_rows)
    else:
        blockers = Counter()

    json_checks = {
        "agent_v3_stats": check_json_presence("stats", issues),
        "strategy_weights": check_weights_payload("strategy_weights", issues),
        "candidate_weights": check_weights_payload("candidate_weights", issues),
        "auto_learning": check_json_presence("auto_learning", issues),
    }

    severities = Counter(issue["severity"] for issue in issues)
    status = "OK"
    if severities.get("ERROR"):
        status = "ERROR"
    elif severities.get("WARNING"):
        status = "WARNING"

    report = {
        "generated_at": utc_now(),
        "status": status,
        "issues_count": len(issues),
        "severity_breakdown": dict(severities),
        "files": file_status,
        "csv_checks": csv_checks,
        "value_checks": value_checks,
        "json_checks": json_checks,
        "decision_counts": counts,
        "overlap_counts": overlap_counts,
        "diagnostics_blockers": dict(blockers),
        "issues": issues,
        "top_issues": issues[:10],
        "what_to_fix": [
            issue["message"] for issue in issues[:8]
        ],
    }
    research = ResearchDataQuality(base_dir=BASE_DIR).build_report(write=False)
    report.update({"research_data_quality": research, "coverage": research["coverage"],
                   "recovered": research["recovered"], "unknown": research["unknown"],
                   "missing": research["missing"]})
    if research["threshold_checks"]:
        report["status"] = "WARNING"
    return report


def save_report(report: Dict[str, Any]) -> None:
    with JSON_OUTPUT.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)


def save_summary(report: Dict[str, Any]) -> None:
    lines = [
        "AITradingAgent Data Quality Check",
        f"Generated at: {report.get('generated_at', '')}",
        "",
        f"Status: {report.get('status', 'N/A')}",
        f"Issues found: {report.get('issues_count', 0)}",
        f"Decision counts: {report.get('decision_counts', {})}",
        "",
        "Top issues:",
    ]
    for issue in report.get("top_issues", [])[:10]:
        lines.append(
            f"- [{issue.get('severity')}] {issue.get('file')}: {issue.get('message')}"
        )
    TEXT_OUTPUT.write_text("\n".join(lines), encoding="utf-8")


def print_summary(report: Dict[str, Any]) -> None:
    print("Data Quality Check")
    print(f"Status          : {report.get('status', 'N/A')}")
    print(f"Issues found    : {report.get('issues_count', 0)}")
    print(f"JSON report     : {JSON_OUTPUT}")
    print(f"TXT summary     : {TEXT_OUTPUT}")


def main() -> None:
    report = build_report()
    save_report(report)
    save_summary(report)
    print_summary(report)


if __name__ == "__main__":
    main()
