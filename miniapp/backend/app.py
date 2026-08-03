"""FastAPI application exposing authenticated GET-only Mini App APIs."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from miniapp.shared.models import (
    DashboardResponse,
    ListResponse,
    PaginatedHistoryResponse,
    SignalChangesResponse,
    SignalIntelligencePayload,
    SignalRequirementsResponse,
    SignalResponse,
    SimilarSetupsResponse,
    StatusResponse,
    TelegramUser,
    WatchlistItem,
)

from .auth import auth_dependency
from .config import MiniAppSettings
from .repository import ReadOnlyRepository


def create_app(
    *,
    settings: MiniAppSettings | None = None,
    repository: ReadOnlyRepository | None = None,
) -> FastAPI:
    config = settings or MiniAppSettings.from_env()
    data = repository or ReadOnlyRepository(
        config.data_dir,
        cache_ttl_seconds=config.cache_ttl_seconds,
        query_timeout_seconds=config.query_timeout_seconds,
        max_source_rows=config.max_source_rows,
        similar_min_sample=config.similar_min_sample,
    )
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

    @api.get(
        "/api/signal/{symbol}/{timeframe}/intelligence",
        response_model=SignalIntelligencePayload,
    )
    async def signal_intelligence(
        symbol: str, timeframe: str, _: TelegramUser = Depends(authenticate),
    ) -> SignalIntelligencePayload:
        result = data.signal_intelligence(symbol, timeframe)
        if result is None:
            raise HTTPException(status_code=404, detail="Signal snapshot not found")
        return result

    @api.get(
        "/api/signal/{symbol}/{timeframe}/history",
        response_model=PaginatedHistoryResponse,
    )
    async def signal_history(
        symbol: str, timeframe: str,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
        _: TelegramUser = Depends(authenticate),
    ) -> PaginatedHistoryResponse:
        return data.signal_history(symbol, timeframe, page=page, page_size=page_size)

    @api.get(
        "/api/signal/{symbol}/{timeframe}/changes",
        response_model=SignalChangesResponse,
    )
    async def signal_changes(
        symbol: str, timeframe: str, _: TelegramUser = Depends(authenticate),
    ) -> SignalChangesResponse:
        return data.signal_changes(symbol, timeframe)

    @api.get(
        "/api/signal/{symbol}/{timeframe}/requirements",
        response_model=SignalRequirementsResponse,
    )
    async def signal_requirements(
        symbol: str, timeframe: str, _: TelegramUser = Depends(authenticate),
    ) -> SignalRequirementsResponse:
        return data.signal_requirements(symbol, timeframe)

    @api.get(
        "/api/signal/{symbol}/{timeframe}/similar",
        response_model=SimilarSetupsResponse,
    )
    async def similar_setups(
        symbol: str, timeframe: str,
        source: str = Query(default="LIVE", pattern="^(LIVE|LEGACY_SHADOW|RESEARCH_LAB)$"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=200),
        _: TelegramUser = Depends(authenticate),
    ) -> SimilarSetupsResponse:
        return data.similar_setups(
            symbol, timeframe, source=source, page=page, page_size=page_size,
        )

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
