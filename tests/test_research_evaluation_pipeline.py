"""Regression coverage for evidence-aware Research Lab projections."""

from __future__ import annotations

from pathlib import Path

from research_lab_v2.analytics import calculate_metrics, promotion_decision, rank_strategies
from research_lab_v2.dashboard import ResearchDashboardV2
from research_lab_v2.database import ResearchDatabase
from research_lab_v2.service import ResearchLab
from ai_research_dashboard import load_walk_forward


def _metric_row(strategy_id: str, values: list[float], **extra):
    return {"strategy_id": strategy_id, **calculate_metrics(values), **extra}


def test_evaluations_are_not_shadow_trade_evidence(tmp_path: Path):
    db = ResearchDatabase(tmp_path / "research.db")
    db.initialize()
    from strategies import registry
    for strategy in registry.all():
        db.upsert_strategy(strategy)
    for index in range(50):
        db.record_run(
            cycle_id=f"cycle-{index}", strategy_id="RISK_CONSERVATIVE",
            timestamp="2026-08-10T00:00:00+00:00", symbol="BTC/USDT",
            decision="SETUP", status="EVALUATED", features={"adx": 30.0},
        )
    evidence = db.strategy_evidence()["RISK_CONSERVATIVE"]
    assert evidence["evaluations"] == 50
    assert evidence["shadow_trades_opened"] == 0
    assert evidence["closed_trades"] == 0
    assert evidence["complete_outcomes"] == 0


def test_tiny_sample_pf_cannot_become_validated_leader():
    baseline = _metric_row("LIVE_BASELINE", [1, -1] * 60)
    tiny = _metric_row("RISK_CONSERVATIVE", [2, -1], walk_forward_status="NOT_RUN")
    ranked = rank_strategies([tiny, baseline])
    assert ranked[0]["strategy_id"] == "LIVE_BASELINE"
    assert next(row for row in ranked if row["strategy_id"] == "RISK_CONSERVATIVE")["evidence_state"] == "INSUFFICIENT"


def test_missing_metrics_do_not_receive_synthetic_profit_factor():
    row = rank_strategies([{"strategy_id": "ADX_CONFIRM"}])[0]
    assert row["final_score"] < 50
    assert row["evidence_state"] == "INSUFFICIENT"


def test_ranking_is_deterministic_and_sufficient_candidate_is_eligible():
    baseline = _metric_row("LIVE_BASELINE", [1, -1] * 60)
    candidate = _metric_row(
        "ADX_CONFIRM", [2, -0.5] * 60, walk_forward_status="PASS",
        confidence="HIGH", better_windows=3, profitable_windows=3,
    )
    first = rank_strategies([candidate, baseline])
    second = rank_strategies([baseline, candidate])
    assert [row["strategy_id"] for row in first] == [row["strategy_id"] for row in second]
    assert first[0]["strategy_id"] == "ADX_CONFIRM"
    assert first[0]["evidence_state"] == "VALIDATED"


def test_walk_forward_requires_closed_evidence_before_candidate_is_ready():
    insufficient = _metric_row(
        "ADX_CONFIRM", [1] * 99, walk_forward_status="PASS", confidence="HIGH",
    )
    sufficient = _metric_row(
        "TREND_CONFIRM", [1] * 100, walk_forward_status="PASS", confidence="HIGH",
    )
    rows = {row["strategy_id"]: row for row in rank_strategies([insufficient, sufficient])}
    assert rows["ADX_CONFIRM"]["evidence_state"] == "COLLECTING"
    assert rows["ADX_CONFIRM"]["ranking_eligible"] is False
    assert rows["TREND_CONFIRM"]["evidence_state"] == "VALIDATED"
    assert rows["TREND_CONFIRM"]["ranking_eligible"] is True


