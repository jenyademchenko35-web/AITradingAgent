import copy
import json
import sqlite3

import pytest

from research_lab_v2 import forward_monitor as monitor
from research_lab_v2 import forward_validation as validator
from research_lab_v2.database import ResearchDatabase


@pytest.fixture
def empty_report(tmp_path):
    path = tmp_path / "research.db"
    ResearchDatabase(path).initialize()
    return validator.build_report(path)


def _counts(report, eligible=12, match=3):
    report = copy.deepcopy(report)
    report["total_eligible"] = eligible
    report["primary"]["match"]["n"] = match
    report["primary"]["complement"]["n"] = eligible - match
    report["secondary_adx"]["available_n"] = eligible - 1 if eligible else 0
    report["secondary_adx"]["unavailable_n"] = 1 if eligible else 0
    return report


@pytest.mark.parametrize(
    ("eligible", "match", "status", "milestone"),
    [
        (0, 0, "ACCUMULATING", None),
        (12, 3, "ACCUMULATING", None),
        (24, 3, "ACCUMULATING", None),
        (25, 3, "MILESTONE_25", 25),
        (49, 3, "MILESTONE_25", 25),
        (50, 3, "MILESTONE_50", 50),
        (99, 3, "MILESTONE_50", 50),
        (100, 3, "MILESTONE_100", 100),
        (199, 40, "MILESTONE_100", 100),
        (200, 39, "MILESTONE_100", 200),
        (200, 40, "REVIEW_READY", 200),
        (201, 41, "REVIEW_READY", 200),
    ],
)
def test_status_and_normal_exit(empty_report, monkeypatch, capsys, eligible, match, status, milestone):
    report = _counts(empty_report, eligible, match)
    before = copy.deepcopy(report)
    calls = []

    def build_report(path):
        calls.append(path)
        return report

    monkeypatch.setattr(validator, "build_report", build_report)
    assert monitor.main(["unused.db", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == status
    assert summary["milestone"] == milestone
    assert summary["total_eligible"] == eligible
    assert summary["primary_match_n"] == match
    assert calls == ["unused.db"]
    assert report == before


@pytest.mark.parametrize("reason", [r for r in validator._EXCLUSION_ORDER if r != "pre_cutoff"] + ["future_reason"])
@pytest.mark.parametrize(("eligible", "match"), [(12, 3), (50, 3), (200, 40)])
def test_any_non_pre_cutoff_exclusion_alerts(empty_report, monkeypatch, capsys, reason, eligible, match):
    report = _counts(empty_report, eligible, match)
    report["exclusions"][reason] = 1
    report["exclusions"]["pre_cutoff"] = 2
    report["total_excluded"] = 3
    monkeypatch.setattr(validator, "build_report", lambda path: report)
    assert monitor.main(["unused.db", "--json"]) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "INTEGRITY_ALERT"
    assert summary["post_cutoff_exclusions"] == 1
    assert summary["total_excluded"] == 3


def test_pre_cutoff_alone_does_not_alert(empty_report):
    report = _counts(empty_report)
    report["exclusions"]["pre_cutoff"] = 7
    report["total_excluded"] = 7
    summary = monitor.summarize_report(report)
    assert summary["status"] == "ACCUMULATING"
    assert summary["post_cutoff_exclusions"] == 0


_REQUIRED_PATHS = [
    ("cohort_version",), ("cutoff_utc",), ("primary_rule",), ("secondary_rule",),
    ("total_eligible",), ("total_excluded",), ("exclusions",),
    ("primary",), ("primary", "match"), ("primary", "match", "n"),
    ("primary", "complement"), ("primary", "complement", "n"),
    ("secondary_adx",), ("secondary_adx", "available_n"), ("secondary_adx", "unavailable_n"),
] + [("exclusions", reason) for reason in validator._EXCLUSION_ORDER]


@pytest.mark.parametrize("path", _REQUIRED_PATHS)
@pytest.mark.parametrize("missing", [True, False])
def test_required_fields_fail_closed(empty_report, monkeypatch, capsys, path, missing):
    report = _counts(empty_report)
    parent = report
    for key in path[:-1]:
        parent = parent[key]
    if missing:
        del parent[path[-1]]
    else:
        parent[path[-1]] = None
    monkeypatch.setattr(validator, "build_report", lambda path: report)
    assert monitor.main(["unused.db", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "execution/configuration failure" in captured.err


@pytest.mark.parametrize("bad", [True, False, -1, 1.0, "12", float("nan")])
def test_counts_require_nonnegative_integers(empty_report, monkeypatch, capsys, bad):
    report = _counts(empty_report)
    report["total_eligible"] = bad
    monkeypatch.setattr(validator, "build_report", lambda path: report)
    assert monitor.main(["unused.db"]) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("cohort_version",), "other"),
        (("cutoff_utc",), "invalid"),
        (("cutoff_utc",), "2026-09-29T17:11:10.650435"),
        (("cutoff_utc",), "2000-01-01T00:00:00+00:00"),
        (("primary_rule",), {}),
        (("secondary_rule",), {}),
        (("total_excluded",), 1),
        (("primary", "match", "n"), 13),
        (("secondary_adx", "available_n"), 13),
        (("exclusions", "pre_cutoff"), True),
        (("exclusions", "future_reason"), "bad"),
    ],
)
def test_inconsistent_or_changed_contract_fails(empty_report, monkeypatch, capsys, path, value):
    report = _counts(empty_report)
    parent = report
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    monkeypatch.setattr(validator, "build_report", lambda path: report)
    assert monitor.main(["unused.db"]) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("report", [None, [], "bad"])
def test_invalid_report_type(empty_report, monkeypatch, capsys, report):
    monkeypatch.setattr(validator, "build_report", lambda path: report)
    assert monitor.main(["unused.db"]) == 1
    assert capsys.readouterr().out == ""


def test_json_shape_and_determinism(empty_report, monkeypatch, capsys):
    report = _counts(empty_report)
    monkeypatch.setattr(validator, "build_report", lambda path: report)
    assert monitor.main(["unused.db", "--json"]) == 0
    first = capsys.readouterr().out
    assert monitor.main(["unused.db", "--json"]) == 0
    assert capsys.readouterr().out == first
    assert json.loads(first) == {
        "cohort_version": validator.COHORT_VERSION,
        "cutoff_utc": report["cutoff_utc"],
        "primary_rule": report["primary_rule"], "secondary_rule": report["secondary_rule"],
        "total_eligible": 12, "total_excluded": 0, "post_cutoff_exclusions": 0,
        "primary_match_n": 3, "primary_complement_n": 9,
        "secondary_adx_available_n": 11, "secondary_adx_unavailable_n": 1,
        "status": "ACCUMULATING", "milestone": None,
    }


def test_human_output_is_concise(empty_report, monkeypatch, capsys):
    report = _counts(empty_report)
    report["observations"] = [{"shadow_trade_id": "must-not-be-printed"}]
    monkeypatch.setattr(validator, "build_report", lambda path: report)
    assert monitor.main(["unused.db"]) == 0
    output = capsys.readouterr().out
    assert "ACCUMULATING" in output
    assert "eligible=12" in output
    assert "must-not-be-printed" not in output
    assert len(output.splitlines()) == 5


@pytest.mark.parametrize("argv", [[], ["unused.db", "--unknown"]])
def test_cli_configuration_errors_exit_one(capsys, argv):
    assert monitor.main(argv) == 1
    assert "execution/configuration failure" in capsys.readouterr().err


def test_execution_failure_exit_one(monkeypatch, capsys):
    def fail(path):
        raise sqlite3.OperationalError("test failure")

    monkeypatch.setattr(validator, "build_report", fail)
    assert monitor.main(["unused.db", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "test failure" in captured.err


def test_monitor_uses_only_read_only_connections_and_leaves_database_unchanged(tmp_path, monkeypatch, capsys):
    path = tmp_path / "research.db"
    ResearchDatabase(path).initialize()
    # Keep the WAL database open so read-only SQLite does not create sidecars.
    with sqlite3.connect(path) as keeper:
        keeper.execute("SELECT count(*) FROM shadow_trade_outcomes").fetchone()
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
        connect = sqlite3.connect
        calls = []

        def read_only_connect(database, *args, **kwargs):
            assert kwargs.get("uri") is True
            assert str(database).endswith("?mode=ro")
            calls.append(database)
            connection = connect(database, *args, **kwargs)
            # Deny every operation except reads, including schema/data writes.
            connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK
                                      if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ)
                                      else sqlite3.SQLITE_DENY)
            return connection

        monkeypatch.setattr(sqlite3, "connect", read_only_connect)
        assert monitor.main([str(path)]) == 0
        assert monitor.main([str(path), "--json"]) == 0
        capsys.readouterr()
        assert len(calls) == 2
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
        assert before == after


def test_missing_database_is_not_created(tmp_path, capsys):
    path = tmp_path / "does-not-exist.db"
    assert monitor.main([str(path), "--json"]) == 1
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().out == ""
