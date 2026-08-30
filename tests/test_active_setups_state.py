from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

import active_setups_state as state_io


def _state(setup_id: str = "BTC_USDT_LONG") -> dict[str, str]:
    return {setup_id: datetime.now(timezone.utc).isoformat()}


def _logger(messages: list[str]):
    return messages.append


def test_normal_save_and_load(tmp_path: Path) -> None:
    path = tmp_path / "active_setups_v3.json"
    expected = _state()
    state_io.save_active_setups_state(path, expected, log=lambda _: None)
    assert state_io.load_active_setups_state(path, log=lambda _: None) == expected


def test_save_uses_atomic_replacement_in_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "active_setups_v3.json"
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(state_io.os, "replace", recording_replace)
    state_io.save_active_setups_state(path, _state(), log=lambda _: None)

    assert replacements
    source, destination = replacements[-1]
    assert source.parent == destination.parent == tmp_path
    assert destination == path


def test_failed_primary_replace_keeps_last_valid_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "active_setups_v3.json"
    original = _state("BTC_USDT_LONG")
    state_io.save_active_setups_state(path, original, log=lambda _: None)
    real_replace = os.replace

    def fail_primary(source, destination):
        if Path(destination) == path:
            raise OSError("simulated interrupted replace")
        return real_replace(source, destination)

    monkeypatch.setattr(state_io.os, "replace", fail_primary)
    with pytest.raises(state_io.ActiveSetupsStateError):
        state_io.save_active_setups_state(
            path, _state("ETH_USDT_SHORT"), log=lambda _: None
        )

    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_corrupted_primary_without_backup_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "active_setups_v3.json"
    path.write_text('{"partial":', encoding="utf-8")
    messages: list[str] = []

    with pytest.raises(state_io.ActiveSetupsStateError, match="refusing fail-open"):
        state_io.load_active_setups_state(path, log=_logger(messages))
    assert any("corrupt" in message for message in messages)


def test_valid_backup_recovery_is_deterministic_and_observable(tmp_path: Path) -> None:
    path = tmp_path / "active_setups_v3.json"
    backup = tmp_path / "active_setups_v3.json.bak"
    expected = _state()
    path.write_text("not-json", encoding="utf-8")
    backup.write_text(json.dumps(expected), encoding="utf-8")
    messages: list[str] = []

    assert state_io.load_active_setups_state(path, log=_logger(messages)) == expected
    assert any("recovered from last-known-good backup" in m for m in messages)


@pytest.mark.parametrize("backup_contents", [None, "also-not-json"])
def test_corrupt_primary_and_missing_or_corrupt_backup_fails_closed(
    tmp_path: Path, backup_contents: str | None
) -> None:
    path = tmp_path / "active_setups_v3.json"
    path.write_text("not-json", encoding="utf-8")
    if backup_contents is not None:
        (tmp_path / "active_setups_v3.json.bak").write_text(
            backup_contents, encoding="utf-8"
        )

    with pytest.raises(state_io.ActiveSetupsStateError):
        state_io.load_active_setups_state(path, log=lambda _: None)


def test_missing_primary_and_backup_is_legitimate_initial_state(tmp_path: Path) -> None:
    assert state_io.load_active_setups_state(
        tmp_path / "active_setups_v3.json", log=lambda _: None
    ) == {}


def test_primary_io_error_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "active_setups_v3.json"
    path.write_text(json.dumps(_state()), encoding="utf-8")
    real_open = Path.open

    def denied_open(self, *args, **kwargs):
        if self == path:
            raise PermissionError("simulated denial")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    messages: list[str] = []
    with pytest.raises(state_io.ActiveSetupsStateError):
        state_io.load_active_setups_state(path, log=_logger(messages))
    assert any("read failed" in message for message in messages)


def test_backup_recovery_preserves_duplicate_suppression_input(tmp_path: Path) -> None:
    path = tmp_path / "active_setups_v3.json"
    expected = _state("BTC_USDT_LONG")
    path.write_text("truncated", encoding="utf-8")
    (tmp_path / "active_setups_v3.json.bak").write_text(
        json.dumps(expected), encoding="utf-8"
    )

    recovered = state_io.load_active_setups_state(path, log=lambda _: None)
    assert recovered.get("BTC_USDT_LONG") == expected["BTC_USDT_LONG"]


def test_live_duplicate_suppression_remains_active_after_backup_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import multi_timeframe_agent_v3 as agent

    path = tmp_path / "active_setups_v3.json"
    expected = _state("BTC_USDT_LONG")
    path.write_text("truncated", encoding="utf-8")
    (tmp_path / "active_setups_v3.json.bak").write_text(
        json.dumps(expected), encoding="utf-8"
    )
    monkeypatch.setattr(agent, "ACTIVE_SETUPS_FILE", str(path))

    assert agent.is_setup_active("BTC_USDT_LONG") is True


def test_live_duplicate_suppression_fails_closed_when_state_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import multi_timeframe_agent_v3 as agent

    path = tmp_path / "active_setups_v3.json"
    path.write_text("truncated", encoding="utf-8")
    monkeypatch.setattr(agent, "ACTIVE_SETUPS_FILE", str(path))

    with pytest.raises(state_io.ActiveSetupsStateError):
        agent.is_setup_active("BTC_USDT_LONG")


def test_valid_state_remains_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "active_setups_v3.json"
    expected = _state("ETH_USDT_SHORT")
    path.write_text(json.dumps(expected), encoding="utf-8")
    assert state_io.load_active_setups_state(path, log=lambda _: None) == expected
