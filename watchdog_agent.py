#!/usr/bin/env python3
"""Process supervisor for the long-running AITradingAgent services."""

from __future__ import annotations

import argparse
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Iterable

import psutil
import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
LOCK_FILE = PROJECT_ROOT / ".watchdog.lock"
NOTIFICATION_COOLDOWN = 15 * 60
RESTART_WINDOW = 30 * 60
CRITICAL_RESTART_COUNT = 3
CRITICAL_RETRY_COOLDOWN = 5 * 60
SYSTEMD_MANAGED_PROCESSES = frozenset({"multi_timeframe_agent_v3.py"})


@dataclass(frozen=True)
class ProcessSpec:
    filename: str
    arguments: tuple[str, ...]
    log_path: Path

    def command(self) -> list[str]:
        return [str(PROJECT_ROOT / "venv/bin/python"), "-u", self.filename, *self.arguments]


PROCESS_SPECS = (
    ProcessSpec("telegram_bot_v4.py", (), Path("logs/telegram_bot_v4.log")),
    ProcessSpec(
        "market_news_observer.py",
        ("--loop", "--interval", "1800"),
        Path("logs/news.log"),
    ),
    ProcessSpec(
        "live_market_monitor.py",
        ("--interval", "3"),
        Path("logs/live_monitor.log"),
    ),
)


def configure_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("watchdog")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_DIR / "watchdog.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


class WatchdogLock:
    def __init__(self, path: Path = LOCK_FILE) -> None:
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        self._file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            self._file = None
            return False
        self._file.seek(0)
        self._file.truncate()
        self._file.write(str(os.getpid()))
        self._file.flush()
        return True

    def release(self) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def __enter__(self) -> "WatchdogLock":
        if not self.acquire():
            raise RuntimeError("Watchdog is already running")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class Watchdog:
    def __init__(
        self,
        specs: Iterable[ProcessSpec] = PROCESS_SPECS,
        *,
        logger: logging.Logger | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        load_dotenv(PROJECT_ROOT / ".env")
        self.specs = tuple(spec for spec in specs if spec.filename not in SYSTEMD_MANAGED_PROCESSES)
        self.logger = logger or configure_logging()
        self.now = now
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.last_notifications: dict[tuple[str, str], float] = {}
        self.restart_history: dict[str, deque[float]] = defaultdict(deque)
        self.last_restart_attempt: dict[str, float] = {}

    @staticmethod
    def find_processes(filename: str) -> list[psutil.Process]:
        found: list[psutil.Process] = []
        for process in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = process.info.get("cmdline") or []
                if any(Path(arg).name == filename for arg in cmdline):
                    found.append(process)
            except (psutil.AccessDenied, psutil.NoSuchProcess, TypeError):
                continue
        return found

    def notify(self, process: str, event_type: str, message: str) -> bool:
        timestamp = self.now()
        key = (process, event_type)
        if timestamp - self.last_notifications.get(key, float("-inf")) < NOTIFICATION_COOLDOWN:
            self.logger.info("Telegram notification suppressed: %s %s", process, event_type)
            return False
        self.last_notifications[key] = timestamp
        if not self.bot_token or not self.chat_id:
            self.logger.warning("Telegram notification skipped (credentials missing): %s", message)
            return False
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                data={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
            response.raise_for_status()
            self.logger.info("Telegram notification sent: %s %s", process, event_type)
            return True
        except requests.RequestException:
            self.logger.exception("Telegram notification failed: %s %s", process, event_type)
            return False

    def recent_restart_count(self, filename: str) -> int:
        cutoff = self.now() - RESTART_WINDOW
        history = self.restart_history[filename]
        while history and history[0] < cutoff:
            history.popleft()
        return len(history)

    def can_attempt_restart(self, filename: str) -> bool:
        if self.recent_restart_count(filename) < CRITICAL_RESTART_COUNT:
            return True
        return self.now() - self.last_restart_attempt.get(filename, 0) >= CRITICAL_RETRY_COOLDOWN

    def start_process(self, spec: ProcessSpec) -> subprocess.Popen[bytes] | None:
        if spec.filename in SYSTEMD_MANAGED_PROCESSES:
            self.logger.error("Refusing to manage systemd-owned process: %s", spec.filename)
            return None
        if not self.can_attempt_restart(spec.filename):
            self.logger.warning("Restart rate limited for %s", spec.filename)
            return None
        timestamp = self.now()
        self.last_restart_attempt[spec.filename] = timestamp
        self.restart_history[spec.filename].append(timestamp)
        log_path = PROJECT_ROOT / spec.log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("ab") as log_file:
                process = subprocess.Popen(
                    spec.command(),
                    cwd=PROJECT_ROOT,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self.logger.info("Restarted %s with pid=%s command=%r", spec.filename, process.pid, spec.command())
            self.notify(
                spec.filename,
                "recovered",
                f"✅ WATCHDOG\nПроцесс восстановлен:\n{spec.filename}",
            )
            count = self.recent_restart_count(spec.filename)
            if count >= CRITICAL_RESTART_COUNT:
                self.notify(
                    spec.filename,
                    "critical",
                    "🔥 WATCHDOG CRITICAL\n"
                    f"Процесс несколько раз аварийно завершался:\n{spec.filename}\n"
                    f"Перезапусков за 30 минут: {count}\n"
                    f"Проверь лог:\n{spec.log_path}",
                )
            return process
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.exception("Failed to restart %s", spec.filename)
            self.notify(
                spec.filename,
                "restart_error",
                f"❌ WATCHDOG\nОшибка перезапуска:\n{spec.filename}\n{exc}",
            )
            return None

    def check_spec(self, spec: ProcessSpec) -> None:
        if spec.filename in SYSTEMD_MANAGED_PROCESSES:
            self.logger.error("Refusing to inspect or manage systemd-owned process: %s", spec.filename)
            return
        processes = self.find_processes(spec.filename)
        self.logger.info("State %s: %s", spec.filename, "running" if processes else "stopped")
        if not processes:
            self.notify(
                spec.filename,
                "stopped",
                "🚨 WATCHDOG\n"
                f"Процесс остановлен:\n{spec.filename}\n"
                "Выполняется автоматический перезапуск.",
            )
            self.start_process(spec)

    def check_once(self) -> None:
        self.logger.info("Watchdog check started")
        for spec in self.specs:
            self.check_spec(spec)

    def run(self, interval: int, once: bool = False) -> None:
        self.notify("watchdog_agent.py", "started", "✅ WATCHDOG\nWatchdog Agent запущен.")
        while True:
            self.check_once()
            if once:
                return
            time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor and restart AITradingAgent processes.")
    parser.add_argument("--interval", type=int, default=60, help="Check interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.interval <= 0:
        raise SystemExit("--interval must be greater than zero")
    lock = WatchdogLock()
    if not lock.acquire():
        print("Watchdog is already running")
        return 1
    try:
        Watchdog().run(args.interval, once=args.once)
    except KeyboardInterrupt:
        return 0
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
