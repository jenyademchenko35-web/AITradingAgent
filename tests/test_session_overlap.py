from __future__ import annotations

import json
from pathlib import Path

from research_lab_v2.session_overlap import H10_VERSION, attach_h10_evidence, classify_session, ensure_h10_boundary


def test_existing_session_is_consumed_without_recalculation() -> None:
    assert classify_session({"session": "OVERLAP"})["classification"] == "OVERLAP"
    assert classify_session({"session": "LONDON"})["classification"] == "NON_OVERLAP"
    assert classify_session({})["classification"] == "UNKNOWN_SESSION"


def test_boundary_is_create_once_and_restart_safe(tmp_path: Path) -> None:
    path = tmp_path / "boundary.json"
    first = ensure_h10_boundary("2026-08-27T10:00:00+00:00", path=path)
    second = ensure_h10_boundary("2026-08-28T10:00:00+00:00", path=path)
    assert first == second == {"h10_version": H10_VERSION, "h10_started_at": "2026-08-27T10:00:00+00:00"}
    assert json.loads(path.read_text())["h10_started_at"] == "2026-08-27T10:00:00+00:00"


def test_attached_evidence_is_forward_only_and_does_not_mutate_snapshot(tmp_path: Path) -> None:
    snapshot = {"timestamp": "2026-08-27T10:00:00+00:00", "session": "OVERLAP"}
    evidence = attach_h10_evidence(snapshot, boundary_path=tmp_path / "boundary.json")
    assert snapshot == {"timestamp": "2026-08-27T10:00:00+00:00", "session": "OVERLAP"}
    assert evidence["h10_version"] == H10_VERSION
    assert evidence["h10_observation_scope"] == "FORWARD_H10"
    assert evidence["session_overlap"]["classification"] == "OVERLAP"


def test_invalid_observation_fails_open_without_boundary(tmp_path: Path) -> None:
    result = attach_h10_evidence({"session": "OVERLAP"}, boundary_path=tmp_path / "boundary.json")
    assert result["h10_observation_scope"] == "H10_BOUNDARY_UNAVAILABLE"
    assert not (tmp_path / "boundary.json").exists()


def test_missing_session_is_never_forward_h10(tmp_path: Path) -> None:
    result = attach_h10_evidence(
        {"timestamp": "2026-08-27T10:00:00+00:00"}, boundary_path=tmp_path / "boundary.json"
    )
    assert result["session_overlap"]["classification"] == "UNKNOWN_SESSION"
    assert result["h10_observation_scope"] == "INSUFFICIENT_EVIDENCE"
