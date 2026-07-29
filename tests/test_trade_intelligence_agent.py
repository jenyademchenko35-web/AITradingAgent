import csv
import json

from trade_intelligence_agent import TradeIntelligenceAgent


def agent(tmp_path):
    return TradeIntelligenceAgent(tmp_path)


def classified(**values):
    base = {"result": "LOSS", "context": {}, "behavior": {}}
    for key, value in values.items():
        target, _, name = key.partition("__")
        base[target][name] = value
    return TradeIntelligenceAgent(".").classify(base)


def test_missing_and_empty_files_are_safe(tmp_path):
    (tmp_path / "trades.csv").write_text("", encoding="utf-8")
    result = agent(tmp_path).analyze()
    assert result["summary"]["total_analyzed"] == 0
    assert result["analysis"]["warnings"]


def test_probable_cause_classifications():
    assert classified(behavior__late_entry=True)["primary_cause"] == "LATE_ENTRY"
    assert classified(context__volume_ratio=0.7)["primary_cause"] == "WEAK_VOLUME"
    assert classified(context__market_regime="SIDEWAYS")["primary_cause"] == "SIDEWAYS_MARKET"
    good = classified(
        context__total_score=28, context__confidence=90,
        context__volume_ratio=1.2, context__trend="ALIGNED",
    )
    assert good["primary_cause"] == "GOOD_SETUP_BAD_OUTCOME"
    assert classified()["primary_cause"] == "UNKNOWN"


def test_mfe_mae_calculation(tmp_path):
    item = agent(tmp_path)
    trade = item._normalize_trade({
        "trade_id": "1", "symbol": "BTC/USDT", "direction": "LONG",
        "entry": 100, "stop_loss": 95, "take_profit": 110,
        "opened_at": "2026-01-01T00:00:00+00:00",
        "closed_at": "2026-01-01T02:00:00+00:00",
    })
    rows = [{"timestamp": f"2026-01-01T0{hour}:00:00+00:00", "high": high, "low": low}
            for hour, high, low in ((0, 102, 99), (1, 108, 96), (2, 105, 98))]
    behavior, complete = item._behavior(trade, {"BTCUSDT": rows})
    assert complete
    assert behavior["maximum_favorable_excursion"] == 8
    assert behavior["maximum_adverse_excursion"] == 4
    assert behavior["mfe_r"] == 1.6
    assert behavior["mae_r"] == 0.8


def test_what_if_analysis():
    rows = [
        {"pnl_r": -1, "context": {"adx": 10}, "behavior": {}},
        {"pnl_r": 2, "context": {"adx": 30}, "behavior": {}},
    ]
    result = TradeIntelligenceAgent(".").what_if(rows)["ADX_LT_18"]
    assert result["removed_trades"] == 1
    assert result["net_r_before"] == 1
    assert result["net_r_after"] == 2


def test_small_sample_never_gets_strong_recommendation():
    rows = [{"pnl_r": -1, "primary_cause": "LATE_ENTRY"} for _ in range(10)]
    summary = TradeIntelligenceAgent(".").build_summary(rows)
    assert all(item["confidence"] == "LOW" for item in summary["recommendations"])


def test_repeated_run_deduplicates_and_does_not_touch_live_files(tmp_path):
    live = tmp_path / "portfolio_config.json"
    live.write_text('{"risk": 1}', encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir()
    trade = {
        "trade_id": "same", "symbol": "BTC/USDT", "direction": "LONG",
        "entry": 100, "exit": 101, "pnl_r": 1, "result": "WIN",
        "opened_at": "2026-01-01T00:00:00+00:00",
        "closed_at": "2026-01-01T01:00:00+00:00",
    }
    (reports / "research_trades_enriched.json").write_text(
        json.dumps({"trades": [trade, trade]}), encoding="utf-8"
    )
    item = agent(tmp_path)
    first = item.analyze()
    second = item.analyze()
    assert len(first["analysis"]["trades"]) == 1
    assert len(second["analysis"]["trades"]) == 1
    assert live.read_text(encoding="utf-8") == '{"risk": 1}'
