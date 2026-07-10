"""Command-line runner for Trade Replay Lab."""

from __future__ import annotations

import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_replay_lab.replay_engine import TradeReplayEngine
from trade_replay_lab.replay_report import format_summary, save_report


def main() -> None:
    """Build, save and print the read-only replay report."""
    report = TradeReplayEngine().build_report()
    save_report(report)
    print(format_summary(report))


if __name__ == "__main__":
    main()
