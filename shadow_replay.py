"""Command-line entry point for Shadow Replay Engine v2."""

from shadow_replay.engine import ShadowReplayEngine
from shadow_replay.report import format_summary


def main() -> None:
    """Build reports and print the compact summary."""
    report = ShadowReplayEngine().run()
    print(format_summary(report))


if __name__ == "__main__":
    main()
