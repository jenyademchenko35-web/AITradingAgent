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
MAX_ERROR_RESPONSE_CHARS = 300
MAX_ERROR_RESPONSE_BYTES = 8_192
_SAFE_ERROR_FIELDS = ("error", "detail", "code", "reason")


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


def _system_summary(root: Path) -> dict[str, Any] | None:
    """Transport only whitelisted operational status, never arbitrary runtime JSON."""
    dashboard = _read_json(root / "dashboard_state.json")
    monitor = _read_json(root / "live_monitor_state.json")
    agent = _read_json(root / "agent_v3_stats.json")
    dashboard = dashboard if isinstance(dashboard, Mapping) else {}
    monitor = monitor if isinstance(monitor, Mapping) else {}
    agent = agent if isinstance(agent, Mapping) else {}
    system = dashboard.get("system") if isinstance(dashboard.get("system"), Mapping) else {}
    trading = dashboard.get("trading") if isinstance(dashboard.get("trading"), Mapping) else {}
    telegram = dashboard.get("telegram") if isinstance(dashboard.get("telegram"), Mapping) else {}
    news = dashboard.get("news") if isinstance(dashboard.get("news"), Mapping) else {}
    live = dashboard.get("live_monitor") if isinstance(dashboard.get("live_monitor"), Mapping) else {}
    payload = {
        "generated_at": dashboard.get("generated_at") or monitor.get("generated_at"),
        "server": system.get("status") or monitor.get("status"),
        "telegram": telegram.get("status"), "news": news.get("status"),
        "cycle": trading.get("last_cycle") or trading.get("cycle") or monitor.get("current_cycle") or monitor.get("last_cycle") or agent.get("runs"),
        "interval_seconds": monitor.get("interval") or live.get("interval") or agent.get("interval_seconds"),
        "next_cycle_seconds": trading.get("next_cycle_seconds") or monitor.get("next_cycle_seconds") or agent.get("next_cycle_seconds"),
        "uptime_seconds": system.get("uptime_seconds") or trading.get("uptime_seconds") or monitor.get("uptime_seconds") or agent.get("uptime_seconds"),
    }
    return payload if any(value not in (None, "") for value in payload.values()) else None


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
        "research_integrity": "research_data_integrity.json",
    }
    for field, filename in reports.items():
        report = _read_json(root / filename)
        if report is not None:
            bundle[field] = report
    system = _system_summary(root)
    if system is not None:
        bundle["system_summary"] = system
    return bundle


def _log(event: str, **fields: Any) -> None:
    """Never include endpoint credentials or request payloads in publisher logs."""
    LOGGER.info("runtime_publish event=%s %s", event, " ".join(f"{key}={value}" for key, value in fields.items()))


def _safe_response_text(value: Any, *, secrets: tuple[str, ...]) -> str:
    """Bound an untrusted response and remove known credentials/payloads."""
    text = " ".join(str(value or "").split())
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:MAX_ERROR_RESPONSE_CHARS]


def _ingest_error_diagnostics(response: Any, *, secret: str, payload_text: str) -> dict[str, str]:
    """Extract a minimal, non-sensitive error explanation from a failed response."""
    try:
        raw = response.read(MAX_ERROR_RESPONSE_BYTES)
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
    except (OSError, ValueError, TypeError, AttributeError):
        return {"response_body": "unavailable"}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        # Never emit an apparent request echo, even in a non-JSON error page.
        if "runtime_snapshot" in text or payload_text in text:
            return {"response_body": "payload_like_response_omitted"}
        return {"response_body": _safe_response_text(text, secrets=(secret, payload_text)) or "unavailable"}
    if not isinstance(parsed, Mapping):
        return {"response_body": "json_non_object"}
    details: dict[str, str] = {}
    for field in _SAFE_ERROR_FIELDS:
        value = parsed.get(field)
        if isinstance(value, (str, int, float, bool)) and not isinstance(value, bytes):
            details[field] = _safe_response_text(value, secrets=(secret, payload_text)) or "unavailable"
    if details:
        return details
    if any(field in parsed for field in ("runtime_snapshot", "scenario_report", "signal_evaluation_report")):
        return {"response_body": "payload_like_response_omitted"}
    return {"response_body": "json_without_safe_error_fields"}


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
    payload_text = encoded.decode("utf-8")
    outgoing = request.Request(
        settings.url, data=encoded, method="POST",
        headers={"Authorization": f"Bearer {settings.secret}", "Content-Type": "application/json"},
    )
    started = time.monotonic()
    diagnostics: dict[str, str] = {}
    try:
        with opener(outgoing, timeout=settings.timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            if status >= 400:
                diagnostics = _ingest_error_diagnostics(
                    response, secret=settings.secret, payload_text=payload_text,
                )
    except error.HTTPError as exc:
        status = int(exc.code)
        diagnostics = _ingest_error_diagnostics(
            exc, secret=settings.secret, payload_text=payload_text,
        )
    except (OSError, ValueError) as exc:
        _log("failed", snapshot_id=snapshot_id, error_type=type(exc).__name__, latency_ms=int((time.monotonic() - started) * 1000))
        return False
    latency_ms = int((time.monotonic() - started) * 1000)
    if 200 <= status < 300 or status == 409:
        _log("success", snapshot_id=snapshot_id, http_status=status, latency_ms=latency_ms)
        return True
    _log("failed", snapshot_id=snapshot_id, http_status=status, latency_ms=latency_ms, **diagnostics)
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
