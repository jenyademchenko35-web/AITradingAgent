"""Performance metrics use resolved, closed shadow outcomes only."""

import sqlite3

import pytest

from research_lab_v2.service import ResearchLab


def _seed_run(db, strategy_id, result_r=999):
    cursor = db.execute("""
        INSERT INTO strategy_runs
        (cycle_id, strategy_id, timestamp, symbol, decision, status,
         feature_snapshot_json, result_r)
        VALUES (?, ?, '2026-08-01T00:00:00Z', 'BTC/USDT', 'SETUP',
                'EVALUATED', '{}', ?)
    """, (f"cycle-{strategy_id}", strategy_id, result_r))
    return cursor.lastrowid


def _seed_outcome(db, trade_id, strategy_id, run_id, pnl_r, *,
                  join_status="RESOLVED", status="CLOSED", snapshot_json=None):
    db.execute("""
        INSERT INTO shadow_trade_outcomes
        (shadow_trade_id, strategy_id, symbol, side, status, pnl_r,
         source_run_id, join_status, outcome_id, source, created_at,
         persisted_at, exit_time, feature_snapshot_json, feature_snapshot_valid)
        VALUES (?, ?, 'BTC/USDT', 'LONG', ?, ?, ?, ?, ?,
                'TEST', '2026-08-01T00:00:00Z', '2026-08-01T01:00:00Z',
                '2026-08-01T01:00:00Z', ?, 0)
    """, (trade_id, strategy_id, status, pnl_r,
          run_id if join_status == "RESOLVED" else None,
          join_status, f"out-{trade_id}", snapshot_json))


def _lab(tmp_path):
    lab = ResearchLab(tmp_path / "research.db", ranking_interval=1)
    lab.register_strategies()
    return lab


def test_metrics_rebuild_uses_only_resolved_closed_outcome_pnl(tmp_path):
    lab = _lab(tmp_path)
    with lab.database.connect() as db:
        run_id = _seed_run(db, "ADX_CONFIRM")
        _seed_outcome(db, "closed-win", "ADX_CONFIRM", run_id, 2,
                      snapshot_json="malformed snapshot")
        _seed_outcome(db, "closed-loss", "ADX_CONFIRM", run_id, -1, status="LOSS")
        _seed_outcome(db, "closed-legacy-win", "ADX_CONFIRM", run_id, 1, status="WIN")
        _seed_outcome(db, "unresolved", "ADX_CONFIRM", run_id, -50,
                      join_status="UNRESOLVED")
        _seed_outcome(db, "still-open", "ADX_CONFIRM", run_id, 50, status="OPEN")

    result = lab.rebuild_outcome_metrics()
    assert result["closed"] == 3
    with lab.database.connect() as db:
        metrics = db.execute("""
            SELECT closed_trades, net_r, expectancy, profit_factor, winrate
            FROM strategy_metrics WHERE strategy_id='ADX_CONFIRM'
        """).fetchone()
        outcomes = db.execute("""
            SELECT shadow_trade_id, join_status FROM shadow_trade_outcomes
            ORDER BY shadow_trade_id
        """).fetchall()
    assert tuple(metrics) == pytest.approx((3, 2, 2 / 3, 3, 200 / 3))
    assert len(outcomes) == 5
    assert ("unresolved", "UNRESOLVED") in [tuple(row) for row in outcomes]


def test_ranking_and_promotion_receive_resolved_counts(tmp_path, monkeypatch):
    lab = _lab(tmp_path)
    with lab.database.connect() as db:
        baseline_run = _seed_run(db, "LIVE_BASELINE")
        candidate_run = _seed_run(db, "ADX_CONFIRM")
        for index in range(100):
            _seed_outcome(db, f"baseline-{index}", "LIVE_BASELINE",
                          baseline_run, 1 if index % 2 == 0 else -1)
        for index in range(99):
            _seed_outcome(db, f"candidate-{index}", "ADX_CONFIRM",
                          candidate_run, 2 if index % 2 == 0 else -1)
        _seed_outcome(db, "candidate-unresolved", "ADX_CONFIRM",
                      candidate_run, 100, join_status="UNRESOLVED")

    monkeypatch.setattr("research_lab_v2.service.evaluate_integrity", lambda *args, **kwargs: {
        "gates": {"ranking_allowed": True, "promotion_allowed": True},
    })
    result = lab.rebuild_outcome_metrics()
    assert result["ranked"] is True
    candidate = next(row for row in result["ranking"] if row["strategy_id"] == "ADX_CONFIRM")
    assert candidate["closed_trades"] == 99
    assert candidate["net_r"] == 51
    with lab.database.connect() as db:
        stored = db.execute("""
            SELECT closed_trades, net_r FROM strategy_metrics
            WHERE strategy_id='ADX_CONFIRM'
        """).fetchone()
        decision = db.execute("""
            SELECT status, reasons_json FROM candidate_history
            WHERE strategy_id='ADX_CONFIRM'
        """).fetchone()
    assert tuple(stored) == (99, 51)
    assert decision[0] == "REJECT"
    assert 'Closed trades >= 100' in decision[1]
