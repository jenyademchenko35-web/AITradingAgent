import json
import traceback
from urllib.error import HTTPError

import pytest

from research_lab_v2 import forward_notifier as notifier
from research_lab_v2 import forward_validation as validator


def _summary(eligible=21, match=7, status=None, post_cutoff=0, milestone=None):
    if status is None:
        if post_cutoff:
            status = "INTEGRITY_ALERT"
        elif eligible >= 200 and match >= 40:
            status = "REVIEW_READY"
        elif eligible >= 100:
            status = "MILESTONE_100"
        elif eligible >= 50:
            status = "MILESTONE_50"
        elif eligible >= 25:
            status = "MILESTONE_25"
        else:
            status = "ACCUMULATING"
    if milestone is None:
        milestone = next((n for n in (200, 100, 50, 25) if eligible >= n), None)
    return {
        "status": status,
        "milestone": milestone,
        "total_eligible": eligible,
        "primary_match_n": match,
        "primary_complement_n": eligible - match,
        "post_cutoff_exclusions": post_cutoff,
    }


def _report(reason=None):
    exclusions = {name: 0 for name in validator._EXCLUSION_ORDER}
    if reason:
        exclusions[reason] = 1
    return {"exclusions": exclusions}


def _state(**updates):
    state = notifier.default_state()
    state.update(updates)
    return state


def _decision(eligible=21, match=7, state=None, status=None, reason=None):
    post_cutoff = int(reason is not None)
    return notifier.decide(
        _report(reason),
        _summary(eligible, match, status, post_cutoff),
        state or notifier.default_state(),
    )


@pytest.mark.parametrize(
    ("previous", "eligible", "expected"),
    [(None, 21, None), (None, 25, 25), (25, 25, None), (None, 55, 50), (50, 100, 100)],
)
def test_milestone_idempotency(previous, eligible, expected):
    decision = _decision(eligible, state=_state(highest_milestone_notified=previous))
    assert decision["action"] == ("NOTIFY_MILESTONE" if expected else "NONE")
    if expected:
        assert decision["next_state"]["highest_milestone_notified"] == expected


def test_milestone_never_downgrades():
    decision = _decision(55, state=_state(highest_milestone_notified=100))
    assert decision["action"] == "NONE"
    assert decision["next_state"]["highest_milestone_notified"] == 100


def test_milestone_uses_canonical_summary_value():
    decision = _decision(55, state=_state(highest_milestone_notified=None))
    assert decision["action"] == "NOTIFY_MILESTONE"
    assert decision["next_state"]["highest_milestone_notified"] == 50

    report = _report()
    summary = _summary(55, status="MILESTONE_25", milestone=25)
    decision = notifier.decide(report, summary, notifier.default_state())
    assert decision["action"] == "NOTIFY_MILESTONE"
    assert decision["next_state"]["highest_milestone_notified"] == 25


def test_exact_milestone_50_uses_canonical_summary_value():
    report = _report()
    summary = _summary(55, status="MILESTONE_50", milestone=50)
    decision = notifier.decide(report, summary, notifier.default_state())
    assert decision["action"] == "NOTIFY_MILESTONE"
    assert decision["milestone"] == 50
    assert decision["next_state"]["highest_milestone_notified"] == 50


def test_review_ready_is_once_and_send_failure_does_not_commit(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    notifier.write_state(state_path, notifier.default_state())
    before = state_path.read_bytes()
    report = _report()
    summary = _summary(204, 44)
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: report)
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: summary)
    monkeypatch.setenv(notifier.TOKEN_ENV, "T")
    monkeypatch.setenv(notifier.CHAT_ID_ENV, "C")
    monkeypatch.setattr(
        notifier,
        "send_telegram_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(notifier.TelegramError("failed")),
    )
    assert notifier.main(["unused.db", "--state-file", str(state_path), "--send"]) == 1
    assert state_path.read_bytes() == before
    assert notifier.load_state(state_path)["review_ready_notified"] is False


