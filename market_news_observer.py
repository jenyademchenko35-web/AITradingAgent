"""Market News Observer v2 CLI wrapper."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from news_observer import MarketNewsObserver, format_summary_text


BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "market_news_observer.log"


def _configure_logging() -> None:
    """Configure secret-free file and console diagnostics once."""
    logger = logging.getLogger("market_news_observer")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Market News Observer v2")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    mode_group.add_argument("--loop", action="store_true", help="Run observer repeatedly.")
    parser.add_argument("--offline", action="store_true", help="Do not fetch network feeds.")
    parser.add_argument("--interval", type=int, default=1800, help="Loop interval in seconds; default is 1800.")
    parser.add_argument("--status", action="store_true", help="Print current cache status without fetching.")
    parser.add_argument("--sources", action="store_true", help="Print registered sources.")
    parser.add_argument("--validate-cache", action="store_true", help="Validate cached report artifacts.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging()
    observer = MarketNewsObserver(BASE_DIR, offline=args.offline)

    if args.sources:
        _print_json({"sources": observer.list_sources()})
        return 0
    if args.status:
        _print_json(observer.status())
        return 0
    if args.validate_cache:
        result = observer.validate_cache()
        _print_json(result)
        return 0 if result.get("status") in {"OK", "PARTIAL", "STALE"} else 1

    run_loop = bool(args.loop)
    while True:
        report = observer.build_report()
        print(format_summary_text(report))
        if not run_loop:
            return 1 if report.get("status") == "FAILED" else 0
        time.sleep(max(60, int(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
