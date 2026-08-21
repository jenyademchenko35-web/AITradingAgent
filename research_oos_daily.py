"""Daily research-only orchestration for validated Crypto and FX OOS tools."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from fx_research.oos_data_collector import OOSCollectionError, collect
from research_oos_checkpoint import build_checkpoint

Collector = Callable[..., dict[str, Any]]
CheckpointBuilder = Callable[..., dict[str, Any]]
REPORT_RETENTION_DEFAULT = 30
REPORT_RETENTION_MAX = 365
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUPS = 3
ERROR_LIMIT = 500
SUCCESS_COLLECTION_STATUSES = {"PUBLISHED", "NO_NEW_CLOSED_CANDLES", "DRY_RUN"}


def _bounded(value: Any, limit: int = ERROR_LIMIT) -> str:
    text = " ".join(str(value).split())
    return text[:limit]


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically publish one strict JSON report at the explicitly scoped path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"oos-research-daily:{path.resolve()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
        formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s")
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


@contextmanager
def _exclusive_lock(path: Path):
    """Yield False when another process owns the stable non-blocking lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        acquired = False
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _symbol_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "status", "new_rows", "previous_rows", "final_rows",
        "previous_last_timestamp", "new_last_timestamp",
        "previous_sha256", "final_sha256",
    )
    return {key: item.get(key) for key in allowed if key in item}


def _collection_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    symbols = report.get("symbols")
    oos = report.get("oos")
    return {
        "status": report.get("status"),
        "dry_run": bool(report.get("dry_run")),
        "symbols": {
            str(symbol): _symbol_summary(item)
            for symbol, item in symbols.items() if isinstance(item, Mapping)
        } if isinstance(symbols, Mapping) else {},
        "fx_validation": {
            "overall": oos.get("overall"),
            "integrity": oos.get("integrity"),
        } if isinstance(oos, Mapping) else None,
    }


def _collection_is_safe(report: Mapping[str, Any]) -> bool:
    if report.get("status") not in SUCCESS_COLLECTION_STATUSES:
        return False
    oos = report.get("oos")
    if not isinstance(oos, Mapping):
        return True
    integrity = oos.get("integrity")
    return (
        oos.get("overall") != "DATA_INVALID"
        and not (isinstance(integrity, Mapping) and integrity.get("valid") is False)
    )


def _retain_reports(history_dir: Path, retention: int) -> None:
    keep = max(1, min(int(retention), REPORT_RETENTION_MAX))
    reports = sorted(path for path in history_dir.glob("*.json") if path.is_file())
    for obsolete in reports[:-keep]:
        obsolete.unlink()


def _persist_report(report: Mapping[str, Any], reports_dir: Path, retention: int) -> None:
    generated = datetime.fromisoformat(str(report["generated_at"]).replace("Z", "+00:00"))
    stamp = generated.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    history_dir = reports_dir / "history"
    _atomic_json(history_dir / f"{stamp}.json", report)
    _atomic_json(reports_dir / "latest.json", report)
    _retain_reports(history_dir, retention)


def _base_report(now: datetime, *, dry_run: bool) -> dict[str, Any]:
    return {
        "schema_version": "OOS_RESEARCH_AUTOMATION_V1",
        "generated_at": now.astimezone(UTC).isoformat(),
        "dry_run": dry_run,
        "status": "STARTED",
        "collection": None,
        "checkpoint": None,
        "error": None,
    }


