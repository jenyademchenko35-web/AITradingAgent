"""CLI runner for Strategy Lab v1."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from strategy_lab.engine import StrategyLabEngine  # noqa: E402
from strategy_lab.comparison import comparison_report  # noqa: E402
from strategy_lab.reports import format_summary, save_lab_reports  # noqa: E402
from strategy_lab.strategy_registry import strategy_by_key  # noqa: E402


def main() -> None:
    """Run Strategy Lab and save artifacts."""
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    strategies = strategy_by_key(key) if key else None
    engine = StrategyLabEngine(strategies=strategies)
    report = engine.run()
    save_lab_reports(report)
    print(format_summary({**report, "comparison": comparison_report(report.get("metrics", []))}))


if __name__ == "__main__":
    main()
