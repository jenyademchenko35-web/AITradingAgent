"""Fail-closed production entry point for the read-only Mini App API."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import uvicorn

from .app import create_app
from .config import MiniAppSettings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def main() -> int:
    configure_logging()
    settings = MiniAppSettings.from_env()
    errors = settings.startup_errors()
    if errors:
        logging.getLogger("miniapp.startup").error(
            "startup validation failed: %s", "; ".join(errors)
        )
        return 2
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
        reload=False,
        access_log=True,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1,::1",
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
