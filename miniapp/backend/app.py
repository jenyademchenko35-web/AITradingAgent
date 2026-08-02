"""FastAPI application exposing authenticated GET-only Mini App APIs."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from miniapp.shared.models import DashboardResponse, ListResponse, SignalResponse, StatusResponse, TelegramUser, WatchlistItem

from .auth import auth_dependency
from .config import MiniAppSettings
from .repository import ReadOnlyRepository


def create_app(
    *,
    settings: MiniAppSettings | None = None,
    repository: ReadOnlyRepository | None = None,
) -> FastAPI:
    config = settings or MiniAppSettings.from_env()
    data = repository or ReadOnlyRepository(config.data_dir)
    authenticate = auth_dependency(config)
    api = FastAPI(title="TradeWatcher Mini App API", version="1.0.0", docs_url=None, redoc_url=None)

    @api.get("/healthz")
    async def health() -> dict[str, object]:
        return {"status": "ok", "miniapp_enabled": config.enabled}

    @api.get("/api/status", response_model=StatusResponse)
    async def status(user: TelegramUser = Depends(authenticate)) -> StatusResponse:
        return StatusResponse(
            enabled=config.enabled, owner_only=config.owner_only,
            user_id=user.id, updated_at=data.updated_at(),
        )

    @api.get("/api/dashboard", response_model=DashboardResponse)
    async def dashboard(_: TelegramUser = Depends(authenticate)) -> dict:
        return data.dashboard()

    @api.get("/api/watchlist", response_model=tuple[WatchlistItem, ...])
    async def watchlist(_: TelegramUser = Depends(authenticate)) -> tuple[dict, ...]:
        return tuple(data.watchlist())

    @api.get("/api/signal/{symbol}", response_model=SignalResponse)
    async def signal(symbol: str, _: TelegramUser = Depends(authenticate)) -> dict:
        result = data.signal(symbol)
        if result is None:
            raise HTTPException(status_code=404, detail="Signal snapshot not found")
        return result

    @api.get("/api/signal/{symbol}/{timeframe}", response_model=SignalResponse)
    async def signal_timeframe(symbol: str, timeframe: str, _: TelegramUser = Depends(authenticate)) -> dict:
        result = data.signal(symbol, timeframe)
        if result is None:
            raise HTTPException(status_code=404, detail="Signal snapshot not found")
        return result

    @api.get("/api/trades/open", response_model=ListResponse)
    async def open_trades(_: TelegramUser = Depends(authenticate)) -> ListResponse:
        rows = tuple(data.open_trades())
        return ListResponse(items=rows, count=len(rows))

    @api.get("/api/trades/history", response_model=ListResponse)
    async def trade_history(_: TelegramUser = Depends(authenticate)) -> ListResponse:
        rows = tuple(data.trade_history())
        return ListResponse(items=rows, count=len(rows))

    @api.get("/api/stats")
    async def stats(_: TelegramUser = Depends(authenticate)) -> dict:
        return data.stats()

    @api.get("/api/research")
    async def research(_: TelegramUser = Depends(authenticate)) -> dict:
        return data.research()

    @api.get("/api/research/rank")
    async def research_rank(_: TelegramUser = Depends(authenticate)) -> dict:
        report = data.research()
        return {"items": report.get("top_strategies", [])}

    @api.get("/api/research/trades")
    async def research_trades(_: TelegramUser = Depends(authenticate)) -> dict:
        report = data.research()
        return report.get("shadow_ledger", {"open": [], "closed": []})

    frontend_dist = config.data_dir / "miniapp" / "frontend" / "dist"
    if frontend_dist.is_dir():
        api.mount("/", StaticFiles(directory=frontend_dist, html=True), name="miniapp")

    return api


app = create_app()
