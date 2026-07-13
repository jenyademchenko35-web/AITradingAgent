"""CLI entry point for Research Orchestrator v1."""

from __future__ import annotations

import argparse

from research_orchestrator.formatter import format_summary
from research_orchestrator.orchestrator import ResearchOrchestrator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Research Orchestrator")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Запустить только allowlist безопасных read-only генераторов.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    orchestrator = ResearchOrchestrator()
    if args.refresh:
        refresh_results = orchestrator.refresh_safe_reports()
        for row in refresh_results:
            print(f"{row['module']}: {row['status']}")
    report = orchestrator.build_report(save=True)
    print(format_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
