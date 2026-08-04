from research_lab_v2.candidate_readiness import build_readiness
from research_lab_v2.analytics import promotion_decision

def test_rejected_momentum_relaxed_can_never_be_promoted():
    decision=promotion_decision({"strategy_id":"MOMENTUM_RELAXED"},{})
    assert decision["status"] == "REJECTED" and decision["eligible"] is False

def test_readiness_never_enables_shadow_and_requires_evidence():
    report=build_readiness([{"candidate_id":"TREND_PULLBACK","condition_active":True,"feature_snapshot":{"trade_plan":{"entry":1,"stop_loss":.5,"take_profit":2,"rr":2},"market_regime":"BULL"}}])
    item=next(x for x in report["candidates"] if x["strategy"]=="TREND_PULLBACK")
    assert item["readiness"] == "INSUFFICIENT_DATA"
    assert report["automatic_shadow_enable"] is False
