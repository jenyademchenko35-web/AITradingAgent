from telegram_ui.data import latest_rows
from telegram_ui.keyboards import market_keyboard
from telegram_ui.screens import format_market_screen, format_stats_screen, format_trades_screen


def test_market_uses_latest_status_per_symbol():
    rows = [
        {"symbol": "BTC/USDT", "timeframe": "1h", "timestamp": "1", "signal": "WAIT"},
        {"symbol": "BTC/USDT", "timeframe": "1h", "timestamp": "2", "signal": "SETUP"},
        {"symbol": "ETH/USDT", "timeframe": "1h", "timestamp": "2", "signal": "NO TRADE"},
    ]
    current = [{**row, "symbol": symbol} for (symbol, _), row in latest_rows(rows).items()]
    text = format_market_screen(current)
    assert "BTC" in text and "🟢 ГОТОВ К ВХОДУ" in text
    assert "ETH" in text and "⚪ НЕТ СДЕЛКИ" in text
    assert "WAIT" not in text


def test_market_keyboard_routes_symbols_and_refresh():
    callbacks = [button.callback_data for row in market_keyboard(["BTC/USDT"]).inline_keyboard for button in row]
    assert callbacks == ["ui:v2:symbol:BTCUSDT", "ui:v2:market", "ui:v2:watchlist", "ui:v2:home"]


def test_compact_trade_and_stats_wrappers():
    trades = format_trades_screen([{"symbol": "BTC/USDT", "direction": "LONG", "status": "OPEN"}])
    stats = format_stats_screen({
        "closed_trades": 10, "winrate": 60, "profit_factor": 1.5,
        "net_r": 3, "max_drawdown_r": 1.2,
    }, ["WIN", "LOSS"])
    assert "Open: 1" in trades
    assert "Closed: 10" in stats and "PF: 1.5" in stats
    assert len(trades) < 4096 and len(stats) < 4096
