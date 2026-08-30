from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import subprocess
import sys

import market_news_observer
from research_lab_v2 import runtime


def _close_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def _rotating_handler(logger: logging.Logger) -> RotatingFileHandler:
    handlers = [handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)]
    assert len(handlers) == 1
    return handlers[0]


def _exercise_rotation(logger: logging.Logger, path: Path, backup_count: int) -> None:
    message = "UTF-8: рынок ликвидность " + ("x" * 96)
    for _ in range(80):
        logger.info(message)
    for handler in logger.handlers:
        handler.flush()

    assert path.exists()
    assert path.with_name(f"{path.name}.1").exists()
    assert "рынок" in "".join(
        candidate.read_text(encoding="utf-8")
        for candidate in [path, path.with_name(f"{path.name}.1")]
    )
    assert not path.with_name(f"{path.name}.{backup_count + 1}").exists()

    logger.info("continues-after-rotation")
    for handler in logger.handlers:
        handler.flush()
    assert "continues-after-rotation" in path.read_text(encoding="utf-8")


def test_market_news_handler_contract_and_rotation(tmp_path, monkeypatch):
    path = tmp_path / "market_news_observer.log"
    logger = logging.getLogger("market_news_observer")
    _close_handlers(logger)
    monkeypatch.setattr(market_news_observer, "LOG_FILE", path)
    monkeypatch.setattr(market_news_observer, "LOG_MAX_BYTES", 512)
    try:
        market_news_observer._configure_logging()
        handler = _rotating_handler(logger)
        assert Path(handler.baseFilename) == path
        assert handler.maxBytes == 512
        assert handler.backupCount == 6
        assert handler.encoding.lower().replace("-", "") == "utf8"
        assert handler.formatter._fmt == "%(asctime)s %(levelname)s %(message)s"
        assert logger.level == logging.INFO
        assert logger.propagate is True
        _exercise_rotation(logger, path, backup_count=6)
    finally:
        _close_handlers(logger)


def test_market_news_production_limits():
    assert market_news_observer.LOG_MAX_BYTES == 10 * 1024 * 1024
    assert market_news_observer.LOG_BACKUP_COUNT == 6


def test_research_handler_contract_and_rotation(tmp_path, monkeypatch):
    path = tmp_path / "research_lab_v2.log"
    monkeypatch.setattr(runtime, "LOG_MAX_BYTES", 512)
    logger = runtime._logger(path)
    try:
        handler = _rotating_handler(logger)
        assert Path(handler.baseFilename) == path
        assert handler.maxBytes == 512
        assert handler.backupCount == 8
        assert handler.encoding.lower().replace("-", "") == "utf8"
        assert handler.formatter._fmt == "%(message)s"
        assert logger.level == logging.INFO
        assert logger.propagate is False
        _exercise_rotation(logger, path, backup_count=8)
    finally:
        _close_handlers(logger)


def test_research_production_limits():
    assert runtime.LOG_MAX_BYTES == 25 * 1024 * 1024
    assert runtime.LOG_BACKUP_COUNT == 8


def test_research_rotation_failure_remains_fail_open(tmp_path, monkeypatch):
    path = tmp_path / "research_lab_v2.log"
    monkeypatch.setattr(runtime, "LOG_MAX_BYTES", 1)
    logger = runtime._logger(path)
    handler = _rotating_handler(logger)
    monkeypatch.setattr(handler, "doRollover", lambda: (_ for _ in ()).throw(OSError("disk error")))
    monkeypatch.setattr(logging, "raiseExceptions", False)
    try:
        runtime._write_log("info", {"event": "rotation-failure"}, path=path)
    finally:
        _close_handlers(logger)


def test_imports_do_not_create_or_configure_production_logs(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    script = (
        "import logging, market_news_observer, research_lab_v2.runtime; "
        "assert not logging.getLogger('market_news_observer').handlers; "
        "assert not list(logging.getLogger().handlers)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={"PYTHONPATH": str(repository)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "market_news_observer.log").exists()
    assert not (tmp_path / "logs" / "research_lab_v2.log").exists()
