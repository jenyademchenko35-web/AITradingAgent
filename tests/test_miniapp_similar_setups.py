from tests.miniapp_intelligence_support import DECISION_FIELDS, decision_row, repository, write_csv


TRADE_FIELDS = ["trade_id", "shadow_trade_id", "symbol", "asset_class", "side", "direction", "timeframe", "entry_time", "entry", "exit_price", "result", "pnl_r", "strategy_id", "candidate_id", "score", "confidence", "market_regime", "trend_direction", "volatility_regime"]


def trade(index: int, **overrides):
    row = {"trade_id": f"live-{index}", "symbol": "BTC/USDT", "asset_class": "CRYPTO", "side": "LONG", "timeframe": "1h", "entry_time": f"2026-07-01T00:{index:02d}:00Z", "entry": "100", "exit_price": "102", "result": "WIN" if index % 2 == 0 else "LOSS", "pnl_r": "1" if index % 2 == 0 else "-0.5", "strategy_id": "LIVE_BASELINE", "score": "27", "confidence": "91", "market_regime": "TREND", "trend_direction": "LONG", "volatility_regime": "NORMAL"}
    row.update(overrides)
    return row


def test_statistics_hidden_below_configured_minimum(tmp_path):
    write_csv(tmp_path / "decision_debug.csv", [decision_row()], DECISION_FIELDS)
    write_csv(tmp_path / "trades.csv", [trade(i) for i in range(19)], TRADE_FIELDS)
    result = repository(tmp_path, minimum=20).similar_setups("BTCUSDT", "1h", source="LIVE")
    assert result.total == 19 and result.statistics_available is False
    assert result.winrate is None and result.profit_factor is None


def test_statistics_and_pagination_at_minimum_sample(tmp_path):
    write_csv(tmp_path / "decision_debug.csv", [decision_row()], DECISION_FIELDS)
    write_csv(tmp_path / "trades.csv", [trade(i) for i in range(20)], TRADE_FIELDS)
    result = repository(tmp_path, minimum=20).similar_setups("BTCUSDT", "1h", source="LIVE", page=2, page_size=5)
    assert result.statistics_available is True and result.winrate == 50
    assert result.count == 5 and result.page == 2


def test_trade_sources_are_never_mixed(tmp_path):
    write_csv(tmp_path / "decision_debug.csv", [decision_row()], DECISION_FIELDS)
    write_csv(tmp_path / "trades.csv", [trade(1)], TRADE_FIELDS)
    write_csv(tmp_path / "candidate_shadow_trades.csv", [trade(2, trade_id="", shadow_trade_id="legacy-2", candidate_id="LIVE_BASELINE")], TRADE_FIELDS)
    write_csv(tmp_path / "research_lab_shadow_history.csv", [trade(3, trade_id="", shadow_trade_id="research-3")], TRADE_FIELDS)
    data = repository(tmp_path, minimum=20)
    live = data.similar_setups("BTCUSDT", "1h", source="LIVE")
    legacy = data.similar_setups("BTCUSDT", "1h", source="LEGACY_SHADOW")
    research = data.similar_setups("BTCUSDT", "1h", source="RESEARCH_LAB")
    assert {item.trade_id for item in live.items} == {"live-1"}
    assert {item.trade_id for item in legacy.items} == {"legacy-2"}
    assert {item.trade_id for item in research.items} == {"research-3"}
    assert all(item.source == live.source for item in live.items)