def test_empty_feature_analysis_is_explicitly_insufficient(tmp_path: Path):
    db = ResearchDatabase(tmp_path / "research.db")
    db.initialize()
    from strategies import registry
    for strategy in registry.all():
        db.upsert_strategy(strategy)
    report = ResearchDashboardV2(tmp_path / "research.db").build_report()
    assert report["feature_analysis"]["status"] == "INSUFFICIENT_DATA"
    assert "INSUFFICIENT_DATA" in ResearchDashboardV2(tmp_path / "research.db").format("features")


def test_closed_feature_outcomes_produce_analysis_after_evidence_floor(tmp_path: Path):
    lab = ResearchLab(tmp_path / "research.db", ranking_interval=1)
    snapshot = {"timestamp": "2026-08-10T00:00:00+00:00", "symbol": "BTC/USDT"}
    closed = []
    for index in range(20):
        closed.append({
            "shadow_trade_id": f"trade-{index}", "candidate_id": "ADX_CONFIRM",
            "symbol": "BTC/USDT", "closed_at": f"2026-08-10T{index:02d}:00:00+00:00",
            "status": "CLOSED", "pnl_r": 1 if index % 2 else -1,
            "feature_snapshot": {"adx": 30.0 if index % 2 else 10.0, "volume_ratio": 1.2},
        })
    lab.process_cycle(cycle_id="c1", snapshot=snapshot, decisions=[], closed_trades=closed)
    report = ResearchDashboardV2(tmp_path / "research.db").build_report()
    assert report["feature_analysis"]["status"] == "READY"
    assert report["feature_analysis"]["joined_outcomes"] == 20
    assert report["top_features"] or report["worst_features"]


def test_rejected_candidate_never_promotes():
    result = promotion_decision(
        _metric_row("MOMENTUM_RELAXED", [2] * 120, walk_forward_status="PASS", confidence="HIGH",
                    better_windows=3, profitable_windows=3),
        _metric_row("LIVE_BASELINE", [1, -1] * 60),
    )
    assert result["eligible"] is False
    assert result["status"] == "REJECTED"


def test_live_baseline_registry_metadata_is_unchanged():
    from strategies import registry
    baseline = registry.get("LIVE_BASELINE")
    assert baseline is not None
    assert baseline.shadow_only is False
    assert baseline.risk_profile == "LIVE_UNCHANGED"


def test_stale_walk_forward_remains_stale(tmp_path: Path):
    source = tmp_path / "candidate_shadow_trades.csv"
    source.write_text("a\n1\n", encoding="utf-8")
    report = tmp_path / "walk_forward_report.json"
    report.write_text(
        '{"status":"PASS","data_source":"candidate_shadow_trades.csv","data_audit":{"source_snapshot":{"size_bytes":1,"mtime_ns":1}}}',
        encoding="utf-8",
    )
    assert load_walk_forward(tmp_path)["status"] == "STALE_WALK_FORWARD_REPORT"


def test_strategy_view_distinguishes_registry_and_runtime(tmp_path: Path):
    db = ResearchDatabase(tmp_path / "research.db")
    db.initialize()
    from strategies import registry
    for strategy in registry.all():
        db.upsert_strategy(strategy)
    status = tmp_path / "status.json"
    status.write_text('{"strategy_modes":{"TREND_PULLBACK":"EVALUATE_ONLY"},"strategies_enabled":["TREND_PULLBACK"]}', encoding="utf-8")
    text = ResearchDashboardV2(tmp_path / "research.db", status_path=status).format("strategies")
    assert "Registry OFF | Runtime EVALUATE_ONLY" in text
    assert "Runtime NOT_CONFIGURED" in text
    health = ResearchDashboardV2(tmp_path / "research.db", status_path=status).format("research_health")
    assert "Pipeline:" in health
    assert "Feature joins:" in health
    artifact = ResearchDashboardV2(tmp_path / "research.db", status_path=status).build_report()["research_health"]
    assert {"producer", "storage", "consumer", "failure_state"} <= set(artifact["stages"][0])
    assert "strategy_counts" in artifact