def test_review_ready_success_persists_and_duplicate_is_silent(tmp_path, monkeypatch):
    report = _report()
    summary = _summary(200, 44)
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: report)
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: summary)
    monkeypatch.setenv(notifier.TOKEN_ENV, "T")
    monkeypatch.setenv(notifier.CHAT_ID_ENV, "C")
    sent = []
    monkeypatch.setattr(notifier, "send_telegram_message", lambda *args, **kwargs: sent.append(args))
    state_path = tmp_path / "state.json"

    assert notifier.main(["unused.db", "--state-file", str(state_path), "--send"]) == 0
    assert notifier.load_state(state_path)["review_ready_notified"] is True
    assert notifier.load_state(state_path)["highest_milestone_notified"] == 200
    assert notifier.main(["unused.db", "--state-file", str(state_path), "--send"]) == 0
    assert len(sent) == 1


def test_review_ready_and_milestone_200_send_once(tmp_path, monkeypatch):
    report = _report()
    summary = _summary(200, 44, status="REVIEW_READY", milestone=200)
    assert notifier.decide(report, summary, notifier.default_state())["action"] == "NOTIFY_REVIEW_READY"
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: report)
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: summary)
    monkeypatch.setenv(notifier.TOKEN_ENV, "T")
    monkeypatch.setenv(notifier.CHAT_ID_ENV, "C")
    sent = []
    monkeypatch.setattr(notifier, "send_telegram_message", lambda *args, **kwargs: sent.append(args))
    state_path = tmp_path / "state.json"

    assert notifier.main(["unused.db", "--state-file", str(state_path), "--send"]) == 0
    assert len(sent) == 1
    assert notifier.load_state(state_path)["highest_milestone_notified"] == 200
    assert notifier.main(["unused.db", "--state-file", str(state_path), "--send"]) == 0
    assert len(sent) == 1


def test_integrity_fingerprint_and_clean_transition():
    alert = _decision(31, reason="snapshot_invalid")
    assert alert["action"] == "NOTIFY_INTEGRITY"
    active = alert["next_state"]
    assert _decision(31, state=active, reason="snapshot_invalid")["action"] == "NONE"
    assert _decision(31, state=active, reason="invalid_pnl")["action"] == "NOTIFY_INTEGRITY"
    clean = _decision(31, state=active)
    assert clean["action"] == "NONE"
    assert clean["state_change_required"] is True
    assert clean["next_state"]["integrity_active"] is False
    assert _decision(31, state=clean["next_state"], reason="snapshot_invalid")["action"] == "NOTIFY_INTEGRITY"


def test_integrity_overrides_milestone_and_review_ready():
    decision = _decision(200, 44, state=_state(highest_milestone_notified=25), reason="snapshot_invalid")
    assert decision["action"] == "NOTIFY_INTEGRITY"


def test_integrity_preserves_pending_milestone_and_review_ready():
    report = _report("snapshot_invalid")
    summary = _summary(200, 44, status="INTEGRITY_ALERT", post_cutoff=1, milestone=200)
    state = _state(highest_milestone_notified=25, review_ready_notified=False)
    decision = notifier.decide(report, summary, state)
    assert decision["action"] == "NOTIFY_INTEGRITY"
    assert decision["next_state"]["highest_milestone_notified"] == 25
    assert decision["next_state"]["review_ready_notified"] is False

    clean_report = _report()
    clean_summary = _summary(200, 44, status="REVIEW_READY", milestone=200)
    recovered = notifier.decide(clean_report, clean_summary, decision["next_state"])
    assert recovered["action"] == "NONE"
    assert recovered["state_change_required"] is True
    ready = notifier.decide(clean_report, clean_summary, recovered["next_state"])
    assert ready["action"] == "NOTIFY_REVIEW_READY"
    assert ready["next_state"]["highest_milestone_notified"] == 200


def test_integrity_preserves_pending_milestone_after_recovery():
    alert_report = _report("snapshot_invalid")
    alert_summary = _summary(55, status="INTEGRITY_ALERT", post_cutoff=1, milestone=50)
    alert = notifier.decide(alert_report, alert_summary, notifier.default_state())
    assert alert["next_state"]["highest_milestone_notified"] is None
    clean = notifier.decide(_report(), _summary(55, status="MILESTONE_50", milestone=50), alert["next_state"])
    assert clean["action"] == "NONE"
    assert clean["state_change_required"] is True
    pending = notifier.decide(_report(), _summary(55, status="MILESTONE_50", milestone=50), clean["next_state"])
    assert pending["action"] == "NOTIFY_MILESTONE"
    assert pending["next_state"]["highest_milestone_notified"] == 50


