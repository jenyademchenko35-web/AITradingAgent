from impulse_probability_engine import evaluate, publish

def test_probability_is_deterministic_and_bounded():
    context={"symbol":"BTC","timestamp":"2026-01-01T00:00:00Z","trend_score":60,"momentum_score":20,"adx":30,"volume_ratio":1.2,"confidence":80,"score":25,"market_regime":"TREND"}
    assert evaluate(context)==evaluate(context)
    assert 0 <= evaluate(context)["impulse_probability"] <= 100

def test_adx_volume_and_failed_risk_change_probability(tmp_path):
    low=evaluate({"symbol":"BTC","adx":10,"volume_ratio":.5})["impulse_probability"]
    high=evaluate({"symbol":"BTC","adx":30,"volume_ratio":1.2})["impulse_probability"]
    risk=evaluate({"symbol":"BTC","adx":30,"volume_ratio":1.2,"failed_filters":["risk"]})["impulse_probability"]
    assert high > low and risk < high
    rows=publish([{"symbol":"BTC","adx":30,"volume_ratio":1.2}],output=tmp_path/"impulse.json",heatmap=tmp_path/"heatmap.json")
    assert rows and (tmp_path/"heatmap.json").exists()
