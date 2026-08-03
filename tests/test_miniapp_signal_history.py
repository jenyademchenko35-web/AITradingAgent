import os

from tests.miniapp_intelligence_support import DECISION_FIELDS, decision_row, repository, write_csv


def test_history_contains_only_real_scoped_rows_and_paginates(tmp_path):
    rows = [decision_row(timestamp=f"2026-08-03T10:0{i}:00Z", cycle_id=f"cycle-{i}", confidence=str(80 + i)) for i in range(5)]
    rows.append(decision_row(symbol="ETH/USDT", cycle_id="other"))
    write_csv(tmp_path / "decision_debug.csv", rows, DECISION_FIELDS)
    page = repository(tmp_path).signal_history("BTCUSDT", "1h", page=2, page_size=2)
    assert page.status == "OK" and page.total == 5
    assert page.count == 2 and page.page == 2
    assert [item.cycle_id for item in page.items] == ["cycle-2", "cycle-3"]
    assert all(item.source == "DECISION_DEBUG" for item in page.items)


def test_empty_and_malformed_history_have_safe_status(tmp_path):
    assert repository(tmp_path).signal_history("BTCUSDT", "1h").status == "NO_HISTORY"
    (tmp_path / "decision_debug.csv").write_text('timestamp,symbol\n"unterminated', encoding="utf-8")
    result = repository(tmp_path, ttl=.1).signal_history("BTCUSDT", "1h")
    assert result.status == "NO_HISTORY" and result.items == ()


def test_cache_invalidates_when_file_mtime_or_size_changes(tmp_path):
    path = tmp_path / "decision_debug.csv"
    write_csv(path, [decision_row(confidence="80")], DECISION_FIELDS)
    data = repository(tmp_path, ttl=600)
    assert data.signal_history("BTCUSDT", "1h").items[0].confidence == 80
    write_csv(path, [decision_row(confidence="100")], DECISION_FIELDS)
    os.utime(path, None)
    assert data.signal_history("BTCUSDT", "1h").items[0].confidence == 100
