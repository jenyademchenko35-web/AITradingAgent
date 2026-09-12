"""SQLite persistence for reproducible shadow and attributed LIVE research."""

from __future__ import annotations

import json
import hashlib
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
    version TEXT NOT NULL, enabled INTEGER NOT NULL, risk_profile TEXT NOT NULL,
    shadow_only INTEGER NOT NULL, parameters_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL REFERENCES strategies(id), timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL DEFAULT '1h',
    decision TEXT NOT NULL, status TEXT NOT NULL,
    feature_snapshot_json TEXT NOT NULL, result_r REAL,
    shadow_trade_id TEXT, would_open_trade INTEGER NOT NULL DEFAULT 0,
    block_reason TEXT, condition_active INTEGER NOT NULL DEFAULT 0,
    entry_triggered INTEGER NOT NULL DEFAULT 0, trigger_reason TEXT,
    signal_fingerprint TEXT, previous_fingerprint TEXT,
    is_new_signal INTEGER NOT NULL DEFAULT 0, blocked_reason TEXT,
    signal_audit_version TEXT, strategy_mode TEXT,
    actual_shadow_opened INTEGER NOT NULL DEFAULT 0,
    shadow_mode_started_at TEXT,
    feature_snapshot_id TEXT, signal_id TEXT, decision_id TEXT,
    strategy_version TEXT, attribution_version TEXT, data_quality TEXT,
    live_trade_id TEXT, source_type TEXT, source_run_uid TEXT UNIQUE,
    UNIQUE(cycle_id, strategy_id, symbol, timeframe)
);
CREATE TABLE IF NOT EXISTS strategy_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL REFERENCES strategies(id),
    calculated_at TEXT NOT NULL, closed_trades INTEGER NOT NULL,
    profit_factor REAL, winrate REAL NOT NULL, net_r REAL NOT NULL,
    max_drawdown REAL NOT NULL, sharpe REAL NOT NULL, sortino REAL NOT NULL,
    expectancy REAL NOT NULL, final_score REAL NOT NULL DEFAULT 0,
    UNIQUE(strategy_id, calculated_at)
);
CREATE TABLE IF NOT EXISTS feature_statistics (
    id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL REFERENCES strategies(id),
    calculated_at TEXT NOT NULL, feature_name TEXT NOT NULL,
    profitable_mean REAL, unprofitable_mean REAL, importance REAL NOT NULL,
    direction TEXT NOT NULL, samples INTEGER NOT NULL,
    UNIQUE(strategy_id, calculated_at, feature_name)
);
CREATE TABLE IF NOT EXISTS walk_forward_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL REFERENCES strategies(id),
    calculated_at TEXT NOT NULL, status TEXT NOT NULL, windows INTEGER NOT NULL,
    better_windows INTEGER NOT NULL, profitable_windows INTEGER NOT NULL,
    oos_pf REAL, oos_net_r REAL NOT NULL, confidence TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL REFERENCES strategies(id),
    timestamp TEXT NOT NULL, status TEXT NOT NULL, promotion_probability REAL NOT NULL,
    reasons_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signal_states (
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
    condition_active INTEGER NOT NULL DEFAULT 0,
    direction TEXT NOT NULL DEFAULT '', signal_fingerprint TEXT,
    last_triggered_at TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY(strategy_id, symbol, timeframe)
);
CREATE TABLE IF NOT EXISTS shadow_trade_outcomes (
    shadow_trade_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL DEFAULT '1h', side TEXT NOT NULL,
    entry_time TEXT, entry_price REAL, stop_loss REAL, take_profit REAL,
    exit_time TEXT, exit_price REAL, exit_reason TEXT, status TEXT NOT NULL,
    pnl_r REAL NOT NULL, mfe_r REAL, mae_r REAL, holding_candles INTEGER,
    signal_fingerprint TEXT, feature_snapshot_json TEXT,
    feature_snapshot_available INTEGER NOT NULL DEFAULT 0,
    feature_snapshot_valid INTEGER NOT NULL DEFAULT 0,
    source_run_id INTEGER REFERENCES strategy_runs(id),
    join_status TEXT NOT NULL DEFAULT 'UNRESOLVED',
    outcome_id TEXT, feature_snapshot_id TEXT, signal_id TEXT, decision_id TEXT,
    strategy_version TEXT, attribution_version TEXT, data_quality TEXT,
    source TEXT NOT NULL, original_shadow_trade_id TEXT,
    created_at TEXT NOT NULL, persisted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_trade_outcomes (
    live_trade_id TEXT PRIMARY KEY,
    outcome_id TEXT NOT NULL UNIQUE,
    source_run_id TEXT NOT NULL UNIQUE REFERENCES strategy_runs(source_run_uid),
    source_type TEXT NOT NULL CHECK(source_type='LIVE'),
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    strategy_version TEXT NOT NULL,
    research_attribution_version TEXT NOT NULL,
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, direction TEXT NOT NULL,
    opened_at TEXT NOT NULL, closed_at TEXT NOT NULL,
    entry_price REAL NOT NULL, stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL, exit_price REAL NOT NULL,
    pnl_r REAL NOT NULL, close_reason TEXT NOT NULL,
    research_signal_fingerprint TEXT NOT NULL,
    feature_snapshot_json TEXT NOT NULL,
    feature_snapshot_id TEXT NOT NULL, signal_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    trade_metadata_quality TEXT NOT NULL,
    research_join_quality TEXT NOT NULL,
    data_quality TEXT NOT NULL, join_status TEXT NOT NULL,
    created_at TEXT NOT NULL, persisted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_strategy_time ON strategy_runs(strategy_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_metrics_strategy_time ON strategy_metrics(strategy_id, calculated_at);
CREATE INDEX IF NOT EXISTS idx_wf_strategy_time ON walk_forward_results(strategy_id, calculated_at);
CREATE INDEX IF NOT EXISTS idx_outcomes_strategy_time
    ON shadow_trade_outcomes(strategy_id, exit_time);
CREATE INDEX IF NOT EXISTS idx_outcomes_source_run
    ON shadow_trade_outcomes(source_run_id);
"""


class ResearchDatabaseBusy(RuntimeError):
    """Raised after SQLite's busy timeout expires; callers may fail open."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchDatabase:
    def __init__(self, path: str | Path = "research.db") -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(SCHEMA)
            self._migrate(connection)
            yield connection
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise ResearchDatabaseBusy(str(exc)) from exc
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Apply additive migrations without deleting research rows."""
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(strategy_runs)")
        }
        additions = {
            "timeframe": "TEXT NOT NULL DEFAULT '1h'",
            "would_open_trade": "INTEGER NOT NULL DEFAULT 0",
            "block_reason": "TEXT",
            "condition_active": "INTEGER NOT NULL DEFAULT 0",
            "entry_triggered": "INTEGER NOT NULL DEFAULT 0",
            "trigger_reason": "TEXT",
            "signal_fingerprint": "TEXT",
            "previous_fingerprint": "TEXT",
            "is_new_signal": "INTEGER NOT NULL DEFAULT 0",
            "blocked_reason": "TEXT",
            "signal_audit_version": "TEXT",
            "strategy_mode": "TEXT",
            "actual_shadow_opened": "INTEGER NOT NULL DEFAULT 0",
            "shadow_mode_started_at": "TEXT",
            "feature_snapshot_id": "TEXT",
            "signal_id": "TEXT",
            "decision_id": "TEXT",
            "strategy_version": "TEXT",
            "attribution_version": "TEXT",
            "data_quality": "TEXT",
            "live_trade_id": "TEXT",
            "source_type": "TEXT",
            "source_run_uid": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE strategy_runs ADD COLUMN {name} {definition}"
                )
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_strategy_runs_cycle_scope
            ON strategy_runs(cycle_id, strategy_id, symbol, timeframe)
        """)
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_strategy_runs_live_trade
            ON strategy_runs(live_trade_id) WHERE live_trade_id IS NOT NULL
        """)
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_strategy_runs_source_run_uid
            ON strategy_runs(source_run_uid) WHERE source_run_uid IS NOT NULL
        """)
        # SQLite foreign-key parent keys require a full UNIQUE index. NULL is
        # still allowed repeatedly, preserving every historical shadow row.
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_strategy_runs_source_run_uid_full
            ON strategy_runs(source_run_uid)
        """)
        outcome_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(shadow_trade_outcomes)")
        }
        outcome_additions = {
            "outcome_id": "TEXT",
            "feature_snapshot_id": "TEXT",
            "signal_id": "TEXT",
            "decision_id": "TEXT",
            "strategy_version": "TEXT",
            "attribution_version": "TEXT",
            "data_quality": "TEXT",
        }
        for name, definition in outcome_additions.items():
            if name not in outcome_columns:
                connection.execute(
                    f"ALTER TABLE shadow_trade_outcomes ADD COLUMN {name} {definition}"
                )
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_outcomes_outcome_id
            ON shadow_trade_outcomes(outcome_id) WHERE outcome_id IS NOT NULL
        """)

    def initialize(self) -> None:
        with self.connect():
            pass

    def upsert_strategy(self, strategy: Any) -> None:
        payload = strategy.candidate_config()
        with self.connect() as db:
            db.execute("""
                INSERT INTO strategies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                description=excluded.description, version=excluded.version,
                enabled=excluded.enabled, risk_profile=excluded.risk_profile,
                shadow_only=excluded.shadow_only,
                parameters_json=excluded.parameters_json, updated_at=excluded.updated_at
            """, (
                strategy.id, strategy.name, strategy.description, strategy.version,
                int(strategy.enabled), strategy.risk_profile, int(strategy.shadow_only),
                json.dumps(payload, sort_keys=True), _utc(),
            ))

    def record_run(self, *, cycle_id: str, strategy_id: str, timestamp: str,
                   symbol: str, timeframe: str = "1h", decision: str, status: str,
                   features: Mapping[str, Any], result_r: float | None = None,
                   shadow_trade_id: str | None = None,
                   would_open_trade: bool = False,
                   block_reason: str | None = None,
                   condition_active: bool = False,
                   entry_triggered: bool = False,
                   trigger_reason: str | None = None,
                   signal_fingerprint: str | None = None,
                   previous_fingerprint: str | None = None,
                   is_new_signal: bool = False,
                   blocked_reason: str | None = None,
                   signal_audit_version: str | None = None,
                   strategy_mode: str | None = None,
                   actual_shadow_opened: bool = False,
                   shadow_mode_started_at: str | None = None,
                   feature_snapshot_id: str | None = None,
                   signal_id: str | None = None,
                   decision_id: str | None = None,
                   strategy_version: str | None = None,
                  attribution_version: str | None = None,
                   data_quality: str | None = None,
                   live_trade_id: str | None = None,
                   source_type: str | None = None) -> None:
        with self.connect() as db:
            db.execute("""
                INSERT INTO strategy_runs
                (cycle_id, strategy_id, timestamp, symbol, timeframe, decision, status,
                 feature_snapshot_json, result_r, shadow_trade_id, would_open_trade,
                 block_reason, condition_active, entry_triggered, trigger_reason,
                 signal_fingerprint, previous_fingerprint, is_new_signal, blocked_reason,
                 signal_audit_version, strategy_mode, actual_shadow_opened,
                 shadow_mode_started_at, feature_snapshot_id, signal_id, decision_id,
                 strategy_version, attribution_version, data_quality,
                 live_trade_id, source_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id, strategy_id, symbol, timeframe) DO NOTHING
            """, (cycle_id, strategy_id, timestamp, symbol, timeframe, decision, status,
                  json.dumps(dict(features), ensure_ascii=False, sort_keys=True, default=str),
                  result_r, shadow_trade_id, int(would_open_trade), block_reason,
                  int(condition_active), int(entry_triggered), trigger_reason,
                  signal_fingerprint, previous_fingerprint, int(is_new_signal),
                  blocked_reason, signal_audit_version, strategy_mode,
                  int(actual_shadow_opened), shadow_mode_started_at,
                  feature_snapshot_id, signal_id, decision_id, strategy_version,
                  attribution_version, data_quality, live_trade_id, source_type))

    def record_live_run(self, *, source_run_id: str, live_trade_id: str,
                        cycle_id: str, timestamp: str,
                        symbol: str, timeframe: str, decision: str,
                        features: Mapping[str, Any], feature_snapshot_id: str,
                        signal_id: str, decision_id: str, strategy_version: str,
                        research_signal_fingerprint: str,
                        attribution_version: str) -> dict[str, str]:
        """Persist and return the sole exact Research run for a LIVE trade."""
        required = {
            "source_run_id": source_run_id, "live_trade_id": live_trade_id,
            "cycle_id": cycle_id,
            "timestamp": timestamp, "symbol": symbol,
            "feature_snapshot_id": feature_snapshot_id, "signal_id": signal_id,
            "decision_id": decision_id, "strategy_version": strategy_version,
            "research_signal_fingerprint": research_signal_fingerprint,
            "attribution_version": attribution_version,
        }
        missing = sorted(key for key, value in required.items() if not str(value or "").strip())
        if missing or not live_trade_id.startswith("LIVE-"):
            raise ValueError(f"invalid LIVE attribution: missing={','.join(missing) or '-'}")
        feature_json = json.dumps(dict(features), ensure_ascii=False, sort_keys=True, default=str)
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM strategy_runs WHERE source_run_uid=? OR live_trade_id=?",
                (source_run_id, live_trade_id),
            ).fetchall()
            if len(existing) > 1:
                raise ValueError("LIVE attribution is not one-to-one")
            if existing:
                row = existing[0]
                expected = {
                    "cycle_id": cycle_id, "timestamp": timestamp,
                    "strategy_id": "LIVE_BASELINE", "symbol": symbol,
                    "timeframe": timeframe, "feature_snapshot_id": feature_snapshot_id,
                    "signal_id": signal_id, "decision_id": decision_id,
                    "strategy_version": strategy_version,
                    "signal_fingerprint": research_signal_fingerprint,
                    "attribution_version": attribution_version, "source_type": "LIVE",
                    "source_run_uid": source_run_id,
                    "feature_snapshot_json": feature_json,
                }
                if any(str(row[key] or "") != str(value) for key, value in expected.items()):
                    raise ValueError("conflicting LIVE attribution retry")
                return {"status": "existing", "source_run_id": source_run_id}
            db.execute("""
                INSERT INTO strategy_runs
                (cycle_id, strategy_id, timestamp, symbol, timeframe, decision,
                 status, feature_snapshot_json, would_open_trade,
                 condition_active, entry_triggered, signal_fingerprint,
                 is_new_signal, strategy_mode, actual_shadow_opened,
                 feature_snapshot_id, signal_id, decision_id, strategy_version,
                 attribution_version, data_quality, live_trade_id, source_type,
                 source_run_uid)
                VALUES (?, 'LIVE_BASELINE', ?, ?, ?, ?, 'LIVE_DECISION_CAPTURED',
                        ?, 1, 1, 1, ?, 1, 'LIVE', 0, ?, ?, ?, ?, ?,
                        'ATTRIBUTION_COMPLETE', ?, 'LIVE', ?)
            """, (
                cycle_id, timestamp, symbol, timeframe, decision, feature_json,
                research_signal_fingerprint, feature_snapshot_id, signal_id,
                decision_id, strategy_version, attribution_version, live_trade_id,
                source_run_id,
            ))
            rows = db.execute(
                "SELECT source_run_uid FROM strategy_runs WHERE live_trade_id=?",
                (live_trade_id,),
            ).fetchall()
            if len(rows) != 1:
                raise ValueError("LIVE attribution did not resolve to exactly one run")
            if str(rows[0]["source_run_uid"]) != source_run_id:
                raise ValueError("LIVE attribution source run identity changed")
            return {"status": "inserted", "source_run_id": source_run_id}

    def persist_live_outcome(self, outcome: Mapping[str, Any], *,
                             persisted_at: str | None = None) -> dict[str, Any]:
        """Persist one exact LIVE outcome; retries are idempotent by trade ID."""
        required = (
            "live_trade_id", "source_run_id", "strategy_id", "strategy_version",
            "research_attribution_version", "symbol", "timeframe", "direction",
            "opened_at", "closed_at", "entry_price", "stop_loss", "take_profit",
            "exit_price", "pnl_r", "close_reason", "research_signal_fingerprint",
            "feature_snapshot", "feature_snapshot_id", "signal_id", "decision_id",
        )
        missing = [name for name in required if outcome.get(name) in (None, "")]
        if missing:
            raise ValueError(f"missing LIVE outcome fields: {','.join(missing)}")
        live_trade_id = str(outcome["live_trade_id"])
        if not live_trade_id.startswith("LIVE-"):
            raise ValueError("LIVE outcome cannot use a shadow trade ID")
        source_run_id = str(outcome["source_run_id"])
        numeric = {
            name: self._number_or_none(outcome[name])
            for name in ("entry_price", "stop_loss", "take_profit", "exit_price", "pnl_r")
        }
        if any(value is None for value in numeric.values()):
            raise ValueError("LIVE outcome contains a non-finite numeric field")
        opened = datetime.fromisoformat(str(outcome["opened_at"]).replace("Z", "+00:00"))
        closed = datetime.fromisoformat(str(outcome["closed_at"]).replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        if closed.tzinfo is None:
            closed = closed.replace(tzinfo=timezone.utc)
        if closed <= opened:
            raise ValueError("LIVE outcome timestamps are not ordered")
        feature_snapshot = dict(outcome["feature_snapshot"])
        snapshot_json = json.dumps(
            feature_snapshot, ensure_ascii=False,
            sort_keys=True, default=str,
        )
        from .attribution import feature_snapshot_id as calculate_feature_snapshot_id
        calculated_feature_id = calculate_feature_snapshot_id(outcome["feature_snapshot"])
        if calculated_feature_id != str(outcome["feature_snapshot_id"]):
            raise ValueError("LIVE outcome feature snapshot identity is invalid")
        semantic_identity = {
            "symbol": outcome["symbol"],
            "timeframe": outcome["timeframe"],
            "direction": str(outcome["direction"]).upper(),
        }
        snapshot_identity = {
            "symbol": feature_snapshot.get("symbol"),
            "timeframe": feature_snapshot.get("timeframe"),
            "direction": str(feature_snapshot.get("direction") or "").upper(),
        }
        if any(str(snapshot_identity[key]) != str(value)
               for key, value in semantic_identity.items()):
            raise ValueError("LIVE outcome identity differs from its feature snapshot")
        outcome_id = "live-out-" + hashlib.sha256(
            f"{live_trade_id}|{outcome['research_attribution_version']}".encode("utf-8")
        ).hexdigest()[:24]
        now = persisted_at or _utc()
        with self.connect() as db:
            existing = db.execute(
                "SELECT * FROM live_trade_outcomes WHERE live_trade_id=?", (live_trade_id,),
            ).fetchone()
            if existing:
                expected_existing = {
                    "outcome_id": outcome_id,
                    "source_run_id": source_run_id,
                    "source_type": "LIVE",
                    "strategy_id": outcome["strategy_id"],
                    "strategy_version": outcome["strategy_version"],
                    "research_attribution_version": outcome["research_attribution_version"],
                    "symbol": outcome["symbol"],
                    "timeframe": outcome["timeframe"],
                    "direction": outcome["direction"],
                    "opened_at": outcome["opened_at"],
                    "closed_at": outcome["closed_at"],
                    "entry_price": numeric["entry_price"],
                    "stop_loss": numeric["stop_loss"],
                    "take_profit": numeric["take_profit"],
                    "exit_price": numeric["exit_price"],
                    "pnl_r": numeric["pnl_r"],
                    "close_reason": outcome["close_reason"],
                    "research_signal_fingerprint": outcome["research_signal_fingerprint"],
                    "feature_snapshot_json": snapshot_json,
                    "feature_snapshot_id": outcome["feature_snapshot_id"],
                    "signal_id": outcome["signal_id"],
                    "decision_id": outcome["decision_id"],
                }
                if any(str(existing[key]) != str(value)
                       for key, value in expected_existing.items()):
                    raise ValueError("conflicting LIVE outcome retry")
                return {"status": "existing", "live_trade_id": live_trade_id,
                        "outcome_id": outcome_id, "join_status": existing["join_status"]}
            run = db.execute(
                "SELECT * FROM strategy_runs WHERE source_run_uid=? AND live_trade_id=? "
                "AND source_type='LIVE'", (source_run_id, live_trade_id),
            ).fetchall()
            if len(run) != 1:
                raise ValueError("LIVE outcome source run is missing or ambiguous")
            row = run[0]
            run_timestamp = datetime.fromisoformat(
                str(row["timestamp"]).replace("Z", "+00:00")
            )
            if run_timestamp.tzinfo is None:
                run_timestamp = run_timestamp.replace(tzinfo=timezone.utc)
            if run_timestamp > opened:
                raise ValueError("LIVE decision timestamp is after trade open")
            exact = {
                "strategy_id": outcome["strategy_id"],
                "symbol": outcome["symbol"], "timeframe": outcome["timeframe"],
                "strategy_version": outcome["strategy_version"],
                "feature_snapshot_id": outcome["feature_snapshot_id"],
                "signal_id": outcome["signal_id"],
                "decision_id": outcome["decision_id"],
                "signal_fingerprint": outcome["research_signal_fingerprint"],
                "attribution_version": outcome["research_attribution_version"],
                "feature_snapshot_json": snapshot_json,
            }
            if any(str(row[key] or "") != str(value) for key, value in exact.items()):
                raise ValueError("LIVE outcome attribution does not match its source run")
            db.execute("""
                INSERT INTO live_trade_outcomes
                (live_trade_id, outcome_id, source_run_id, source_type,
                 strategy_id, strategy_version, research_attribution_version,
                 symbol, timeframe, direction, opened_at, closed_at,
                 entry_price, stop_loss, take_profit, exit_price, pnl_r,
                 close_reason, research_signal_fingerprint, feature_snapshot_json,
                 feature_snapshot_id, signal_id, decision_id,
                 trade_metadata_quality, research_join_quality, data_quality,
                 join_status, created_at, persisted_at)
                VALUES (?, ?, ?, 'LIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, 'JOIN_COMPLETE', 'OUTCOME_COMPLETE',
                        'JOIN_COMPLETE', ?, ?)
            """, (
                live_trade_id, outcome_id, source_run_id,
                outcome["strategy_id"], outcome["strategy_version"],
                outcome["research_attribution_version"], outcome["symbol"],
                outcome["timeframe"], outcome["direction"],
                outcome["opened_at"], outcome["closed_at"],
                numeric["entry_price"], numeric["stop_loss"],
                numeric["take_profit"], numeric["exit_price"], numeric["pnl_r"],
                outcome["close_reason"], outcome["research_signal_fingerprint"],
                snapshot_json, outcome["feature_snapshot_id"], outcome["signal_id"],
                outcome["decision_id"], outcome.get("trade_metadata_quality", "COMPLETE"),
                outcome["opened_at"], now,
            ))
        return {"status": "inserted", "live_trade_id": live_trade_id,
                "outcome_id": outcome_id, "join_status": "JOIN_COMPLETE",
                "data_quality": "OUTCOME_COMPLETE"}

    def live_join_coverage(self) -> dict[str, int]:
        """Return exact LIVE join counts without mixing shadow metrics."""
        with self.connect() as db:
            row = db.execute("""
                SELECT COUNT(*) AS outcomes,
                       SUM(CASE WHEN join_status='JOIN_COMPLETE' THEN 1 ELSE 0 END) AS joined,
                       COUNT(DISTINCT live_trade_id) AS trades,
                       COUNT(DISTINCT source_run_id) AS runs
                FROM live_trade_outcomes
            """).fetchone()
        return {key: int(row[key] or 0) for key in ("outcomes", "joined", "trades", "runs")}

    def live_materialized_trade_ids(self) -> dict[str, set[str]]:
        """Fetch reconciliation watermarks in one read transaction."""
        with self.connect() as db:
            source_runs = {
                str(row[0]) for row in db.execute(
                    "SELECT live_trade_id FROM strategy_runs "
                    "WHERE source_type='LIVE' AND live_trade_id IS NOT NULL"
                )
            }
            outcomes = {
                str(row[0]) for row in db.execute(
                    "SELECT live_trade_id FROM live_trade_outcomes"
                )
            }
        return {"source_runs": source_runs, "outcomes": outcomes}

    def load_signal_states(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM signal_states").fetchall()
        return {
            (row["strategy_id"], row["symbol"], row["timeframe"]): dict(row)
            for row in rows
        }

    def upsert_signal_state(self, *, strategy_id: str, symbol: str, timeframe: str,
                            condition_active: bool, direction: str,
                            signal_fingerprint: str | None,
                            last_triggered_at: str | None, updated_at: str) -> None:
        with self.connect() as db:
            db.execute("""
                INSERT INTO signal_states
                (strategy_id, symbol, timeframe, condition_active, direction,
                 signal_fingerprint, last_triggered_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id, symbol, timeframe) DO UPDATE SET
                    condition_active=excluded.condition_active,
                    direction=excluded.direction,
                    signal_fingerprint=excluded.signal_fingerprint,
                    last_triggered_at=excluded.last_triggered_at,
                    updated_at=excluded.updated_at
            """, (strategy_id, symbol, timeframe, int(condition_active), direction,
                  signal_fingerprint, last_triggered_at, updated_at))

    def dry_run_diagnostics(self) -> dict[str, dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT strategy_id, symbol, condition_active, entry_triggered,
                       would_open_trade, blocked_reason, signal_fingerprint,
                       actual_shadow_opened
                FROM strategy_runs
                WHERE cycle_id NOT LIKE '%:closed:%'
                  AND signal_audit_version = 'event_dedup_v1'
                ORDER BY id
            """).fetchall()
        report: dict[str, dict[str, Any]] = {}
        fingerprints: dict[str, set[str]] = {}
        symbols: dict[str, set[str]] = {}
        for raw in rows:
            row = dict(raw)
            strategy_id = row["strategy_id"]
            item = report.setdefault(strategy_id, {
                "evaluations": 0, "condition_active": 0,
                "new_entry_triggers": 0, "repeated_active_conditions": 0,
                "would_open": 0, "actual_shadow_opened": 0,
                "blocked_by_reason": {},
                "signal_rate": 0.0, "unique_signal_fingerprints": 0,
                "symbols_with_signals": [],
            })
            item["evaluations"] += 1
            item["condition_active"] += int(row["condition_active"] or 0)
            item["new_entry_triggers"] += int(row["entry_triggered"] or 0)
            item["would_open"] += int(row["would_open_trade"] or 0)
            item["actual_shadow_opened"] += int(row["actual_shadow_opened"] or 0)
            if row["condition_active"] and not row["entry_triggered"]:
                item["repeated_active_conditions"] += 1
            reason = str(row["blocked_reason"] or "")
            if reason:
                item["blocked_by_reason"][reason] = item["blocked_by_reason"].get(reason, 0) + 1
            fingerprint = str(row["signal_fingerprint"] or "")
            if fingerprint:
                fingerprints.setdefault(strategy_id, set()).add(fingerprint)
            if row["entry_triggered"]:
                symbols.setdefault(strategy_id, set()).add(str(row["symbol"]))
        for strategy_id, item in report.items():
            item["signal_rate"] = round(
                item["would_open"] / item["evaluations"] * 100, 2
            ) if item["evaluations"] else 0.0
            item["unique_signal_fingerprints"] = len(fingerprints.get(strategy_id, set()))
            item["symbols_with_signals"] = sorted(symbols.get(strategy_id, set()))
        return report

    def record_metrics(self, strategy_id: str, metrics: Mapping[str, Any],
                       calculated_at: str | None = None) -> None:
        with self.connect() as db:
            db.execute("""
                INSERT INTO strategy_metrics
                (strategy_id, calculated_at, closed_trades, profit_factor, winrate,
                 net_r, max_drawdown, sharpe, sortino, expectancy, final_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (strategy_id, calculated_at or _utc(), metrics["closed_trades"],
                  metrics.get("profit_factor"), metrics["winrate"], metrics["net_r"],
                  metrics["max_drawdown"], metrics["sharpe"], metrics["sortino"],
                  metrics["expectancy"], metrics.get("final_score", 0)))

    @staticmethod
    def _feature_snapshot_payload(trade: Mapping[str, Any]) -> tuple[str | None, bool, bool]:
        """Keep a supplied ledger snapshot intact while rejecting malformed JSON."""
        raw = trade.get("feature_snapshot_json")
        if raw in (None, ""):
            raw = trade.get("feature_snapshot")
        if raw in (None, ""):
            return None, False, False
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                return raw, True, False
            return raw, True, isinstance(parsed, Mapping)
        if isinstance(raw, Mapping):
            return json.dumps(dict(raw), ensure_ascii=False, sort_keys=True, default=str), True, True
        return None, True, False

    @staticmethod
    def _number_or_none(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    def _source_run_id(self, db: sqlite3.Connection, *, shadow_trade_id: str,
                       strategy_id: str, symbol: str, timeframe: str) -> int | None:
        """Only an exact opening ID is a deterministic evaluation↔outcome join."""
        row = db.execute("""
            SELECT id FROM strategy_runs
            WHERE shadow_trade_id=? AND strategy_id=? AND symbol=? AND timeframe=?
              AND cycle_id NOT LIKE '%:closed:%'
            ORDER BY id DESC LIMIT 1
        """, (shadow_trade_id, strategy_id, symbol, timeframe)).fetchone()
        return int(row["id"]) if row else None

    def persist_closed_outcome(self, trade: Mapping[str, Any], *, source: str,
                               persisted_at: str | None = None) -> dict[str, Any]:
        """Persist one canonical shadow closure, idempotently by shadow_trade_id.

        The outcome table deliberately stands apart from `strategy_runs`: one
        evaluation may lead to zero or multiple independently closed trades.
        """
        trade_id = str(trade.get("shadow_trade_id") or "").strip()
        attribution_version = str(trade.get("attribution_version") or "") or None
        is_new_attribution = attribution_version == "attribution_chain_v1"
        # New runtime trades must carry their explicit strategy identity.  The
        # candidate alias remains only for historical ledger compatibility.
        strategy_value = trade.get("strategy_id")
        if not strategy_value and not is_new_attribution:
            strategy_value = trade.get("candidate_id")
        strategy_id = str(strategy_value or "").upper()
        symbol = str(trade.get("symbol") or "").strip()
        timeframe = str(trade.get("timeframe") or "1h").strip() or "1h"
        side = str(trade.get("side") or trade.get("direction") or "UNKNOWN").upper()
        pnl_r = self._number_or_none(trade.get("pnl_r"))
        if not trade_id or not strategy_id or not symbol or pnl_r is None:
            return {"status": "invalid", "shadow_trade_id": trade_id,
                    "reason": "MISSING_REQUIRED_OUTCOME_FIELDS"}
        snapshot_json, snapshot_available, snapshot_valid = self._feature_snapshot_payload(trade)
        with self.connect() as db:
            existing = db.execute(
                "SELECT shadow_trade_id FROM shadow_trade_outcomes WHERE shadow_trade_id=?",
                (trade_id,),
            ).fetchone()
            if existing:
                return {"status": "existing", "shadow_trade_id": trade_id}
            source_run_id = self._source_run_id(
                db, shadow_trade_id=trade_id, strategy_id=strategy_id,
                symbol=symbol, timeframe=timeframe,
            )
            feature_snapshot_id = str(trade.get("feature_snapshot_id") or "") or None
            signal_id = str(trade.get("signal_id") or "") or None
            decision_id = str(trade.get("decision_id") or "") or None
            strategy_version = str(trade.get("strategy_version") or "") or None
            required_links = (feature_snapshot_id, signal_id, decision_id, strategy_version)
            fully_attributed = source_run_id is not None and (
                not is_new_attribution or all(required_links)
            )
            join_status = "RESOLVED" if fully_attributed else "UNRESOLVED"
            data_quality = "COMPLETE" if fully_attributed else (
                "PARTIAL" if is_new_attribution else "LEGACY_PARTIAL"
            )
            now = persisted_at or _utc()
            outcome_id = f"out-{trade_id}"
            db.execute("""
                INSERT INTO shadow_trade_outcomes
                (shadow_trade_id, strategy_id, symbol, timeframe, side,
                 entry_time, entry_price, stop_loss, take_profit,
                 exit_time, exit_price, exit_reason, status, pnl_r, mfe_r, mae_r,
                 holding_candles, signal_fingerprint, feature_snapshot_json,
                 feature_snapshot_available, feature_snapshot_valid, source_run_id,
                 join_status, outcome_id, feature_snapshot_id, signal_id, decision_id,
                 strategy_version, attribution_version, data_quality,
                 source, original_shadow_trade_id, created_at, persisted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id, strategy_id, symbol, timeframe, side,
                trade.get("entry_time") or trade.get("opened_at"),
                self._number_or_none(trade.get("entry_price", trade.get("entry"))),
                self._number_or_none(trade.get("stop_loss")),
                self._number_or_none(trade.get("take_profit")),
                trade.get("exit_time") or trade.get("closed_at"),
                self._number_or_none(trade.get("exit_price")),
                trade.get("exit_reason"), str(trade.get("status") or "CLOSED"), pnl_r,
                self._number_or_none(trade.get("mfe_r")), self._number_or_none(trade.get("mae_r")),
                int(self._number_or_none(trade.get("holding_candles")) or 0),
                trade.get("signal_fingerprint"), snapshot_json, int(snapshot_available),
                int(snapshot_valid), source_run_id, join_status,
                outcome_id, feature_snapshot_id, signal_id, decision_id,
                strategy_version, attribution_version, data_quality, source, trade_id,
                trade.get("entry_time") or trade.get("opened_at") or now, now,
            ))
        return {"status": "inserted", "shadow_trade_id": trade_id,
                "join_status": join_status,
                "outcome_id": outcome_id, "data_quality": data_quality,
                "feature_snapshot_available": snapshot_available,
                "feature_snapshot_valid": snapshot_valid}

    def outcome_reconciliation(self, ledger_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        """Read-only ledger↔canonical-outcome audit, keyed only by shadow_trade_id."""
        rows = [row for row in ledger_rows if str(row.get("status", "")).upper() == "CLOSED"]
        ids = [str(row.get("shadow_trade_id") or "").strip() for row in rows]
        valid_ids = [trade_id for trade_id in ids if trade_id]
        duplicates = len(valid_ids) - len(set(valid_ids))
        with self.connect() as db:
            db_ids = {
                str(row["shadow_trade_id"]) for row in db.execute(
                    "SELECT shadow_trade_id FROM shadow_trade_outcomes"
                ).fetchall()
            }
            unresolved = int(db.execute(
                "SELECT COUNT(*) FROM shadow_trade_outcomes WHERE join_status='UNRESOLVED'"
            ).fetchone()[0])
        ledger_id_set = set(valid_ids)
        return {
            "ledger_closed_total": len(rows), "db_closed_total": len(db_ids),
            "outcome_sync_gap": len(ledger_id_set - db_ids),
            "duplicate_shadow_trade_ids": duplicates,
            "unresolved_outcome_joins": unresolved,
            "missing_shadow_trade_ids": sorted(ledger_id_set - db_ids),
        }

    def completed_runs(self) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT strategy_id, pnl_r, feature_snapshot_json
                FROM shadow_trade_outcomes ORDER BY exit_time, shadow_trade_id
            """).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["strategy_id"], []).append({
                "pnl_r": row["pnl_r"],
                "feature_snapshot": json.loads(row["feature_snapshot_json"] or "{}"),
            })
        return grouped

    def feature_completed_runs(self) -> dict[str, list[dict[str, Any]]]:
        """Closed outcomes eligible for feature attribution, never inferred joins."""
        with self.connect() as db:
            rows = db.execute("""
                SELECT strategy_id, pnl_r, feature_snapshot_json
                FROM shadow_trade_outcomes
                WHERE join_status='RESOLVED' AND feature_snapshot_valid=1
                ORDER BY exit_time, shadow_trade_id
            """).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["strategy_id"], []).append({
                "pnl_r": row["pnl_r"],
                "feature_snapshot": json.loads(row["feature_snapshot_json"] or "{}"),
            })
        return grouped

    def strategy_evidence(self) -> dict[str, dict[str, Any]]:
        """Return counters with deliberately separate evaluation and trade facts."""
        with self.connect() as db:
            rows = db.execute("""
                WITH evaluations AS (
                    SELECT strategy_id,
                           COUNT(*) AS evaluations,
                           SUM(CASE WHEN UPPER(decision) IN ('SETUP', 'HIGH PRIORITY') THEN 1 ELSE 0 END) AS eligible_signals,
                           SUM(would_open_trade) AS would_open,
                           SUM(actual_shadow_opened) AS shadow_opened,
                           MAX(timestamp) AS last_evaluation_at
                    FROM strategy_runs
                    WHERE cycle_id NOT LIKE '%:closed:%'
                    GROUP BY strategy_id
                ), outcomes AS (
                    SELECT strategy_id, COUNT(*) AS closed_trades,
                           SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
                           SUM(CASE WHEN pnl_r < 0 THEN 1 ELSE 0 END) AS losses,
                           MAX(exit_time) AS last_closed_at
                    FROM shadow_trade_outcomes GROUP BY strategy_id
                )
                SELECT s.id AS strategy_id,
                       COALESCE(e.evaluations, 0) AS evaluations,
                       COALESCE(e.eligible_signals, 0) AS eligible_signals,
                       COALESCE(e.would_open, 0) AS would_open,
                       COALESCE(e.shadow_opened, 0) AS shadow_opened,
                       COALESCE(o.closed_trades, 0) AS closed_trades,
                       COALESCE(o.wins, 0) AS wins, COALESCE(o.losses, 0) AS losses,
                       e.last_evaluation_at, o.last_closed_at
                FROM strategies s
                LEFT JOIN evaluations e ON e.strategy_id=s.id
                LEFT JOIN outcomes o ON o.strategy_id=s.id
                ORDER BY s.id
            """).fetchall()
        result = {row["strategy_id"]: dict(row) for row in rows}
        for row in result.values():
            row["incomplete_outcomes"] = max(
                int(row["shadow_opened"] or 0) - int(row["closed_trades"] or 0), 0
            )
            # Backward-compatible aliases make the data flow unambiguous to
            # dashboards and Telegram formatters.
            row["shadow_trades_opened"] = row["shadow_opened"]
            row["shadow_trades_closed"] = row["closed_trades"]
            row["complete_outcomes"] = row["closed_trades"]
        return result

    def feature_join_coverage(self) -> dict[str, Any]:
        """Report only closed outcome ↔ feature joins; never infer missing data."""
        with self.connect() as db:
            rows = db.execute("""
                SELECT strategy_id, feature_snapshot_json, join_status,
                       feature_snapshot_available, feature_snapshot_valid
                FROM shadow_trade_outcomes ORDER BY exit_time, shadow_trade_id
            """).fetchall()
        total = len(rows)
        joined = 0
        malformed = 0
        unresolved = 0
        numeric_fields: set[str] = set()
        for row in rows:
            if row["join_status"] != "RESOLVED":
                unresolved += 1
                continue
            if not row["feature_snapshot_valid"]:
                malformed += 1
                continue
            try:
                snapshot = json.loads(row["feature_snapshot_json"] or "{}")
            except (TypeError, ValueError):
                malformed += 1
                continue
            if not isinstance(snapshot, Mapping):
                malformed += 1
                continue
            values = [
                key for key, value in snapshot.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ]
            if values:
                joined += 1
                numeric_fields.update(values)
        return {
            "closed_outcomes": total,
            "joined_outcomes": joined,
            "missing_or_malformed": total - joined,
            "malformed_snapshots": malformed,
            "unresolved_outcome_joins": unresolved,
            "join_coverage_percent": round(joined / total * 100, 2) if total else 0.0,
            "numeric_feature_fields": sorted(numeric_fields),
        }

    def cycle_count(self) -> int:
        with self.connect() as db:
            row = db.execute("""
                SELECT COUNT(DISTINCT cycle_id) FROM strategy_runs
                WHERE cycle_id NOT LIKE '%:closed:%'
            """).fetchone()
        return int(row[0] if row else 0)

    def latest_walk_forward(self) -> dict[str, dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT w.* FROM walk_forward_results w
                JOIN (SELECT strategy_id, MAX(id) id FROM walk_forward_results GROUP BY strategy_id) latest
                ON latest.id=w.id
            """).fetchall()
        return {row["strategy_id"]: dict(row) for row in rows}

    def record_feature_statistics(self, strategy_id: str, rows: Iterable[Mapping[str, Any]],
                                  calculated_at: str | None = None) -> None:
        stamp = calculated_at or _utc()
        with self.connect() as db:
            db.executemany("""
                INSERT INTO feature_statistics
                (strategy_id, calculated_at, feature_name, profitable_mean,
                 unprofitable_mean, importance, direction, samples)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [(strategy_id, stamp, row["feature"], row.get("profitable_mean"),
                   row.get("unprofitable_mean"), row["importance"], row["direction"],
                   row["samples"]) for row in rows])

    def record_walk_forward(self, strategy_id: str, result: Mapping[str, Any]) -> None:
        with self.connect() as db:
            db.execute("""
                INSERT INTO walk_forward_results
                (strategy_id, calculated_at, status, windows, better_windows,
                 profitable_windows, oos_pf, oos_net_r, confidence, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (strategy_id, result.get("generated_at", _utc()), result.get("status", "NOT_RUN"),
                  int(result.get("windows", 0)), int(result.get("better_windows", 0)),
                  int(result.get("profitable_windows", 0)), result.get("oos_pf"),
                  float(result.get("oos_net_r", 0) or 0), result.get("confidence", "LOW"),
                  json.dumps(dict(result), ensure_ascii=False, sort_keys=True, default=str)))

    def record_candidate(self, strategy_id: str, decision: Mapping[str, Any]) -> None:
        with self.connect() as db:
            db.execute("""
                INSERT INTO candidate_history
                (strategy_id, timestamp, status, promotion_probability, reasons_json)
                VALUES (?, ?, ?, ?, ?)
            """, (strategy_id, _utc(), decision["status"], decision["promotion_probability"],
                  json.dumps(decision.get("reasons", []), ensure_ascii=False)))

    def table_names(self) -> set[str]:
        with self.connect() as db:
            return {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
