import csv
from datetime import datetime, timedelta, timezone

from diagnose_walk_forward import diagnose, main


FIELDS = ["shadow_trade_id", "strategy_name", "status", "close_time", "pnl_r", "symbol", "side"]


def rows(strategy, count, start, *, prefix, naive=False):
    result = []
    for index in range(count):
        stamp = start + timedelta(hours=index)
        timestamp = stamp.replace(tzinfo=None).isoformat() if naive else stamp.isoformat()
        result.append({"shadow_trade_id": f"{prefix}-{index}", "strategy_name": strategy,
                       "status": "WIN" if index % 3 == 0 else "LOSS", "close_time": timestamp,
                       "pnl_r": "1" if index % 3 == 0 else "-0.4", "symbol": "BTC/USDT", "side": "LONG"})
    return result


def write(path, data):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(data)


def test_107_baseline_121_candidate_create_three_windows(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    path = tmp_path / "shadow.csv"
    write(path, rows("LIVE_BASELINE", 107, start, prefix="b") +
          rows("MOMENTUM_RELAXED", 121, start, prefix="c"))
    report = diagnose(path)
    assert report["strategies"]["LIVE_BASELINE"]["count"] == 107
    assert report["strategies"]["MOMENTUM_RELAXED"]["count"] == 121
    assert report["window_config"]["potential_windows"] == 3
    assert report["final_verdict"]["can_create_three_windows"] is True
    assert all(item["accepted"] for item in report["window_audit"])


def test_partial_overlap_is_counted(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    path = tmp_path / "shadow.csv"
    write(path, rows("LIVE_BASELINE", 20, start, prefix="b") +
          rows("MOMENTUM_RELAXED", 20, start + timedelta(hours=10), prefix="c"))
    report = diagnose(path)
    assert report["overlap"]["exists"] is True
    assert report["overlap"]["baseline_trades"] == 10
    assert report["overlap"]["candidate_trades"] == 10


def test_no_overlap_has_clear_rejection(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    path = tmp_path / "shadow.csv"
    write(path, rows("LIVE_BASELINE", 10, start, prefix="b") +
          rows("MOMENTUM_RELAXED", 10, start + timedelta(days=3), prefix="c"))
    report = diagnose(path)
    assert report["overlap"]["exists"] is False
    assert report["window_audit"][-1]["rejection_reasons"] == ["No common time overlap."]


def test_duplicate_shadow_ids_are_reported(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    data = rows("LIVE_BASELINE", 2, start, prefix="same")
    data[1]["shadow_trade_id"] = data[0]["shadow_trade_id"]
    path = tmp_path / "shadow.csv"
    write(path, data)
    report = diagnose(path)
    assert report["deduplication"]["unique_shadow_trade_ids"] == 1
    assert report["deduplication"]["duplicate_shadow_trade_ids"] == 1
    assert report["deduplication"]["duplicate_examples"][0]["occurrences"] == 2


def test_naive_and_aware_timestamps_are_audited(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    path = tmp_path / "shadow.csv"
    write(path, rows("LIVE_BASELINE", 1, start, prefix="aware") +
          rows("LIVE_BASELINE", 1, start + timedelta(hours=1), prefix="naive", naive=True))
    stats = diagnose(path)["strategies"]["LIVE_BASELINE"]
    assert stats["naive_timestamps"] == 1
    assert stats["utc_aware_timestamps"] == 1


def test_different_ranges_produce_actionable_window_reason(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    path = tmp_path / "shadow.csv"
    write(path, rows("LIVE_BASELINE", 107, start, prefix="b") +
          rows("MOMENTUM_RELAXED", 121, start + timedelta(days=10), prefix="c"))
    report = diagnose(path)
    assert report["final_verdict"]["can_create_three_windows"] is False
    assert "required" in report["final_verdict"]["reason"] or "overlap" in report["final_verdict"]["reason"]


def test_json_output_contains_full_report(tmp_path):
    source, output = tmp_path / "shadow.csv", tmp_path / "diagnostic.json"
    write(source, [])
    assert main(["--input", str(source), "--json-output", str(output)]) == 0
    payload = output.read_text(encoding="utf-8")
    assert '"source"' in payload and '"final_verdict"' in payload
