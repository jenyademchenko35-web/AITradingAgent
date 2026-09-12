import csv
import hashlib
import json
import logging
import os
import stat
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
LIVE_ATTRIBUTION_VERSION = "live_attribution_bridge_v1"
LIVE_ATTRIBUTION_TRADE_PREFIX = "LIVE-RAV1-"
REQUIRED_ATTRIBUTION_FIELDS = (
    "research_attribution_version", "source_run_id", "signal_id",
    "decision_id", "strategy_id", "strategy_version", "feature_snapshot_id",
    "research_signal_fingerprint", "live_trade_id", "feature_snapshot",
    "trade_metadata_quality", "research_join_quality", "decision_timestamp",
)


def _atomic_write_trades(rows):
    """Publish a complete canonical ledger without an in-place truncation window."""
    previous_mode = None
    if TRADES_FILE.exists():
        previous_mode = stat.S_IMODE(TRADES_FILE.stat().st_mode)
    temporary = TRADES_FILE.with_name(
        f".{TRADES_FILE.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            if previous_mode is not None:
                os.fchmod(handle.fileno(), previous_mode)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, TRADES_FILE)
        directory_fd = os.open(TRADES_FILE.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


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
    values = dict(metadata or {})
    attribution_required = bool(values.get("research_attribution_version"))
    if attribution_required:
        missing = [field for field in REQUIRED_ATTRIBUTION_FIELDS if not values.get(field)]
        if missing:
            raise ValueError(
                "incomplete LIVE Research attribution: " + ",".join(missing)
            )
    try:
        return json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        if attribution_required:
            raise ValueError("LIVE Research attribution is not serializable") from exc
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
        _atomic_write_trades([])
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
    _atomic_write_trades(rows)

def open_trade(symbol, direction, entry, stop_loss, take_profit, *,
               research_metadata=None, trade_id=None):
    ensure_file()
    now = datetime.utcnow().isoformat()
    trade_id = str(trade_id or f"LIVE-{uuid.uuid4().hex}")
    if not trade_id.startswith("LIVE-"):
        raise ValueError("new canonical trade must use LIVE identity")
    metadata = dict(research_metadata or {})
    attribution_version = metadata.get("research_attribution_version")
    versioned_trade = trade_id.startswith(LIVE_ATTRIBUTION_TRADE_PREFIX)
    if bool(versioned_trade) != (attribution_version == LIVE_ATTRIBUTION_VERSION):
        raise ValueError("LIVE Research attribution version and trade identity disagree")
    if attribution_version:
        if attribution_version != LIVE_ATTRIBUTION_VERSION:
            raise ValueError("unsupported LIVE Research attribution version")
        if str(metadata.get("live_trade_id") or "") != trade_id:
            raise ValueError("canonical trade_id differs from LIVE Research attribution")
    serialized_metadata = _metadata_json(metadata)
    with TRADES_FILE.open("r", newline="") as f:
        if any(str(row.get("trade_id")) == trade_id for row in csv.DictReader(f)):
            raise ValueError(f"duplicate canonical trade identity: {trade_id}")
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
        "trade_id": trade_id,
        "trade_id_provenance": "LIVE_PERSISTED",
        "research_metadata_json": serialized_metadata,
        "research_data_quality": "",
    }
    assessment = _assess_trade_safely(trade)
    trade["research_data_quality"] = assessment["data_quality"]
    with TRADES_FILE.open("r", newline="") as f:
        rows = [dict(row) for row in csv.DictReader(f)]
    _atomic_write_trades([*rows, trade])
    _research_diagnostics(trade, snapshot=True, assessment=assessment)
    return trade


def get_all_trades():
    """Return canonical trade rows for durable notification reconciliation."""
    ensure_file()
    with TRADES_FILE.open("r", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]

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
    _atomic_write_trades(trades)

    closed = [
        row for row in trades
        if row["symbol"] == symbol and row["closed_at"] == now
    ]

    print(f"Trade closed: {symbol} -> {result}")
    return closed
