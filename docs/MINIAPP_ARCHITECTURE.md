# TradeWatcher Telegram Mini App Architecture

## Purpose and boundaries

The TradeWatcher Mini App is a separate read-only presentation service for
Telegram WebApp. It consumes persisted AITradingAgent artifacts and cannot call
DecisionEngine, LIVE_BASELINE, execution adapters, Portfolio Manager mutation,
Research Lab strategy runtime, or configuration writers.

Safe defaults:

```text
MINIAPP_ENABLED=false
MINIAPP_OWNER_ONLY=true
```

No API route opens or closes a trade, changes risk, edits a strategy, changes a
configuration file, or triggers a research calculation. All application routes
are GET. `/healthz` is an unauthenticated infrastructure health probe and does
not expose trading data.

## Project layout

```text
miniapp/
├── .env.example
├── backend/
│   ├── app.py             # FastAPI routes and optional static dist mount
│   ├── auth.py            # Telegram WebApp initData HMAC validation
│   ├── config.py          # immutable rollout settings
│   ├── repository.py      # read-only artifact projections
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── App.tsx        # screens and client-side navigation
│   │   ├── Chart.tsx      # Lightweight Charts adapter
│   │   ├── api.ts         # authenticated GET-only API client
│   │   ├── telegram.ts    # Telegram WebApp bootstrap/theme
│   │   └── styles.css
│   ├── package.json
│   └── vite.config.ts
└── shared/
    ├── contracts.ts       # frontend contracts
    └── models.py          # frozen backend contracts
```

## Data flow

```text
Persisted runtime artifacts
  ├── decision_debug.csv / signals_v3.csv
  ├── trades.csv
  ├── ohlcv_cache/*.csv
  ├── research.db
  └── Research Lab shadow ledger
          │ read only
          ▼
ReadOnlyRepository → frozen API contracts → authenticated GET API
          │
          ▼
React data cache → local symbol search → screens / Lightweight Charts
```

The repository uses the existing immutable `SignalCardPayload` adapter. Entry,
SL and TP1 are returned only from a saved decision or matching open/exact trade
snapshot. TP2 and TP3 are returned only when explicitly persisted. No target is
interpolated or synthesized. Chart candles are copied from the existing OHLCV
cache and never fetched from an exchange by the Mini App.

## Backend API

| Method | Route | Data |
|---|---|---|
| GET | `/healthz` | process health and rollout flag |
| GET | `/api/status` | authenticated service status |
| GET | `/api/dashboard` | open count, WR, PF, Research status |
| GET | `/api/watchlist` | latest saved symbol/timeframe snapshots |
| GET | `/api/signal/{symbol}` | preferred available saved timeframe |
| GET | `/api/signal/{symbol}/{timeframe}` | exact saved timeframe |
| GET | `/api/trades/open` | persisted open trades |
| GET | `/api/trades/history` | persisted non-open trades |
| GET | `/api/stats` | shared normalized trade metrics |
| GET | `/api/research` | persisted Research Lab report |
| GET | `/api/research/rank` | persisted strategy ranking |
| GET | `/api/research/trades` | separate Research Lab shadow ledger |

Unknown symbols and unsupported timeframes return 404. Symbol and timeframe
tokens are validated before any cache path is built.

## Telegram WebApp authentication

The frontend sends Telegram `initData` in `X-Telegram-Init-Data`. The backend:

1. parses the query-string fields;
2. removes `hash` and creates Telegram's sorted data-check string;
3. derives the secret with HMAC-SHA256 using `WebAppData` and `BOT_TOKEN`;
4. verifies the supplied hash using constant-time comparison;
5. validates `auth_date` against `MINIAPP_AUTH_MAX_AGE_SECONDS`;
6. parses the immutable Telegram user;
7. compares `user.id` to `TELEGRAM_OWNER_USER_ID` when owner-only is enabled.

Invalid data returns 401, a non-owner returns 403, and a disabled Mini App
returns 404. The bot token is never sent to the frontend.

## Frontend

The React/TypeScript/Vite frontend uses Telegram theme CSS variables with a
dark fallback. Telegram WebApp is expanded during bootstrap.

Screens:

- Home: ONLINE, last update, open trades, winrate, PF and Research;
- Signals/Market: local instant search over the single loaded watchlist;
- Signal: Lightweight Charts, saved Entry/SL/TP1/TP2/TP3, confidence, quality,
  trend and saved explanation fields;
- Portfolio: persisted open positions, PnL/risk/exposure fields when present;
- Research: Shadow, strategies, ranking, promotion, feature importance and
  walk-forward availability;
- Statistics: normalized saved results;
- Settings: read-only status, with no trading controls.

Typing in search performs no network request. A signal request is made only
when a result is selected.

## Contracts

Backend Pydantic models use `frozen=True` and reject unexpected fields.
Frontend interfaces live in `miniapp/shared/contracts.ts`. Network contracts
carry only JSON-compatible snapshots and presentation values.

## Run and build

Backend development:

```text
venv/bin/uvicorn miniapp.backend.app:app --host 127.0.0.1 --port 8080
```

Frontend:

```text
cd miniapp/frontend
npm install
npm test
npm run build
```

When `miniapp/frontend/dist` exists, FastAPI mounts it after all `/api` routes.
A production reverse proxy should terminate TLS and forward only the Mini App
origin to this service.

## Rollout

Owner-only staging:

```text
MINIAPP_ENABLED=true
MINIAPP_OWNER_ONLY=true
TELEGRAM_OWNER_USER_ID=<owner user id>
BOT_TOKEN=<telegram bot token>
```

Keep the service bound to loopback behind HTTPS. Configure the Telegram menu
button only after the authenticated HTTPS URL is available. Enabling the Mini
App does not enable Telegram UI v2 and does not change any trading process.

## Tests

- `tests/test_miniapp_auth.py`: flags, HMAC, expiry and frozen user;
- `tests/test_miniapp_api.py`: auth, owner rollout, required GET routes and no
  POST surface;
- `tests/test_miniapp_contracts.py`: frozen contracts, no fake levels, OHLCV
  reads and path validation;
- frontend Vitest: dashboard rendering, local search, supplied targets and
  authenticated GET client behavior.
