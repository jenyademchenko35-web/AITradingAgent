"""Read-only FastAPI backend for the TradeWatcher Mini App."""

from .app import app, create_app

__all__ = ["app", "create_app"]
