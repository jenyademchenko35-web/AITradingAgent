from types import SimpleNamespace
from decision_telemetry import telemetry_row

def test_telemetry_only_serializes_finalized_values():
    result = telemetry_row(timestamp="t", cycle_id="c", symbol="BTC", market=SimpleNamespace(tf1h=SimpleNamespace(volume_ratio=1.2, trend_ema="BULLISH")), decision=SimpleNamespace(direction="LONG", signal="SETUP", raw_signal_status="SETUP", final_filter_status="BLOCKED", execution_status="NO_TRADE", veto_reasons=["VETO"], failed_filters=[]), trend=SimpleNamespace(long=60, short=0), structure=SimpleNamespace(long=10, short=2), momentum=SimpleNamespace(long=20, short=0), risk=SimpleNamespace(long=15, short=0))
    assert result["trend_score"] == "60.0"
    assert result["blocking_reasons"] == '["VETO"]'
