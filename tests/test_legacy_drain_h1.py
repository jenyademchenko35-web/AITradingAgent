"""Adversarial recovery tests; all mutations are confined to pytest tmp roots."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

import legacy_drain as drain
import legacy_drain_safety as safety
from tests.test_legacy_drain import (model, _hot, _cold, _run, _validator,
                                     _next_trigger, SimulatedCrash)


@pytest.fixture
def trusted_transition_root():
    """Model the production V2 transition hierarchy without relying on umask."""
    trusted_root = Path(tempfile.mkdtemp(prefix="legacy-drain-h1-trusted-")).resolve()
    try:
        trusted_root.chmod(0o700)
        v2_root = trusted_root / "AITradingAgent-v2"
        v2_root.mkdir(mode=0o755)
        transition_root = v2_root / drain.TRANSITION_DIR
        transition_root.mkdir(mode=0o700)
        transition_root.chmod(0o700)
        yield transition_root
    finally:
        shutil.rmtree(trusted_root)


def crash_at(stage):
    def fault(actual):
        if actual == stage:
            raise SimulatedCrash()
    return fault


def pending(model, stage="after_tombstone", cold=False):
    first = (_cold if cold else _hot)(model["legacy"], "20260830_000000")
    second = _hot(model["legacy"], "20260830_000001")
    with pytest.raises(SimulatedCrash):
        _run(model, fault=crash_at(stage))
    journal = model["v2"] / drain.TRANSITION_DIR / f"{model['trigger'].name}.json"
    return first, second, journal, json.loads(journal.read_text())


def recover(model, **kwargs):
    trigger = _next_trigger(model, 2)
    result = drain.drain_one(model["drain_config"], trigger,
                            validate_trigger=_validator, lock_held=True, **kwargs)
    assert _validator(trigger)["verified"]
    assert _validator(model["trigger"])["verified"]
    return result


@pytest.mark.parametrize("field,value", [
    ("legacy_root", "/unrelated"),
    ("legacy_candidate", "/unrelated/20260830_000000"),
    ("tombstone_basename", "/unrelated/.20260830_000000.tombstone"),
    ("tombstone_basename", ".20260830_000000.x/../../outside.tombstone"),
    ("candidate_basename", "../20260830_000000"),
    ("candidate_basename", "20260830_000000_extra"),
    ("candidate_basename", "20269999_999999"),
    ("candidate_basename", "２０２６０８３０_００００００"),
    ("tombstone_basename", ".20260830_000000.similar.legacy-drain.tombstone"),
    ("legacy_candidate", "/legacy-similar-prefix/20260830_000000"),
    ("candidate_type", "UNKNOWN"),
    ("trigger_v2_point", "/unrelated/20260905_000000"),
    ("trigger_v2_timestamp", "20260905_000001"),
    ("state", "PLANNED_DELETE_ANYTHING"),
    ("action", "DELETED"),
])
def test_tampered_journal_fails_closed(model, field, value):
    _, second, journal, record = pending(model)
    tombstone = model["legacy"] / record["tombstone_basename"]
    before = safety.inventory(tombstone, record["directory_identity"])
    record[field] = value
    journal.write_text(json.dumps(record))
    deletes = []
    result = recover(model, delete_tree=deletes.append)
    assert result["state"] == "RECOVERY_BLOCKED", result
    assert not deletes and second.exists()
    assert safety.inventory(tombstone, record["directory_identity"]) == before


@pytest.mark.parametrize("stage", ["after_intent", "after_tombstone"])
@pytest.mark.parametrize("field", ["inode", "device"])
def test_recorded_identity_tampering(model, stage, field):
    first, second, journal, record = pending(model, stage)
    record["directory_identity"][field] += 1
    record["validation"]["directory_" + field] += 1
    journal.write_text(json.dumps(record))
    result = recover(model)
    assert result["state"] == "RECOVERY_BLOCKED"
    assert second.exists()
    assert first.exists() or (model["legacy"] / record["tombstone_basename"]).exists()


@pytest.mark.parametrize("stage", ["after_intent", "after_tombstone"])
@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_candidate_or_tombstone_replacement(model, stage, replacement):
    first, second, _, record = pending(model, stage)
    target = first if first.exists() else model["legacy"] / record["tombstone_basename"]
    preserved = target.with_name("preserved-original")
    target.rename(preserved)
    if replacement == "symlink":
        target.symlink_to(second, target_is_directory=True)
    else:
        shutil.copytree(preserved, target)
    result = recover(model)
    assert result["state"] == "RECOVERY_BLOCKED", result
    assert second.exists() and preserved.exists()


@pytest.mark.parametrize("stage", ["before_rename", "after_rename", "recovery"])
def test_open_fd_recheck(model, stage):
    if stage == "recovery":
        _, second, _, record = pending(model)
        model["drain_config"] = replace(model["drain_config"], open_fd_check=lambda _: True)
        result = recover(model)
        assert second.exists() and (model["legacy"] / record["tombstone_basename"]).exists()
    else:
        first = _hot(model["legacy"], "20260830_000000")
        calls = []
        def checker(path):
            calls.append(path)
            return len(calls) >= 2 if stage == "before_rename" else path.name.startswith(".")
        model["drain_config"] = replace(model["drain_config"], open_fd_check=checker)
        result = _run(model)
        assert first.exists() if stage == "before_rename" else list(model["legacy"].glob(".*.tombstone"))
    assert result["action"] == "SKIPPED"


@pytest.mark.parametrize("kind", ["missing", "error", "warning", "ambiguous"])
def test_lsof_fail_closed_policy(model, monkeypatch, kind):
    first = _hot(model["legacy"], "20260830_000000")
    model["drain_config"] = replace(model["drain_config"], open_fd_check=None)
    monkeypatch.setattr(drain.shutil, "which", lambda _: None if kind == "missing" else "/fixture/lsof")
    monkeypatch.setattr(drain.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 2 if kind == "error" else 0 if kind == "ambiguous" else 1,
                                                    "", "cannot inspect" if kind == "warning" else ""))
    result = _run(model)
    assert result["action"] == "SKIPPED" and first.exists()


@pytest.mark.parametrize("payload", ["{broken", "[]", '{"state":"VALIDATED","state":"TOMBSTONED"}'])
def test_malformed_journal(model, payload):
    _, second, journal, record = pending(model)
    journal.write_text(payload)
    result = recover(model)
    assert result["state"] == "RECOVERY_BLOCKED"
    assert second.exists() and (model["legacy"] / record["tombstone_basename"]).exists()


@pytest.mark.parametrize("stage", ["after_journal_created", "after_intent", "after_rename_before_journal", "after_tombstone", "after_delete"])
@pytest.mark.parametrize("cold", [False, True])
def test_crash_windows_hot_and_cold(model, stage, cold):
    first, second, _, record = pending(model, stage, cold)
    result = recover(model)
    assert result["state"] == "RECOVERY_CONSUMED_RUN", result
    assert second.exists()
    if stage in {"after_journal_created", "after_intent"}:
        assert first.exists()
    else:
        assert not first.exists()
        assert not (model["legacy"] / record["tombstone_basename"]).exists()


def test_fully_retargeted_journal_cannot_delete_second(model):
    _, second, journal, record = pending(model)
    record.update(candidate_basename=second.name, legacy_candidate=str(second),
                  tombstone_basename=drain._tombstone_name(second.name, model["trigger"].name),
                  directory_identity=safety.check_directory(second))
    record["inventory"] = safety.inventory(second, record["directory_identity"])
    record["validation"]["directory_inode"] = second.stat().st_ino
    record["validation"]["directory_device"] = second.stat().st_dev
    journal.write_text(json.dumps(record))
    result = recover(model)
    assert result["state"] == "RECOVERY_BLOCKED" and second.exists()


def test_recovery_trigger_consumed_before_delete_crash(model):
    _, second, _, _ = pending(model)
    new = _next_trigger(model, 2)
    def crash(_):
        raise SimulatedCrash()
    with pytest.raises(SimulatedCrash):
        drain.drain_one(model["drain_config"], new, validate_trigger=_validator,
                        lock_held=True, delete_tree=crash)
    result = drain.drain_one(model["drain_config"], new, validate_trigger=_validator, lock_held=True)
    assert result["state"] == "RECOVERY_CONSUMED_RUN" and second.exists()


def test_old_schema_pending_never_automigrated(model):
    _, second, journal, record = pending(model)
    record["schema_version"] = 1
    journal.write_text(json.dumps(record))
    assert recover(model)["state"] == "RECOVERY_BLOCKED"
    assert second.exists()


def test_invalid_transition_rejected_without_journal_write(model):
    journal = model["v2"] / "state.json"
    record = drain._record_base(model["drain_config"], model["trigger"])
    with pytest.raises(drain.LegacyDrainError, match="invalid state transition"):
        drain._write_state(journal, record, model["drain_config"], state="COMPLETED", action="DELETED")
    assert not journal.exists()


def test_partial_recovery_rejects_added_or_replaced_files(model):
    _, second, _, record = pending(model)
    target = model["legacy"] / record["tombstone_basename"]
    (target / "unexpected").write_text("not validated")
    assert recover(model)["state"] == "RECOVERY_BLOCKED"
    assert second.exists() and (target / "unexpected").read_text() == "not validated"


def test_descriptor_delete_refuses_nested_symlink(model):
    first = _hot(model["legacy"], "20260830_000000")
    second = _hot(model["legacy"], "20260830_000001")
    expected = safety.check_directory(first)
    inventory = safety.inventory(first, expected)
    (first / "escape").symlink_to(second, target_is_directory=True)
    with pytest.raises(drain.LegacyDrainError):
        safety.delete_bound(model["legacy"], first.name, expected, inventory)
    assert (first / "research.db").exists() and (second / "research.db").exists()


def test_immutable_intent_no_overwrite(model):
    _, _, journal, _ = pending(model)
    receipt = drain._receipt(journal)
    before = receipt.read_bytes()
    with pytest.raises(FileExistsError):
        safety.publish_json(receipt, {"replaced": True}, once=True)
    assert receipt.read_bytes() == before


def test_symlink_ancestor_is_rejected(model):
    alias = model["legacy"].parent / "alias"
    alias.symlink_to(model["legacy"].parent, target_is_directory=True)
    with pytest.raises(OSError):
        safety.check_directory(alias / "legacy")


def test_outside_sentinel_and_v2_unchanged_on_block(model):
    _, second, journal, record = pending(model)
    sentinel = model["source"] / "research.db"
    before = sentinel.read_bytes()
    record["legacy_candidate"] = str(model["source"])
    journal.write_text(json.dumps(record))
    assert recover(model)["state"] == "RECOVERY_BLOCKED"
    assert sentinel.read_bytes() == before and second.exists()


@pytest.mark.parametrize("state,action", [("COMPLETED", "DELETED"), ("STARTED", "PENDING"),
                                         ("RECOVERY_CONSUMED_RUN", "SKIPPED")])
def test_tampered_state_pair_cannot_hide_pending_candidate(model, state, action):
    _, second, journal, record = pending(model)
    record.update(state=state, action=action)
    journal.write_text(json.dumps(record))
    result = recover(model)
    assert result["state"] == "RECOVERY_BLOCKED" and second.exists()


def test_downgraded_fake_terminal_cannot_hide_tombstone(model):
    _, second, journal, record = pending(model)
    record.update(schema_version=1, state="COMPLETED", action="DELETED")
    journal.write_text(json.dumps(record))
    result = recover(model)
    assert result["state"] == "RECOVERY_BLOCKED" and second.exists()


@pytest.mark.parametrize("field", ["directory_inode", "directory_device"])
def test_validation_identity_mismatch_before_rename(model, monkeypatch, field):
    first = _hot(model["legacy"], "20260830_000000")
    validate = drain._validate_candidate
    def changed(*args):
        validation, fingerprint, size = validate(*args)
        validation[field] += 1
        return validation, fingerprint, size
    monkeypatch.setattr(drain, "_validate_candidate", changed)
    assert _run(model)["state"] == "RECOVERY_BLOCKED"
    assert first.exists()


def test_same_contents_new_inode_before_rename_rejected(model):
    first = _hot(model["legacy"], "20260830_000000")
    def replace_candidate(stage):
        if stage == "after_candidate_validated":
            preserved = first.with_name("preserved")
            first.rename(preserved)
            shutil.copytree(preserved, first)
    result = _run(model, fault=replace_candidate)
    assert result["state"] == "RECOVERY_BLOCKED" and first.exists()


def test_rename_never_clobbers_existing_destination(model):
    first = _hot(model["legacy"], "20260830_000000")
    destination = model["legacy"] / ".occupied"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        safety.rename_bound(model["legacy"], first.name, destination.name,
                            safety.check_directory(first), safety.check_directory(model["legacy"]))
    assert first.exists() and destination.exists()


def test_symlink_journal_never_followed(model):
    _, second, journal, _ = pending(model)
    original = journal.read_bytes()
    outside = model["source"] / "not-a-journal.json"
    outside.write_bytes(original)
    journal.unlink()
    journal.symlink_to(outside)
    assert recover(model)["state"] == "RECOVERY_BLOCKED"
    assert second.exists() and outside.read_bytes() == original


def test_delete_started_without_permit_blocked(model):
    _, second, journal, record = pending(model)
    record.update(state="DELETE_STARTED")
    journal.write_text(json.dumps(record))
    assert recover(model)["state"] == "RECOVERY_BLOCKED"
    assert second.exists()


def test_empty_partial_tombstone_requires_manual_review(model):
    first = _hot(model["legacy"], "20260830_000000")
    second = _hot(model["legacy"], "20260830_000001")
    def empty_but_leave_directory(path):
        for child in path.iterdir():
            child.unlink()
        raise OSError("crash before final rmdir")
    result = _run(model, delete_tree=empty_but_leave_directory)
    assert result["journal_action"] == "PENDING"
    assert recover(model)["state"] == "RECOVERY_BLOCKED"
    assert not first.exists() and second.exists()


def test_hard_link_in_tree_blocks_delete(model):
    first = _hot(model["legacy"], "20260830_000000")
    os.link(first / "research.db", model["source"] / "linked-db")
    result = _run(model)
    assert result["action"] == "SKIPPED" and first.exists()


def test_journal_write_failure_keeps_old_complete_json(trusted_transition_root, monkeypatch):
    path = trusted_transition_root / "journal.json"
    safety.publish_json(path, {"old": True})
    def fail_replace(*args, **kwargs):
        raise OSError("injected atomic publication failure")
    monkeypatch.setattr(safety.os, "replace", fail_replace)
    with pytest.raises(OSError):
        safety.publish_json(path, {"new": True})
    assert json.loads(path.read_text()) == {"old": True}
    assert not list(trusted_transition_root.glob(".*.partial"))


def test_production_like_transition_directory_is_trusted(trusted_transition_root):
    info = trusted_transition_root.stat()
    assert info.st_uid == os.geteuid()
    assert info.st_mode & 0o777 == 0o700
    path = trusted_transition_root / "accepted.json"
    safety.publish_json(path, {"trusted": True})
    assert safety.read_json(path) == {"trusted": True}


@pytest.mark.parametrize("mode", [0o720, 0o702], ids=["group-writable", "world-writable"])
def test_writable_transition_directory_is_rejected(trusted_transition_root, mode):
    transition_root = trusted_transition_root
    transition_root.chmod(mode)
    with pytest.raises(safety.DrainSafetyError, match="untrusted journal directory permissions"):
        safety.publish_json(transition_root / "rejected.json", {"trusted": False})


def test_transition_directory_ownership_mismatch_is_rejected(
        trusted_transition_root, monkeypatch):
    actual_uid = os.geteuid()
    monkeypatch.setattr(safety.os, "geteuid", lambda: actual_uid + 1)
    with pytest.raises(safety.DrainSafetyError, match="untrusted journal directory permissions"):
        safety._check_journal_parent(trusted_transition_root.stat())


@pytest.mark.parametrize("mode", [0o755, 0o1777], ids=["non-writable", "sticky-writable"])
def test_foreign_owned_ancestor_is_rejected(trusted_transition_root, monkeypatch, mode):
    ancestor = trusted_transition_root.parent
    ancestor.chmod(mode)
    actual_uid = os.geteuid()
    monkeypatch.setattr(safety.os, "geteuid", lambda: actual_uid + 1)
    with pytest.raises(safety.DrainSafetyError, match="untrusted journal ancestor owner"):
        safety._check_journal_ancestor(ancestor.stat())


def test_symlink_transition_parent_is_rejected(trusted_transition_root):
    actual = trusted_transition_root.parent / "actual-transition"
    actual.mkdir(mode=0o700)
    alias = trusted_transition_root.parent / "transition-alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(OSError):
        safety.publish_json(alias / "rejected.json", {"trusted": False})


def test_world_writable_tmp_ancestor_allows_owned_trusted_transition(trusted_transition_root):
    tmp_parent = trusted_transition_root.parent / "tmp-like"
    tmp_parent.mkdir(mode=0o700)
    tmp_parent.chmod(0o1777)
    v2_root = tmp_parent / "AITradingAgent-v2"
    v2_root.mkdir(mode=0o755)
    transition_root = v2_root / drain.TRANSITION_DIR
    transition_root.mkdir(mode=0o700)
    path = transition_root / "accepted.json"
    safety.publish_json(path, {"trusted-child": True})
    assert safety.read_json(path) == {"trusted-child": True}


def test_non_sticky_writable_ancestor_is_rejected(trusted_transition_root):
    untrusted_parent = trusted_transition_root.parent / "untrusted-parent"
    untrusted_parent.mkdir(mode=0o700)
    untrusted_parent.chmod(0o777)
    v2_root = untrusted_parent / "AITradingAgent-v2"
    v2_root.mkdir(mode=0o755)
    transition_root = v2_root / drain.TRANSITION_DIR
    transition_root.mkdir(mode=0o700)
    with pytest.raises(safety.DrainSafetyError, match="untrusted writable journal ancestor"):
        safety.publish_json(transition_root / "rejected.json", {"trusted-child": False})


def test_real_lsof_open_fd_is_detected(model):
    if shutil.which("lsof") is None:
        pytest.skip("platform has no lsof; missing policy tested separately")
    first = _hot(model["legacy"], "20260830_000000")
    with (first / "research.db").open("rb"):
        assert drain._default_open_fd_check(first) is True


def test_physical_delete_does_not_enter_replaced_subdirectory(model, monkeypatch):
    first = _hot(model["legacy"], "20260830_000000")
    second = _hot(model["legacy"], "20260830_000001")
    nested = first / "nested"
    nested.mkdir()
    (nested / "keep").write_text("original")
    expected = safety.check_directory(first)
    inventory = safety.inventory(first, expected)
    real_open = safety.os.open
    calls = 0
    def racing_open(path, flags, *args, **kwargs):
        nonlocal calls
        if path == "nested" and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == 2:  # inventory succeeded; replace before recursive delete
                nested.rename(first / "preserved")
                nested.symlink_to(second, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(safety.os, "open", racing_open)
    with pytest.raises((OSError, drain.LegacyDrainError)):
        safety.delete_bound(model["legacy"], first.name, expected, inventory)
    assert (second / "research.db").exists()


def test_v2_backup_success_survives_recovery_error(model):
    import backup_v2 as backup
    _, second, journal, record = pending(model)
    record["legacy_candidate"] = "/outside"
    journal.write_text(json.dumps(record))
    config = replace(model["backup_config"], legacy_drain_enabled=True, legacy_root=model["legacy"])
    result = backup.create_backup(config, rebalance_after=False)
    assert result["legacy_drain"]["state"] == "RECOVERY_BLOCKED"
    assert _validator(Path(result["path"]))["verified"] and second.exists()
