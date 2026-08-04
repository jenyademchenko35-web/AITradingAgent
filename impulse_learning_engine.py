"""Observer-only learning reports for published Impulse Probability snapshots."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


BASE_DIR = Path(__file__).resolve().parent
HISTORY = BASE_DIR / "impulse_probability_history.jsonl"
ACCURACY = BASE_DIR / "impulse_accuracy.json"
HEATMAP = BASE_DIR / "market_heatmap.json"
CHANGES = BASE_DIR / "impulse_changes.json"
REPORT = BASE_DIR / "impulse_learning_report.json"
DRIFT = BASE_DIR / "learning_drift.json"
RECOMMENDATIONS = BASE_DIR / "impulse_learning_recommendations.json"
MIN_TRAINING_SAMPLES = 20
RECENT_WINDOW = 20
DRIFT_DEGRADATION_POINTS = 20.0


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return fallback


def _history(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError, TypeError):
        return []


def _outcome(row: Mapping[str, Any]) -> bool | None:
    for key in ("confirmed", "impulse_confirmed", "successful", "is_successful"):
        if isinstance(row.get(key), bool):
            return row[key]
    value = str(row.get("outcome") or "").upper()
    if value in {"SUCCESS", "CONFIRMED", "WIN", "TRUE"}: return True
    if value in {"FAIL", "FAILED", "FALSE", "LOSS"}: return False
    return None


def _rate(successes: int, total: int) -> float | None:
    return round(successes * 100 / total, 2) if total else None


def _bucket(probability: Any) -> str | None:
    try: value = max(0.0, min(100.0, float(probability)))
    except (TypeError, ValueError): return None
    if value < 20: return "0-20"
    if value < 40: return "20-40"
    if value < 60: return "40-60"
    if value < 80: return "60-80"
    return "80-100"


def _feature_learning(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    keys = ("adx", "atr", "volume_ratio", "trend_score", "structure_score", "momentum_score", "risk_score", "edge", "score", "confidence", "market_regime", "failed_filters")
    rows = list(rows); successes = [r for r in rows if _outcome(r) is True]; failures = [r for r in rows if _outcome(r) is False]
    for key in keys:
        def present(row: Mapping[str, Any]) -> bool:
            value = row.get(key)
            return value not in (None, "", [], {})
        count = sum(present(row) for row in rows)
        if count:
            result[key] = {"samples": count, "success_rate": _rate(sum(present(r) for r in successes), len(successes)), "failure_rate": _rate(sum(present(r) for r in failures), len(failures))}
    return result


def _symbol_learning(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows: grouped[str(row.get("symbol") or "UNKNOWN")].append(row)
    output = {}
    for symbol, values in sorted(grouped.items()):
        confirmed = [r for r in values if _outcome(r) is True]
        moves = [float(r["realized_move"]) for r in values if isinstance(r.get("realized_move"), (int, float))]
        buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in values:
            if (bucket := _bucket(row.get("impulse_probability"))) is not None: buckets[bucket].append(row)
        best = max(buckets, key=lambda name: (_rate(sum(_outcome(r) is True for r in buckets[name]), len(buckets[name])) or -1, name)) if buckets else None
        probabilities = [float(r["impulse_probability"]) for r in values if isinstance(r.get("impulse_probability"), (int, float))]
        output[symbol] = {"samples": len(values), "success_rate": _rate(len(confirmed), len(values)), "average_probability": round(sum(probabilities) / len(probabilities), 2) if probabilities else None, "average_realized_move": round(sum(moves) / len(moves), 6) if moves else None, "best_probability_bucket": best}
    return output


def _regime_learning(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows: grouped[str(row.get("market_regime") or "UNKNOWN").upper()].append(row)
    output = {}
    for regime, values in sorted(grouped.items()):
        probabilities = [float(r["impulse_probability"]) for r in values if isinstance(r.get("impulse_probability"), (int, float))]
        output[regime] = {"samples": len(values), "success_rate": _rate(sum(_outcome(r) is True for r in values), len(values)), "average_probability": round(sum(probabilities) / len(probabilities), 2) if probabilities else None}
    return output


def _calibration(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    groups = {name: [] for name in ("0-20", "20-40", "40-60", "60-80", "80-100")}
    for row in rows:
        if (bucket := _bucket(row.get("impulse_probability"))) is not None: groups[bucket].append(row)
    return {name: {"predictions": len(values), "confirmed": sum(_outcome(r) is True for r in values), "confirmation_rate": _rate(sum(_outcome(r) is True for r in values), len(values))} for name, values in groups.items()}


def _drift(rows: list[Mapping[str, Any]], generated_at: str) -> dict[str, Any]:
    if len(rows) < MIN_TRAINING_SAMPLES:
        return {"schema_version": "ipe-learning-drift-v1", "generated_at": generated_at, "status": "INSUFFICIENT_DATA", "historical_success_rate": None, "recent_success_rate": None, "window": RECENT_WINDOW, "limitations": [f"At least {MIN_TRAINING_SAMPLES} confirmed outcomes are required."]}
    recent = rows[-RECENT_WINDOW:]; historical = rows[:-RECENT_WINDOW] or rows
    recent_rate = _rate(sum(_outcome(r) is True for r in recent), len(recent)); historical_rate = _rate(sum(_outcome(r) is True for r in historical), len(historical))
    status = "DRIFT_DETECTED" if historical_rate is not None and recent_rate is not None and historical_rate - recent_rate >= DRIFT_DEGRADATION_POINTS else "STABLE"
    return {"schema_version": "ipe-learning-drift-v1", "generated_at": generated_at, "status": status, "historical_success_rate": historical_rate, "recent_success_rate": recent_rate, "window": len(recent), "limitations": []}


def _recommendations(report: Mapping[str, Any], drift: Mapping[str, Any]) -> dict[str, Any]:
    if report["insufficient_data"]:
        return {"schema_version": "ipe-learning-recommendations-v1", "generated_at": report["generated_at"], "status": "INSUFFICIENT_DATA", "recommendations": []}
    recommendations = []
    for name, values in report["calibration"].items():
        rate = values["confirmation_rate"]
        if values["predictions"] >= 10 and rate is not None and rate < 50:
            recommendations.append({"type": "CALIBRATION_WEAK", "evidence": f"Probability bucket {name} confirmed {rate}% across {values['predictions']} published outcomes."})
    if drift["status"] == "DRIFT_DETECTED": recommendations.append({"type": "DRIFT_DETECTED", "evidence": f"Recent confirmation rate {drift['recent_success_rate']}% is below historical {drift['historical_success_rate']}%."})
    return {"schema_version": "ipe-learning-recommendations-v1", "generated_at": report["generated_at"], "status": "READY", "recommendations": recommendations}


def run_once(base_dir: Path = BASE_DIR) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat()
    history = _history(base_dir / HISTORY.name)
    accuracy = _read_json(base_dir / ACCURACY.name, {})
    heatmap = _read_json(base_dir / HEATMAP.name, [])
    changes = _read_json(base_dir / CHANGES.name, {})
    labeled = [row for row in history if _outcome(row) is not None]
    report = {"schema_version": "ipe-learning-v1", "generated_at": generated_at, "data_quality": {"history_rows": len(history), "heatmap_rows": len(heatmap) if isinstance(heatmap, list) else None, "change_rows": len(changes.get("items", [])) if isinstance(changes, Mapping) and isinstance(changes.get("items"), list) else None, "accuracy_status": accuracy.get("status") if isinstance(accuracy, Mapping) else None, "labeled_samples": len(labeled)}, "training_samples": len(labeled), "pending_samples": len(history) - len(labeled), "successful_predictions": sum(_outcome(row) is True for row in labeled), "failed_predictions": sum(_outcome(row) is False for row in labeled), "insufficient_data": len(labeled) < MIN_TRAINING_SAMPLES, "status": "INSUFFICIENT_DATA" if len(labeled) < MIN_TRAINING_SAMPLES else "READY", "feature_learning": _feature_learning(labeled), "symbol_learning": _symbol_learning(labeled), "regime_learning": _regime_learning(labeled), "calibration": _calibration(labeled)}
    drift = _drift(labeled, generated_at)
    report["learning_drift"] = drift
    recommendations = _recommendations(report, drift)
    for path, payload in ((base_dir / REPORT.name, report), (base_dir / DRIFT.name, drift), (base_dir / RECOMMENDATIONS.name, recommendations)):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return report
