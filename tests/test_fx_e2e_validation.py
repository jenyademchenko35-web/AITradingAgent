"""End-to-end validation uses fixtures and leaves no persistent FX or crypto state."""

from __future__ import annotations

from fx_research.e2e_validation import run_validation


def test_fx_historical_e2e_validation_is_deterministic_and_isolated() -> None:
    report = run_validation()
    assert report["overall"] is True
    assert report["EUR/USD"] == "PASS"
    assert report["GBP/USD"] == "PASS"
    assert report["ambiguous_intrabar"] == "PASS"
    assert report["restart_recovery"] == "PASS"
    assert report["idempotency"] == "PASS"
    assert report["crypto_isolation"] == "PASS"
    assert report["checkpoint_integrity"] == "PASS"
    assert report["checkpoint"]["integrity"] == {
        "closed": 4, "eligible": 3, "unresolved": 1, "incomplete": 1,
    }
