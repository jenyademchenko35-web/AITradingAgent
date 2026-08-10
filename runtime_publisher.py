"""Fail-open standalone publisher for the Railway Mini App runtime bridge.

Run this process separately from the trading agent.  It reads only already
published JSON reports and never imports the decision, risk, portfolio, or
execution layers.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from urllib import error, request

from dotenv import load_dotenv

from runtime_contract import read_runtime_snapshot


LOGGER = logging.getLogger("runtime_publisher")
MAX_REPORT_BYTES = 1_048_576


@dataclass(frozen=True)
class RuntimePublisherSettings:
    url: str = ""
    secret: str = ""
    interval_seconds: int = 30
    timeout_seconds: float = 10.0
    max_retries: int = 3
    base_dir: Path = Path(".")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, base_dir: Path | None = None) -> "RuntimePublisherSettings":
        root = base_dir or Path(__file__).resolve().parent
        if environ is None:
            load_dotenv(root / ".env", override=False)
        env = os.environ if environ is None else environ
        def integer(name: str, default: int, minimum: int = 1) -> int:
            try:
                return max(minimum, int(env.get(name, default)))
            except (TypeError, ValueError):
                return default
        try:
            timeout = max(0.1, float(env.get("RUNTIME_PUBLISH_TIMEOUT_SECONDS", "10")))
        except (TypeError, ValueError):
            timeout = 10.0
        return cls(
            url=str(env.get("RUNTIME_INGEST_URL") or "").strip(),
            secret=str(env.get("RUNTIME_INGEST_SECRET") or ""),
            interval_seconds=integer("RUNTIME_PUBLISH_INTERVAL_SECONDS", 30),
            timeout_seconds=timeout,
            max_retries=integer("RUNTIME_PUBLISH_MAX_RETRIES", 3, 0),
            base_dir=Path(root),
        )


def _read_json(path: Path) -> Any | None:
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def build_runtime_bundle(base_dir: str | Path) -> dict[str, Any] | None:
    """Build a bounded transport object from reports already produced by observers."""
    root = Path(base_dir)
    snapshot = read_runtime_snapshot(root / "runtime_snapshot.json")
    if snapshot is None:
        return None
    bundle: dict[str, Any] = {"runtime_snapshot": snapshot}
    reports = {
        "scenario_report": "scenario_report.json",
        "signal_evaluation_report": "signal_evaluation_report.json",
        "scenario_changes": "scenario_changes.json",
        "research_summary": "research_lab_v2_status.json",
        "impulse_summary": "impulse_probability.json",
    }
    for field, filename in reports.items():
        report = _read_json(root / filename)
        if report is not None:
            bundle[field] = report
    return bundle


def _log(event: str, **fields: Any) -> None:
    """Never include endpoint credentials or request payloads in publisher logs."""
    LOGGER.info("runtime_publish event=%s %s", event, " ".join(f"{key}={value}" for key, value in fields.items()))


def publish_once(
    settings: RuntimePublisherSettings,
    *,
    opener: Callable[..., Any] = request.urlopen,
) -> bool:
    bundle = build_runtime_bundle(settings.base_dir)
    snapshot_id = str(((bundle or {}).get("runtime_snapshot") or {}).get("snapshot_id") or "UNKNOWN")
    if bundle is None:
        _log("skipped", reason="runtime_snapshot_unavailable")
        return False
    if not settings.url or not settings.secret:
        _log("skipped", snapshot_id=snapshot_id, reason="publisher_not_configured")
        return False
    encoded = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    outgoing = request.Request(
        settings.url, data=encoded, method="POST",
        headers={"Authorization": f"Bearer {settings.secret}", "Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with opener(outgoing, timeout=settings.timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
    except error.HTTPError as exc:
        status = int(exc.code)
    except (OSError, ValueError) as exc:
        _log("failed", snapshot_id=snapshot_id, error_type=type(exc).__name__, latency_ms=int((time.monotonic() - started) * 1000))
        return False
    latency_ms = int((time.monotonic() - started) * 1000)
    if 200 <= status < 300 or status == 409:
        _log("success", snapshot_id=snapshot_id, http_status=status, latency_ms=latency_ms)
        return True
    _log("failed", snapshot_id=snapshot_id, http_status=status, latency_ms=latency_ms)
    return False


def publish_with_retry(
    settings: RuntimePublisherSettings,
    *,
    opener: Callable[..., Any] = request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    for attempt in range(settings.max_retries + 1):
        if publish_once(settings, opener=opener):
            return True
        if attempt < settings.max_retries:
            delay = min(60, 2 ** attempt)
            _log("retry", attempt=attempt + 1, delay_seconds=delay)
            sleep(delay)
    return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = RuntimePublisherSettings.from_env()
    while True:
        try:
            publish_with_retry(settings)
        except Exception as exc:  # Final fail-open boundary for this separate process.
            _log("unexpected_error", error_type=type(exc).__name__)
        time.sleep(settings.interval_seconds)


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
