import pytest

from telegram_ui.miniapp import build_miniapp_deep_link


def test_home_signal_and_intelligence_deep_links_are_bounded():
    base = "https://mini.example/app"
    assert build_miniapp_deep_link(base) == base
    assert build_miniapp_deep_link(base, symbol="BTC/USDT", timeframe="1h") == (
        "https://mini.example/app#/signals/BTCUSDT/1h"
    )
    assert build_miniapp_deep_link(
        base, symbol="BTCUSDT", timeframe="1h", intelligence=True,
    ).endswith("#/signals/BTCUSDT/1h/intelligence")


@pytest.mark.parametrize("url", ["http://mini.example", "javascript:alert(1)", "https://user:pass@mini.example"])
def test_deep_links_require_safe_https_base(url):
    with pytest.raises(ValueError):
        build_miniapp_deep_link(url)


def test_deep_links_reject_traversal_and_unknown_timeframes():
    with pytest.raises(ValueError):
        build_miniapp_deep_link("https://mini.example", symbol="../secret", timeframe="1h")
    with pytest.raises(ValueError):
        build_miniapp_deep_link("https://mini.example", symbol="BTCUSDT", timeframe="5m")