@pytest.mark.parametrize(
    "bad",
    [
        lambda state: {**state, "schema_version": 2},
        lambda state: {**state, "highest_milestone_notified": True},
        lambda state: {**state, "highest_milestone_notified": 30},
        lambda state: {key: value for key, value in state.items() if key != "integrity_active"},
    ],
)
def test_state_rejects_corrupt_values(bad):
    with pytest.raises(notifier.StateError):
        notifier._validate_state(bad(notifier.default_state()))


@pytest.mark.parametrize(
    "updates",
    [
        {"integrity_active": False, "last_integrity_fingerprint": "abc"},
        {"integrity_active": True, "last_integrity_fingerprint": None},
        {"integrity_active": True, "last_integrity_fingerprint": ""},
        {"integrity_active": True, "last_integrity_fingerprint": "   "},
        {"integrity_active": "true", "last_integrity_fingerprint": "abc"},
    ],
)
def test_state_rejects_incoherent_integrity_state(updates):
    state = notifier.default_state()
    state.update(updates)
    with pytest.raises(notifier.StateError):
        notifier._validate_state(state)


def test_atomic_state_write_and_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    notifier.write_state(path, notifier.default_state())
    assert path.stat().st_mode & 0o777 == 0o600
    original = path.read_bytes()
    path.write_text("{broken")
    with pytest.raises(notifier.StateError):
        notifier.load_state(path)
    path.write_bytes(original)
    assert notifier.load_state(path) == notifier.default_state()
    path.write_text("{broken")
    with pytest.raises(notifier.StateError):
        notifier.write_state(path, notifier.default_state())
    assert path.read_text() == "{broken"


def test_dry_run_does_not_create_or_modify_state(tmp_path, monkeypatch, capsys):
    report = _report()
    summary = _summary(25)
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: report)
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: summary)
    path = tmp_path / "state.json"
    assert notifier.main(["unused.db", "--state-file", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "NOTIFY_MILESTONE"
    assert not path.exists()


def test_dry_run_preserves_existing_state_bytes_and_temp_files(tmp_path, monkeypatch, capsys):
    report = _report()
    summary = _summary(25)
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: report)
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: summary)
    path = tmp_path / "state.json"
    notifier.write_state(path, notifier.default_state())
    before = path.read_bytes()
    assert notifier.main(["unused.db", "--state-file", str(path), "--json"]) == 0
    assert path.read_bytes() == before
    assert not any(item.name.startswith(".state.json.") for item in tmp_path.iterdir())
    capsys.readouterr()


def test_send_failure_leaves_state_byte_for_byte_unchanged(tmp_path, monkeypatch, capsys):
    report = _report()
    summary = _summary(25)
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: report)
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: summary)
    monkeypatch.setenv(notifier.TOKEN_ENV, "T")
    monkeypatch.setenv(notifier.CHAT_ID_ENV, "C")
    path = tmp_path / "state.json"
    notifier.write_state(path, notifier.default_state())
    before = path.read_bytes()
    monkeypatch.setattr(
        notifier,
        "send_telegram_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(notifier.TelegramError("network failure")),
    )
    assert notifier.main(["unused.db", "--state-file", str(path), "--send"]) == 1
    assert path.read_bytes() == before
    assert "T" not in capsys.readouterr().err


def test_missing_credentials_leave_required_notification_state_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(notifier.TOKEN_ENV, raising=False)
    monkeypatch.delenv(notifier.CHAT_ID_ENV, raising=False)
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: _report())
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: _summary(25))
    path = tmp_path / "state.json"
    notifier.write_state(path, notifier.default_state())
    before = path.read_bytes()
    assert notifier.main(["unused.db", "--state-file", str(path), "--send"]) == 1
    assert path.read_bytes() == before
    capsys.readouterr()