def orchestrate(
    *,
    crypto_db: str | Path,
    crypto_history: str | Path,
    eurusd_path: str | Path,
    gbpusd_path: str | Path,
    staging_dir: str | Path,
    reports_dir: str | Path,
    log_file: str | Path,
    lock_file: str | Path,
    dukascopy_command: Sequence[str],
    retention: int = REPORT_RETENTION_DEFAULT,
    dry_run: bool = False,
    now: datetime | None = None,
    collector: Collector = collect,
    checkpoint_builder: CheckpointBuilder = build_checkpoint,
) -> tuple[int, dict[str, Any]]:
    """Run collection then checkpoint under one lock; never call trading code."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    logger = _logger(Path(log_file))
    with _exclusive_lock(Path(lock_file)) as acquired:
        if not acquired:
            report = _base_report(current, dry_run=dry_run)
            report["status"] = "ALREADY_RUNNING"
            logger.info("status=ALREADY_RUNNING")
            return 0, report

        report = _base_report(current, dry_run=dry_run)
        try:
            collection = collector(
                eurusd_path=eurusd_path,
                gbpusd_path=gbpusd_path,
                staging_dir=staging_dir,
                dry_run=dry_run,
                now=current,
                dukascopy_command=tuple(dukascopy_command),
            )
            report["collection"] = _collection_summary(collection)
        except (OOSCollectionError, OSError, ValueError, RuntimeError) as exc:
            report["status"] = "COLLECTION_FAILED"
            report["error"] = _bounded(exc)
            _persist_report(report, Path(reports_dir), retention)
            logger.error("status=COLLECTION_FAILED error=%s", report["error"])
            return 2, report

        if not _collection_is_safe(collection):
            report["status"] = "COLLECTION_INTEGRITY_FAILED"
            report["error"] = "collector returned an invalid or unsafe integrity state"
            _persist_report(report, Path(reports_dir), retention)
            logger.error("status=COLLECTION_INTEGRITY_FAILED")
            return 3, report

        try:
            checkpoint = checkpoint_builder(
                crypto_db=crypto_db,
                crypto_history=crypto_history,
                eurusd_path=eurusd_path,
                gbpusd_path=gbpusd_path,
            )
            report["checkpoint"] = checkpoint
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            report["status"] = "CHECKPOINT_FAILED"
            report["error"] = _bounded(exc)
            _persist_report(report, Path(reports_dir), retention)
            logger.error("status=CHECKPOINT_FAILED error=%s", report["error"])
            return 4, report

        integrity = checkpoint.get("checkpoint", {}).get("integrity")
        report["status"] = "SUCCESS" if integrity == "PASS" else "CHECKPOINT_DEGRADED"
        _persist_report(report, Path(reports_dir), retention)
        logger.info(
            "status=%s collection=%s checkpoint_action=%s",
            report["status"], collection.get("status"),
            checkpoint.get("checkpoint", {}).get("action"),
        )
        return (0 if report["status"] == "SUCCESS" else 5), report


def _print(report: Mapping[str, Any], reports_dir: Path) -> None:
    print("OOS RESEARCH AUTOMATION")
    print(f"Status: {report['status']}")
    collection = report.get("collection")
    if isinstance(collection, Mapping):
        print(f"Collection: {collection.get('status')}")
    checkpoint = report.get("checkpoint")
    if isinstance(checkpoint, Mapping):
        print(f"Checkpoint: {checkpoint.get('checkpoint', {}).get('action')}")
    if report.get("error"):
        print(f"Error: {report['error']}")
    print(f"Report: {reports_dir / 'latest.json'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crypto-db", type=Path, default=Path("research.db"))
    parser.add_argument("--crypto-history", type=Path, default=Path("research_lab_shadow_history.csv"))
    parser.add_argument("--eurusd", type=Path, default=Path("data/fx/canonical/EURUSD_1h.csv"))
    parser.add_argument("--gbpusd", type=Path, default=Path("data/fx/canonical/GBPUSD_1h.csv"))
    parser.add_argument("--staging-dir", type=Path, default=Path("data/fx/oos_staging"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports/oos"))
    parser.add_argument("--log-file", type=Path, default=Path("logs/oos_research_daily.log"))
    parser.add_argument("--lock-file", type=Path, default=Path("reports/oos/.daily.lock"))
    parser.add_argument("--retention", type=int, default=REPORT_RETENTION_DEFAULT)
    parser.add_argument("--dukascopy-npx", type=Path, default=os.environ.get("OOS_NPX"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dukascopy_npx is None:
        parser.error("--dukascopy-npx or OOS_NPX is required")
    npx = args.dukascopy_npx.expanduser()
    if not npx.is_absolute() or not npx.is_file() or not os.access(npx, os.X_OK):
        parser.error("dukascopy npx path must be an existing absolute executable")
    if not 1 <= args.retention <= REPORT_RETENTION_MAX:
        parser.error(f"--retention must be between 1 and {REPORT_RETENTION_MAX}")
    code, report = orchestrate(
        crypto_db=args.crypto_db,
        crypto_history=args.crypto_history,
        eurusd_path=args.eurusd,
        gbpusd_path=args.gbpusd,
        staging_dir=args.staging_dir,
        reports_dir=args.reports_dir,
        log_file=args.log_file,
        lock_file=args.lock_file,
        dukascopy_command=(str(npx), "--no-install", "dukascopy-node"),
        retention=args.retention,
        dry_run=args.dry_run,
    )
    _print(report, args.reports_dir)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
