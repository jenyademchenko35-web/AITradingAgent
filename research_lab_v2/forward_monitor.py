"""Stateless, read-only daily summary of the frozen forward-validation cohort.

Run with ``python -m research_lab_v2.forward_monitor DATABASE [--json]``.
Milestone is the highest reached eligible count, or None before 25. Status
describes accumulation/integrity only; it makes no claim about strategy quality.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from . import forward_validation as validator


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _count(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def summarize_report(report: Any) -> dict[str, Any]:
    """Validate the canonical report and summarize without recalculating features."""
    report = _mapping(report, "report")
    if report["cohort_version"] != validator.COHORT_VERSION:
        raise ValueError("unexpected cohort_version")
    cutoff = report["cutoff_utc"]
    if not isinstance(cutoff, str):
        raise ValueError("cutoff_utc must be a timezone-aware UTC timestamp")
    parsed = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    expected = datetime.fromisoformat(validator.FORWARD_ENTRY_CUTOFF_UTC)
    if parsed.utcoffset() != timezone.utc.utcoffset(None) or parsed != expected:
        raise ValueError("unexpected cutoff_utc")

    # Read frozen values from the validator; do not define thresholds here.
    expected_rules = {
        "primary_rule": {
            "atr_pct_lte": validator.PRIMARY_ATR_MAX,
            "volume_ratio_gt": validator.PRIMARY_VOLUME_MIN,
        },
        "secondary_rule": {"adx_lte": validator.SECONDARY_ADX_MAX},
    }
    rules = {}
    for name, expected_rule in expected_rules.items():
        rule = _mapping(report[name], name)
        if rule != expected_rule or any(type(value) not in (int, float) for value in rule.values()):
            raise ValueError(f"unexpected {name}")
        rules[name] = dict(rule)

    eligible = _count(report["total_eligible"], "total_eligible")
    excluded = _count(report["total_excluded"], "total_excluded")
    exclusions = _mapping(report["exclusions"], "exclusions")
    # Reuse the canonical reason set so missing counters cannot hide alerts.
    if not set(validator._EXCLUSION_ORDER).issubset(exclusions):
        raise ValueError("missing required exclusion counters")
    for reason, count in exclusions.items():
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("exclusion reasons must be non-empty strings")
        _count(count, f"exclusions.{reason}")
    if sum(exclusions.values()) != excluded:
        raise ValueError("total_excluded does not match exclusion counters")
    post_cutoff = sum(count for reason, count in exclusions.items() if reason != "pre_cutoff")

    primary = _mapping(report["primary"], "primary")
    match = _count(_mapping(primary["match"], "primary.match")["n"], "primary.match.n")
    complement = _count(_mapping(primary["complement"], "primary.complement")["n"], "primary.complement.n")
    secondary = _mapping(report["secondary_adx"], "secondary_adx")
    available = _count(secondary["available_n"], "secondary_adx.available_n")
    unavailable = _count(secondary["unavailable_n"], "secondary_adx.unavailable_n")
    if match + complement != eligible or available + unavailable != eligible:
        raise ValueError("primary/secondary counts do not partition total_eligible")

    milestone = next((n for n in (200, 100, 50, 25) if eligible >= n), None)
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
    return {
        "cohort_version": report["cohort_version"],
        "cutoff_utc": cutoff,
        **rules,
        "total_eligible": eligible,
        "total_excluded": excluded,
        "post_cutoff_exclusions": post_cutoff,
        "primary_match_n": match,
        "primary_complement_n": complement,
        "secondary_adx_available_n": available,
        "secondary_adx_unavailable_n": unavailable,
        "status": status,
        "milestone": milestone,
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def main(argv: Sequence[str] | None = None) -> int:
    """Return 0 for normal status, 2 for integrity alerts, 1 for failures."""
    parser = _Parser(description=__doc__)
    parser.add_argument("database", help="path to the existing research database (read-only)")
    parser.add_argument("--json", action="store_true", help="emit a deterministic JSON summary")
    try:
        args = parser.parse_args(argv)
        summary = summarize_report(validator.build_report(args.database))
        if args.json:
            print(json.dumps(summary, sort_keys=True, allow_nan=False))
        else:
            print(f"{summary['cohort_version']} | {summary['status']} | milestone={summary['milestone']}")
            print(f"cutoff_utc={summary['cutoff_utc']}")
            print(f"eligible={summary['total_eligible']} excluded={summary['total_excluded']} "
                  f"post_cutoff_exclusions={summary['post_cutoff_exclusions']}")
            print(f"primary: match={summary['primary_match_n']} complement={summary['primary_complement_n']}")
            print(f"secondary_adx: available={summary['secondary_adx_available_n']} "
                  f"unavailable={summary['secondary_adx_unavailable_n']}")
        return 2 if summary["status"] == "INTEGRITY_ALERT" else 0
    except Exception as error:
        print(f"forward monitor execution/configuration failure: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