def test_silent_recovery_persists_without_credentials(tmp_path, monkeypatch):
    alert_report = _report("snapshot_invalid")
    alert_summary = _summary(31, post_cutoff=1)
    active_state = notifier.decide(alert_report, alert_summary, notifier.default_state())["next_state"]
    path = tmp_path / "state.json"
    notifier.write_state(path, active_state)

    monkeypatch.delenv(notifier.TOKEN_ENV, raising=False)
    monkeypatch.delenv(notifier.CHAT_ID_ENV, raising=False)
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: _report())
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: _summary(31))
    monkeypatch.setattr(
        notifier,
        "send_telegram_message",
        lambda *args, **kwargs: pytest.fail("silent recovery must not send"),
    )
    assert notifier.main(["unused.db", "--state-file", str(path), "--send"]) == 0
    clean = notifier.load_state(path)
    assert clean["integrity_active"] is False
    assert clean["last_integrity_fingerprint"] is None


def test_successful_send_commits_state(tmp_path, monkeypatch):
    report = _report()
    summary = _summary(25)
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: report)
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: summary)
    monkeypatch.setenv(notifier.TOKEN_ENV, "T")
    monkeypatch.setenv(notifier.CHAT_ID_ENV, "C")
    sent = []
    monkeypatch.setattr(notifier, "send_telegram_message", lambda *args, **kwargs: sent.append(args))
    path = tmp_path / "state.json"
    assert notifier.main(["unused.db", "--state-file", str(path), "--send"]) == 0
    assert len(sent) == 1
    assert notifier.load_state(path)["highest_milestone_notified"] == 25


def test_missing_credentials_is_a_configuration_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(notifier.TOKEN_ENV, raising=False)
    monkeypatch.delenv(notifier.CHAT_ID_ENV, raising=False)
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: _report())
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: _summary(25))
    assert notifier.main(["unused.db", "--state-file", str(tmp_path / "state.json"), "--send"]) == 1
    assert "credentials" in capsys.readouterr().err


def test_integrity_exit_and_duplicate_deduplication(tmp_path, monkeypatch):
    report = _report("snapshot_invalid")
    summary = _summary(31, post_cutoff=1)
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: report)
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: summary)
    monkeypatch.setenv(notifier.TOKEN_ENV, "T")
    monkeypatch.setenv(notifier.CHAT_ID_ENV, "C")
    sent = []
    monkeypatch.setattr(notifier, "send_telegram_message", lambda *args, **kwargs: sent.append(args))
    path = tmp_path / "state.json"
    assert notifier.main(["unused.db", "--state-file", str(path), "--send"]) == 2
    assert notifier.main(["unused.db", "--state-file", str(path), "--send"]) == 2
    assert len(sent) == 1


def test_atomic_replace_failure_preserves_state_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    notifier.write_state(path, notifier.default_state())
    before = path.read_bytes()
    monkeypatch.setattr(notifier.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("replace")))
    with pytest.raises(notifier.StateError):
        notifier.write_state(path, notifier.default_state())
    assert path.read_bytes() == before
    assert not any(item.name.startswith(".state.json.") for item in tmp_path.iterdir())


def test_fsync_failure_before_replace_preserves_state_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    notifier.write_state(path, notifier.default_state())
    before = path.read_bytes()

    def fail_file_fsync(fd):
        raise OSError("fsync failure")

    monkeypatch.setattr(notifier.os, "fsync", fail_file_fsync)
    with pytest.raises(notifier.StateError):
        notifier.write_state(path, notifier.default_state())
    assert path.read_bytes() == before
    assert not any(item.name.startswith(".state.json.") for item in tmp_path.iterdir())


