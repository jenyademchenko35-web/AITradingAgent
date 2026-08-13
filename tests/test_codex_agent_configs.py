"""Static contracts for project-scoped Codex workflow roles."""

from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / ".codex" / "agents"


def _agent(name: str) -> dict[str, object]:
    return tomllib.loads((AGENTS / f"{name}.toml").read_text(encoding="utf-8"))


def test_multi_agent_workflow_roles_use_supported_project_config_fields():
    expected = {
        "aitrading-builder": "workspace-write",
        "aitrading-reviewer": "read-only",
        "aitrading-production-auditor": "read-only",
    }
    for name, sandbox_mode in expected.items():
        payload = _agent(name)
        assert payload["name"] == name
        assert isinstance(payload["description"], str) and payload["description"]
        assert payload["sandbox_mode"] == sandbox_mode
        assert payload["model_reasoning_effort"] == "high"
        assert isinstance(payload["developer_instructions"], str)
        assert "Data integrity before strategy optimization" in payload["developer_instructions"]


def test_roles_preserve_release_and_trading_safety_boundaries():
    builder = _agent("aitrading-builder")["developer_instructions"]
    reviewer = _agent("aitrading-reviewer")["developer_instructions"]
    auditor = _agent("aitrading-production-auditor")["developer_instructions"]
    for instructions in (builder, reviewer, auditor):
        assert "DecisionEngine" in instructions
        assert "historical backfill" in instructions
        assert "commit, push, deploy" in instructions
    assert "не исправляешь код автоматически" in reviewer
    assert "read-only" in auditor
    assert "Server Operator" in auditor


def test_workflow_doc_requires_serial_builder_reviewer_and_user_release_gate():
    content = (ROOT / "docs" / "MULTI_AGENT_WORKFLOW_V1.md").read_text(encoding="utf-8")
    for text in (
        "Builder writes → Reviewer verifies → Production Auditor verifies deployment",
        "must never edit the same working tree concurrently",
        "User explicitly authorizes commit and push",
        "Data integrity before strategy optimization",
        "Historical unresolved outcomes (baseline: 22)",
    ):
        assert text in content
