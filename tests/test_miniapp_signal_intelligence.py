from miniapp.backend.intelligence import build_evidence_explanation
from miniapp.shared.models import SignalIntelligencePayload
from tests.miniapp_intelligence_support import DECISION_FIELDS, decision_row, repository, write_csv


def test_long_intelligence_uses_saved_fields_only(tmp_path):
    write_csv(tmp_path / "decision_debug.csv", [decision_row()], DECISION_FIELDS)
    result = repository(tmp_path).signal_intelligence("BTCUSDT", "1h")
    assert result.side == "LONG"
    assert result.trend_score == 55
    assert result.entry == 100
    assert result.signal_fingerprint == "saved-fingerprint"
    assert result.explanation.confirmations == ("trend alignment", "volume confirmed")


def test_short_and_no_trade_preserve_real_direction_and_evidence(tmp_path):
    row = decision_row(
        direction="SHORT", signal="NO TRADE", entry="", stop_loss="", take_profit="",
        trend_score="", trend_long="10", trend_short="51", confirmations="",
        blockers="WEAK_ADX", failed_filters="ADX:17<20", summary="",
    )
    write_csv(tmp_path / "decision_debug.csv", [row], DECISION_FIELDS)
    result = repository(tmp_path).signal_intelligence("BTCUSDT", "1h")
    assert result.side == "SHORT" and result.status == "NO TRADE"
    assert result.trend_score == 51
    assert result.entry is None and result.take_profit is None
    assert result.explanation.blockers == ("WEAK_ADX",)
    assert result.explanation.limitations == ("ADX:17<20",)


def test_missing_optional_fields_remain_none_and_explanation_is_evidence_only():
    payload = SignalIntelligencePayload(symbol="BTC/USDT", timeframe="1h", score=27)
    explanation = build_evidence_explanation(payload)
    assert payload.adx is None and payload.entry is None
    assert explanation.confirmations == ()
    assert explanation.limitations == ()
    assert explanation.blockers == ()
