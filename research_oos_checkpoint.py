"""Unified, read-only checkpoint for frozen Crypto and FX OOS evidence.

This module is deliberately an observability wrapper.  It delegates all
population, integrity, cutoff, hypothesis, and performance decisions to the
existing frozen validators; it neither starts collection nor changes state.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fx_research.frozen_oos_validation import build_report as build_fx_report
from research_lab_v2.oos_validation import build_oos_report as build_crypto_report

CryptoBuilder = Callable[..., dict[str, Any]]
FXBuilder = Callable[..., dict[str, Any]]


def _crypto_unavailable(error: Exception) -> dict[str, Any]:
    return {
        "cutoff": None,
        "cutoff_id": None,
        "eligible_oos_outcomes": None,
        "overall": "UNAVAILABLE",
        "hypotheses": [],
        "integrity": {"valid": False, "issues": [f"{type(error).__name__}: {error}"[:300]]},
    }


def _fx_unavailable(error: Exception) -> dict[str, Any]:
    return {
        "cutoff": None,
        "overall": "UNAVAILABLE",
        "hypotheses": [],
        "integrity": {"valid": False, "issues": [f"{type(error).__name__}: {error}"[:300]]},
    }


def _crypto_view(report: Mapping[str, Any]) -> dict[str, Any]:
    population = report.get("analysis_population")
    integrity = report.get("oos_integrity")
    hypotheses = report.get("hypotheses")
    return {
        "cutoff": report.get("cutoff"),
        "cutoff_id": report.get("cutoff_id"),
        "eligible_oos_outcomes": (
            population.get("eligible_closed_resolved_complete_finite")
            if isinstance(population, Mapping) else None
        ),
        "overall": report.get("oos_verdict", "UNAVAILABLE"),
        "hypotheses": [
            {"id": item.get("id"), "status": item.get("status")}
            for item in hypotheses if isinstance(item, Mapping)
        ] if isinstance(hypotheses, list) else [],
        "integrity": {
            "valid": isinstance(integrity, Mapping) and not any(
                value for key, value in integrity.items()
                if key in {"duplicate_shadow_trade_id", "invalid_feature_evidence", "nonfinite_pnl_r"}
            ),
            "details": dict(integrity) if isinstance(integrity, Mapping) else None,
        },
    }


def _fx_view(report: Mapping[str, Any]) -> dict[str, Any]:
    hypotheses = report.get("hypotheses")
    return {
        "cutoff": report.get("cutoff"),
        "overall": report.get("overall", "UNAVAILABLE"),
        "hypotheses": [
            {
                "id": item.get("hypothesis_id"),
                "resolved": item.get("metrics", {}).get("resolved")
                if isinstance(item.get("metrics"), Mapping) else None,
                "profit_factor": item.get("metrics", {}).get("profit_factor")
                if isinstance(item.get("metrics"), Mapping) else None,
                "net_r": item.get("metrics", {}).get("net_r")
                if isinstance(item.get("metrics"), Mapping) else None,
                "expectancy_r": item.get("metrics", {}).get("expectancy_r")
                if isinstance(item.get("metrics"), Mapping) else None,
                "verdict": item.get("verdict"),
            }
            for item in hypotheses if isinstance(item, Mapping)
        ] if isinstance(hypotheses, list) else [],
        "integrity": dict(report.get("integrity", {}))
        if isinstance(report.get("integrity"), Mapping) else {"valid": False, "issues": ["missing integrity report"]},
    }


def _sample_state(track: Mapping[str, Any]) -> str:
    if track.get("overall") in {"UNAVAILABLE", "DATA_INVALID"}:
        return "UNAVAILABLE"
    hypotheses = track.get("hypotheses")
    verdicts = [str(item.get("verdict", item.get("status", ""))) for item in hypotheses if isinstance(item, Mapping)] if isinstance(hypotheses, list) else []
    return "INSUFFICIENT" if not verdicts or any(value.startswith("WAITING") for value in verdicts) else "EVIDENCE_AVAILABLE"


def _checkpoint(crypto: Mapping[str, Any], fx: Mapping[str, Any]) -> dict[str, str]:
    crypto_sample, fx_sample = _sample_state(crypto), _sample_state(fx)
    integrity_ok = (
        crypto_sample != "UNAVAILABLE" and fx_sample != "UNAVAILABLE"
        and bool(crypto.get("integrity", {}).get("valid"))
        and bool(fx.get("integrity", {}).get("valid"))
    )
    if not integrity_ok:
        action = "REVIEW_DATA_INTEGRITY"
    elif "INSUFFICIENT" in {crypto_sample, fx_sample}:
        action = "COLLECT_MORE_OOS_DATA"
    else:
        action = "OOS_EVIDENCE_READY_FOR_REVIEW"
    return {
        "crypto_sample": crypto_sample,
        "fx_sample": fx_sample,
        "integrity": "PASS" if integrity_ok else "DEGRADED",
        "action": action,
    }


def build_checkpoint(
    *,
    crypto_db: str | Path,
    crypto_history: str | Path,
    eurusd_path: str | Path,
    gbpusd_path: str | Path,
    crypto_builder: CryptoBuilder = build_crypto_report,
    fx_builder: FXBuilder = build_fx_report,
) -> dict[str, Any]:
    """Return normalized existing-validator reports without writing any inputs."""
    try:
        crypto = _crypto_view(crypto_builder(
            database_path=crypto_db, history_path=crypto_history,
        ))
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        crypto = _crypto_unavailable(exc)
    try:
        fx = _fx_view(fx_builder(eurusd_path=eurusd_path, gbpusd_path=gbpusd_path))
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        fx = _fx_unavailable(exc)
    return {"crypto": crypto, "fx": fx, "checkpoint": _checkpoint(crypto, fx)}


def _format_number(value: Any) -> str:
    return "—" if value is None else f"{float(value):.3f}"


def print_checkpoint(report: Mapping[str, Any]) -> None:
    """Print only normalized high-level evidence, never raw outcomes/features."""
    crypto, fx, checkpoint = report["crypto"], report["fx"], report["checkpoint"]
    print("OOS RESEARCH CHECKPOINT")
    print("\nCRYPTO")
    print(f"Cutoff: {crypto['cutoff']}")
    print(f"Cutoff ID: {crypto['cutoff_id']}")
    print(f"Eligible: {crypto['eligible_oos_outcomes'] if crypto['eligible_oos_outcomes'] is not None else 'unavailable'}")
    print(f"Overall: {crypto['overall']}")
    for item in crypto["hypotheses"]:
        print(f"{item['id']} {item['status']}")
    print("\nFX")
    print(f"Cutoff: {fx['cutoff']}")
    print(f"Overall: {fx['overall']}")
    for item in fx["hypotheses"]:
        print(f"\n{item['id']}")
        print(f"Resolved: {item['resolved'] if item['resolved'] is not None else 'unavailable'}")
        print(f"PF: {_format_number(item['profit_factor'])}")
        print(f"Net R: {_format_number(item['net_r'])}")
        print(f"Exp R: {_format_number(item['expectancy_r'])}")
        print(f"Verdict: {item['verdict']}")
    print("\nCHECKPOINT")
    print(f"Crypto sample: {checkpoint['crypto_sample']}")
    print(f"FX sample: {checkpoint['fx_sample']}")
    print(f"Integrity: {checkpoint['integrity']}")
    print(f"Action: {checkpoint['action']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crypto-db", type=Path, default=Path("research.db"))
    parser.add_argument("--crypto-history", type=Path, default=Path("research_lab_shadow_history.csv"))
    parser.add_argument("--eurusd", type=Path, required=True)
    parser.add_argument("--gbpusd", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    report = build_checkpoint(
        crypto_db=args.crypto_db, crypto_history=args.crypto_history,
        eurusd_path=args.eurusd,
        gbpusd_path=args.gbpusd,
    )
    print_checkpoint(report)
    if args.json_output:
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