def test_cli_fsync_failure_returns_one_and_preserves_state(tmp_path, monkeypatch):
    report = _report()
    summary = _summary(25)
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: report)
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: summary)
    monkeypatch.setenv(notifier.TOKEN_ENV, "T")
    monkeypatch.setenv(notifier.CHAT_ID_ENV, "C")
    monkeypatch.setattr(notifier, "send_telegram_message", lambda *args, **kwargs: None)
    path = tmp_path / "state.json"
    notifier.write_state(path, notifier.default_state())
    before = path.read_bytes()
    monkeypatch.setattr(notifier.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync failure")))
    assert notifier.main(["unused.db", "--state-file", str(path), "--send"]) == 1
    assert path.read_bytes() == before
    assert not any(item.name.startswith(".state.json.") for item in tmp_path.iterdir())


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def test_telegram_response_handling(monkeypatch):
    monkeypatch.setattr(notifier.urllib.request, "urlopen", lambda *a, **k: _Response(b'{"ok":true}'))
    notifier.send_telegram_message("T", "C", "hello")
    for payload in (b'{"ok":false}', b"not-json", b"{}", b"[]", b'{"ok":1}', None):
        monkeypatch.setattr(notifier.urllib.request, "urlopen", lambda *a, **k: _Response(payload))
        with pytest.raises(notifier.TelegramError) as error:
            notifier.send_telegram_message("T", "C", "hello")
        assert error.value.__cause__ is None
        assert error.value.__context__ is None


def test_telegram_http_failure_is_sanitized(monkeypatch):
    token = "".join(["TEST_SECRET_", "TOKEN_123"])
    chat_id = "".join(["TEST_", "CHAT_456"])
    sensitive_url = f"https://api.telegram.org/bot{token}/sendMessage"
    sensitive_original_message = "".join(["SENSITIVE_", "HTTP_FAILURE_456"])
    failure = HTTPError(sensitive_url, 500, sensitive_original_message, {}, None)
    monkeypatch.setattr(
        notifier.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )
    with pytest.raises(notifier.TelegramError) as error:
        notifier.send_telegram_message(token, chat_id, "hello")
    assert token not in str(error.value)
    assert chat_id not in str(error.value)
    assert sensitive_url not in str(error.value)
    assert sensitive_original_message not in str(error.value)
    assert "api.telegram.org" not in str(error.value)
    formatted = "".join(traceback.format_exception(error.value))
    assert token not in formatted
    assert chat_id not in formatted
    assert sensitive_url not in formatted
    assert sensitive_original_message not in formatted
    assert "api.telegram.org" not in formatted
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_cli_telegram_failure_stderr_and_state_are_sanitized(tmp_path, monkeypatch, capsys):
    report = _report()
    summary = _summary(25)
    monkeypatch.setattr(notifier.validator, "build_report", lambda _: report)
    monkeypatch.setattr(notifier.monitor, "summarize_report", lambda _: summary)
    monkeypatch.setenv(notifier.TOKEN_ENV, "TEST_SECRET_TOKEN_123")
    monkeypatch.setenv(notifier.CHAT_ID_ENV, "TEST_CHAT_456")
    monkeypatch.setattr(
        notifier.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            HTTPError(
                "https://api.telegram.org/botTEST_SECRET_TOKEN_123/sendMessage",
                500,
                "SENSITIVE_ORIGINAL_RESPONSE",
                {},
                None,
            )
        ),
    )
    path = tmp_path / "state.json"
    notifier.write_state(path, notifier.default_state())
    before = path.read_bytes()
    assert notifier.main(["unused.db", "--state-file", str(path), "--send"]) == 1
    error = capsys.readouterr().err
    assert "TEST_SECRET_TOKEN_123" not in error
    assert "TEST_CHAT_456" not in error
    assert "https://api.telegram.org/botTEST_SECRET_TOKEN_123/sendMessage" not in error
    assert "SENSITIVE_ORIGINAL_RESPONSE" not in error
    assert path.read_bytes() == before


def test_telegram_network_failure_is_sanitized(monkeypatch):
    for failure in (OSError("token-bearing URL"), TimeoutError("TOKEN CHAT timeout")):
        monkeypatch.setattr(
            notifier.urllib.request,
            "urlopen",
            lambda *args, _failure=failure, **kwargs: (_ for _ in ()).throw(_failure),
        )
        with pytest.raises(notifier.TelegramError) as error:
            notifier.send_telegram_message("TOKEN", "CHAT", "hello")
        assert "TOKEN" not in str(error.value)
        assert "CHAT" not in str(error.value)


def test_integrity_fingerprint_is_order_independent():
    first = {"snapshot_invalid": 1, "invalid_pnl": 2, "pre_cutoff": 9}
    second = {"pre_cutoff": 9, "invalid_pnl": 2, "snapshot_invalid": 1}
    assert notifier._integrity_fingerprint({"exclusions": first}) == notifier._integrity_fingerprint(
        {"exclusions": second}
    )
    changed = {"pre_cutoff": 9, "invalid_pnl": 3, "snapshot_invalid": 1}
    assert notifier._integrity_fingerprint({"exclusions": first}) != notifier._integrity_fingerprint(
        {"exclusions": changed}
    )
