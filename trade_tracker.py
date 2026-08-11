import csv
import hashlib
import json
import logging
import uuid
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
TRADES_FILE = BASE_DIR / "trades.csv"
FIELDS = [
    "symbol",
    "direction",
    "entry",
    "stop_loss",
    "take_profit",
    "status",
    "result",
    "opened_at",
    "closed_at",
    "exit_price",
    "pnl",
    "trade_id",
    "trade_id_provenance",
    "research_metadata_json",
    "research_data_quality",
]
LOGGER = logging.getLogger(__name__)


def _legacy_trade_id(trade):
    """Return a stable legacy identifier only when the lifecycle identity exists."""
    identity = [
        str(trade.get("symbol") or "").strip().upper(),
        str(trade.get("direction") or "").strip().upper(),
        str(trade.get("opened_at") or "").strip(),
        str(trade.get("entry") or trade.get("entry_price") or "").strip(),
    ]
    if not all(identity):
        return ""
    return "LEGACY-" + hashlib.sha256("|".join(identity).encode("utf-8")).hexdigest()[:20]


def _ensure_trade_identity(trade):
    """Preserve an existing id or attach a deterministic provenance-labelled legacy id."""
    if str(trade.get("trade_id") or "").strip():
        trade.setdefault("trade_id_provenance", "LIVE_PERSISTED")
        return
    legacy_id = _legacy_trade_id(trade)
    if legacy_id:
        trade["trade_id"] = legacy_id
        trade["trade_id_provenance"] = "LEGACY_DETERMINISTIC"


def _metadata_json(metadata):
    """Serialize an immutable open-time research context without failing trades."""
    try:
        return json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "{}"


def _assess_trade_safely(trade):
    """Return a diagnostic assessment without ever delaying a live lifecycle write."""
    try:
        from research_data_quality import assess_research_trade
        return assess_research_trade(trade)
    except Exception as error:  # research instrumentation is deliberately fail-open
        LOGGER.warning("research trade assessment failed: %s", error)
        return {
            "data_quality": "UNAVAILABLE",
            "missing_required": [],
            "missing_optional": [],
            "not_applicable": [],
            "trade_id": trade.get("trade_id"),
            "trade_id_provenance": trade.get("trade_id_provenance") or "UNAVAILABLE",
        }


def _research_diagnostics(trade, *, snapshot=False, assessment=None):
    """Best-effort diagnostics; failures must never affect trade persistence."""
    try:
        from research_data_quality import assess_research_trade, build_decision_snapshot
        assessment = assessment or assess_research_trade(trade)
        if assessment["data_quality"] != "COMPLETE":
            LOGGER.warning(
                "trade research data_quality=%s trade_id=%s missing_required=%s missing_optional=%s not_applicable=%s",
                assessment["data_quality"], assessment.get("trade_id") or "UNAVAILABLE",
                ",".join(assessment["missing_required"]) or "-",
                ",".join(assessment["missing_optional"]) or "-",
                ",".join(assessment["not_applicable"]) or "-",
            )
        if snapshot:
            build_decision_snapshot(symbol=trade.get("symbol"), opened_at=trade.get("opened_at"), extra=trade)
    except Exception as error:  # research instrumentation is deliberately fail-open
        LOGGER.warning("research diagnostics failed: %s", error)

def ensure_file():
    if not TRADES_FILE.exists():
        with TRADES_FILE.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
        return
    # Existing production CSVs predate the research columns.  Upgrade only the
    # header/empty columns before appending so a new metadata-rich row can never
    # become misaligned with an old header.  Historical values are copied as-is.
    with TRADES_FILE.open("r", newline="") as f:
        reader = csv.DictReader(f)
        existing_fields = reader.fieldnames or []
        if all(field in existing_fields for field in FIELDS):
            return
        rows = [dict(row) for row in reader]
    temporary = TRADES_FILE.with_suffix(TRADES_FILE.suffix + ".tmp")
    with temporary.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(TRADES_FILE)

def open_trade(symbol, direction, entry, stop_loss, take_profit, *, research_metadata=None):
    ensure_file()
    now = datetime.utcnow().isoformat()
    trade = {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "status": "OPEN",
        "result": "",
        "opened_at": now,
        "closed_at": "",
        "exit_price": "",
        "pnl": "",
        "trade_id": f"LIVE-{uuid.uuid4().hex}",
        "trade_id_provenance": "LIVE_PERSISTED",
        "research_metadata_json": _metadata_json(research_metadata),
        "research_data_quality": "",
    }
    assessment = _assess_trade_safely(trade)
    trade["research_data_quality"] = assessment["data_quality"]
    with TRADES_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writerow(trade)
    _research_diagnostics(trade, snapshot=True, assessment=assessment)

def get_open_trades():
    ensure_file()
    trades = []
    with TRADES_FILE.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["status"] == "OPEN":
                trades.append(row)
    return trades

def close_trade(symbol, result, exit_price=None, pnl=None):
    ensure_file()
    trades = []
    now = datetime.utcnow().isoformat()
    with TRADES_FILE.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["symbol"] == symbol and row["status"] == "OPEN":
                _ensure_trade_identity(row)
                row["status"] = result
                row["result"] = result
                row["closed_at"] = now
                row["exit_price"] = exit_price if exit_price is not None else ""
                row["pnl"] = pnl if pnl is not None else ""
                assessment = _assess_trade_safely(row)
                row["research_data_quality"] = assessment["data_quality"]
                _research_diagnostics(row, assessment=assessment)
            trades.append(row)
    with TRADES_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(trades)

    print(f"Trade closed: {symbol} -> {result}")
