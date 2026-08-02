import json
import os
from pathlib import Path
import time
from datetime import datetime, timezone

from telegram_ui.models import build_signal_fingerprint

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "bot_config.json"
LAST_FILE = BASE_DIR / "last_notification.json"
SIGNAL_COOLDOWN_SECONDS = int(os.getenv("TELEGRAM_SIGNAL_COOLDOWN_SECONDS", "3600"))


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)

def save_last_active_chat_id(chat_id: int) -> None:
    """Remember navigation activity without changing owner or notifications."""
    payload = _read_json(CONFIG_FILE)
    payload["last_active_chat_id"] = int(chat_id)
    _write_json(CONFIG_FILE, payload)


def save_chat_id(chat_id: int) -> None:
    """Backward-compatible alias; /start now updates last-active chat only."""
    save_last_active_chat_id(chat_id)


def load_last_active_chat_id() -> int | None:
    configured = os.getenv("TELEGRAM_LAST_ACTIVE_CHAT_ID")
    value = configured if configured not in (None, "") else _read_json(CONFIG_FILE).get("last_active_chat_id")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def set_notification_chat_id(chat_id: int) -> None:
    """Persist an owner-approved notification destination."""
    payload = _read_json(CONFIG_FILE)
    payload["notification_chat_id"] = int(chat_id)
    _write_json(CONFIG_FILE, payload)


def load_notification_chat_id() -> int | None:
    configured = os.getenv("TELEGRAM_NOTIFICATION_CHAT_ID")
    payload = _read_json(CONFIG_FILE)
    # The legacy chat_id fallback preserves existing deployments until the
    # owner explicitly runs /set_notification_chat or configures the env var.
    value = configured if configured not in (None, "") else payload.get(
        "notification_chat_id", payload.get("chat_id")
    )
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def load_chat_id() -> int | None:
    """Backward-compatible notification destination accessor."""
    return load_notification_chat_id()


def save_last_notification(signal: str) -> None:
    """Backward-compatible wrapper for fingerprint-based state."""
    mark_as_sent(signal)


def last_notification() -> str:
    """Return the last fingerprint, with legacy text-state fallback."""
    payload = _read_json(LAST_FILE)
    return str(payload.get("last_fingerprint") or payload.get("signal") or "")


def is_duplicate(
    signal_fingerprint: str,
    *,
    now: float | None = None,
    cooldown_seconds: int | None = None,
) -> bool:
    """Check a stable setup key inside a separate notification cooldown."""
    payload = _read_json(LAST_FILE)
    fingerprints = payload.get("fingerprints", {})
    if not isinstance(fingerprints, dict):
        fingerprints = {}
    sent_at = fingerprints.get(signal_fingerprint)
    if sent_at is None:
        # Compatibility for the former {"signal": "..."} state.
        return signal_fingerprint == payload.get("signal")
    current = time.time() if now is None else float(now)
    ttl = SIGNAL_COOLDOWN_SECONDS if cooldown_seconds is None else int(cooldown_seconds)
    try:
        return current - float(sent_at) < max(0, ttl)
    except (TypeError, ValueError):
        return False


def decision_signal_fingerprint(
    symbol,
    decision,
    *,
    timeframe: str,
    entry,
    stop_loss,
    take_profit,
) -> str:
    explicit = str(getattr(decision, "signal_fingerprint", "") or "").strip()
    if explicit:
        return explicit
    return build_signal_fingerprint(
        symbol=symbol,
        side=decision.direction,
        timeframe=timeframe,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        status=decision.signal,
    )

def trend_name(trend):
    return {
        "BULL": "🟢 Восходящий",
        "BEAR": "🔴 Нисходящий",
        "SIDEWAYS": "🟡 Боковой",
    }.get(trend, trend)


def direction_name(direction):
    return {
        "LONG": "🟢 LONG",
        "SHORT": "🔴 SHORT",
    }.get(direction, direction)

def format_signal(
    symbol,
    decision,
    market,
    *,
    entry=None,
    stop_loss=None,
    take_profit=None,
):
    # The active agent path supplies the already accepted trade plan.  The
    # fallback keeps older direct integrations compatible.
    entry = market.tf1h.close if entry is None else entry
    if stop_loss is None or take_profit is None:
        if decision.direction == "LONG":
            stop_loss = entry - market.tf1h.atr
            take_profit = entry + market.tf1h.atr * 2
        else:
            stop_loss = entry + market.tf1h.atr
            take_profit = entry - market.tf1h.atr * 2

    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    rr = reward / risk if risk else 0

    return (
    "🚨 AI Trading Agent\n\n"

        f"🪙 {symbol}\n\n"

        f"📉 Направление: {direction_name(decision.direction)}\n"
        f"🎯 Сигнал: {decision.signal}\n"
        f"⭐ Качество: {decision.quality}\n\n"

        f"💰 Цена: {market.tf1h.close:.2f}\n"
        f"📊 Score: {decision.score}\n"
        f"🎯 Confidence: {decision.confidence}%\n\n"
        f"🎯 Entry: {entry:.2f}\n"
        f"🛑 Stop Loss: {stop_loss:.2f}\n"
        f"✅ Take Profit: {take_profit:.2f}\n"
        f"⚖️ Risk/Reward: 1:{rr:.1f}\n\n"

        "📈 Тренд\n"
        f"• 1H: {trend_name(market.tf1h.trend_ema)}\n"
        f"• 4H: {trend_name(market.tf4h.trend_ema)}\n"
        f"• 1D: {trend_name(market.tf1d.trend_ema)}\n"

        f"📏 ATR (1H): {market.tf1h.atr:.2f}\n\n"

        "📝 Причина\n"
        f"{decision.summary}\n\n"

        f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )


def mark_as_sent(signal_fingerprint: str, *, now: float | None = None) -> None:
    """Persist a bounded map of stable fingerprints and send timestamps."""
    payload = _read_json(LAST_FILE)
    fingerprints = payload.get("fingerprints", {})
    if not isinstance(fingerprints, dict):
        fingerprints = {}
    current = time.time() if now is None else float(now)
    fingerprints[str(signal_fingerprint)] = current
    recent = sorted(fingerprints.items(), key=lambda item: float(item[1]))[-500:]
    _write_json(LAST_FILE, {
        "last_fingerprint": str(signal_fingerprint),
        "updated_at": datetime.fromtimestamp(current, timezone.utc).isoformat(),
        "fingerprints": dict(recent),
    })
