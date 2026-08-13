import sqlite3

from research_lab_v2.dashboard import ResearchDashboardV2
from research_lab_v2.database import ResearchDatabase
from research_lab_v2.health import evidence_watch_progress
from research_lab_v2.trace_outcome import current_pipeline_summary

from tests.test_research_attribution_trace import _insert_current


def test_no_current_outcomes_starts_at_first_e2e_milestone(tmp_path):
    summary = current_pipeline_summary(tmp_path / "research.db")
    watch = evidence_watch_progress(summary)
    assert summary["read_only"] is True
    assert watch["fully_joined"] == 0
    assert watch["next_milestone"] == {
        "name": "FIRST_OUTCOME", "label": "First E2E outcome", "target": 1, "progress": 0,
    }


def test_first_and_pipeline_sample_milestones_are_informational(tmp_path):
    path = _insert_current(tmp_path)
    first = evidence_watch_progress(current_pipeline_summary(path))
    assert first["achieved_milestones"] == ["FIRST_OUTCOME"]
    assert first["next_milestone"]["name"] == "PIPELINE_SAMPLE"
    sample = evidence_watch_progress({"fully_joined": 5})
    assert sample["fully_joined"] == 5
    assert sample["achieved_milestones"] == ["FIRST_OUTCOME", "PIPELINE_SAMPLE"]
    assert sample["next_milestone"]["name"] == "EXPLORATORY_FEATURES"


def test_milestones_are_unique_and_never_change_research_gates():
    watch = evidence_watch_progress({"fully_joined": 100})
    assert watch["achieved_milestones"] == [
        "FIRST_OUTCOME", "PIPELINE_SAMPLE", "EXPLORATORY_FEATURES",
        "RANKING_EVIDENCE", "STRONGER_RANKING",
    ]
    assert len(watch["achieved_milestones"]) == len(set(watch["achieved_milestones"]))
    assert watch["next_milestone"] is None
    # Evidence Watch is informational: it intentionally exposes no gate,
    # promotion, Walk-Forward, or mode-changing output.
    assert not ({"ranking_allowed", "walk_forward_allowed", "promotion_allowed", "automatic_shadow_enable"} & set(watch))


def test_research_health_surfaces_compact_current_pipeline_block(tmp_path):
    path = _insert_current(tmp_path)
    text = ResearchDashboardV2(path).format("research_health")
    assert "Current pipeline:" in text
    assert "Fully joined: 1" in text
    assert "Next milestone: Pipeline sample — 1/5" in text


def test_historical_debt_is_separate_and_current_partial_or_broken_is_regression(tmp_path):
    path = _insert_current(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE shadow_trade_outcomes SET join_status='UNRESOLVED', data_quality='PARTIAL'")
    ResearchDatabase(path).persist_closed_outcome({
        "shadow_trade_id": "legacy", "candidate_id": "TREND_CONFIRM", "symbol": "BTC/USDT",
        "timeframe": "1h", "side": "LONG", "entry_time": "2026-08-01T10:00:00Z",
        "exit_time": "2026-08-01T11:00:00Z", "entry_price": 100, "stop_loss": 98,
        "take_profit": 104, "exit_price": 98, "exit_reason": "STOP_LOSS", "pnl_r": -1,
    }, source="LEDGER_BACKFILL")
    before = path.read_bytes()
    summary = current_pipeline_summary(path)
    assert path.read_bytes() == before
    assert summary["historical_unresolved"] == 1
    assert summary["partial"] == 1
    assert summary["current_pipeline_regression"] is True
    with sqlite3.connect(path) as db:
        db.execute("UPDATE shadow_trade_outcomes SET pnl_r='NaN' WHERE shadow_trade_id='trace-1'")
    broken = current_pipeline_summary(path)
    assert broken["broken"] == 1
    assert broken["current_pipeline_regression"] is True
