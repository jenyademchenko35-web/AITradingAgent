"""CLI entrypoint for read-only Live Market Monitor."""

from __future__ import annotations

import argparse

from live_monitor.monitor import LiveMarketMonitor


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="AITradingAgent Live Market Monitor")
    parser.add_argument("--interval", type=int, default=3, help="Update interval in seconds")
    parser.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "websocket", "rest", "local"],
        help="Price provider mode",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one update cycle and exit",
    )
    return parser.parse_args()


def main() -> None:
    """Run monitor CLI."""
    args = parse_args()
    monitor = LiveMarketMonitor(interval=args.interval, provider=args.provider)
    if args.once:
        state = monitor.run_once()
        print(
            "Live Monitor updated: "
            f"status={state.get('status')} "
            f"tracked={state.get('tracked_count')} "
            f"priced={state.get('priced_count')}"
        )
        return
    monitor.run_forever()


if __name__ == "__main__":
    main()
