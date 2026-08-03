# TradeWatcher Mini App deployment

## Architecture

The deployment is deliberately separated from trading runtime:

```text
Telegram WebApp
  -> HTTPS public URL
  -> Caddy
       -> Vite dist (static)
       -> /api/*, /healthz, /readyz
          -> FastAPI on 127.0.0.1:8081

Trading agent, Telegram bot and Mini App are separate processes.
```

The backend projects existing immutable CSV/JSON/SQLite artifacts. It does not
import execution, DecisionEngine or Portfolio Manager and exposes no mutation
routes.

## Configuration

Copy `miniapp/backend/.env.example` and `miniapp/frontend/.env.example` to local,
ignored files. Never commit their values. Keep these rollout defaults until the
service and HTTPS endpoint have been checked:

```text
MINIAPP_ENABLED=false
MINIAPP_OWNER_ONLY=true
MINIAPP_HOST=127.0.0.1
MINIAPP_PORT=8081
```

`MINIAPP_ALLOWED_ORIGINS` is a comma-separated HTTPS allowlist.
`MINIAPP_DATA_ROOT` points at the existing runtime data directory. The Telegram
bot token is backend-only and must never use a `VITE_` prefix.

## Build

Run `scripts/build_miniapp.sh`. It uses `npm ci`, runs frontend tests, builds Vite
and verifies `miniapp/frontend/dist/index.html`. It does not start a service.

## Backend launch

Run `python -m miniapp.backend.run` from the repository root only after providing
the local environment. The launcher refuses to start when disabled, when the
owner/token/data root is missing, or when the requested bind host is not
localhost. Uvicorn reload is disabled and shutdown is handled by Uvicorn.

## HTTPS and LaunchAgent

Use `deploy/caddy/Caddyfile.example` as a local template. Replace placeholders,
verify the public DNS name and let Caddy provision HTTPS. Directory browsing is
not enabled. `/api/*` is proxied to localhost; frontend assets are served from
`dist`.

`deploy/macos/com.tradewatcher.miniapp.plist.example` is not installed
automatically. Copy it outside Git, replace absolute paths, and provide secrets
through a protected local launch configuration or Keychain-backed wrapper. Do
not embed an env file or token in the committed plist. Load and inspect it
manually with `launchctl` only during an approved deployment window.

## Telegram BotFather

Manual operation only:

1. Open `/mybots` in BotFather.
2. Select `TradeWatcherCrypto_Bot`.
3. Open **Bot Settings** -> **Menu Button**.
4. Choose **Configure menu button**.
5. Enter the verified HTTPS Mini App URL.

The inline `⚡ Открыть TradeWatcher` button is shown only when Telegram UI v2 and
Mini App are enabled, the public URL is valid HTTPS, and the current user passes
the owner-only policy.

## Health and readiness

`GET /healthz` proves only that the process is alive. `GET /readyz` returns 503
unless the feature is enabled, the data root exists and a saved signal source is
available. Neither response contains paths, tokens or owner identifiers.

## Owner-only rollout

1. Build and scan frontend assets for secrets.
2. Start FastAPI on localhost with `MINIAPP_ENABLED=true` and owner-only enabled.
3. Verify `/healthz`, `/readyz`, HMAC rejection and owner authorization locally.
4. Enable Caddy HTTPS and verify security headers/CORS.
5. Set the public URL for the Telegram bot process.
6. Enable Telegram UI v2 only for the owner and verify the WebApp button.
7. Configure BotFather only after these checks pass.

## Rollback

Set `MINIAPP_ENABLED=false`, unload only the Mini App LaunchAgent and remove the
BotFather menu URL. Do not restart or modify the trading agent. Static assets can
remain on disk because all authenticated API access fails closed.

## Logs

The example LaunchAgent uses `logs/miniapp.log` and
`logs/miniapp_error.log`. Logs must not contain initData, tokens, owner IDs or
runtime record payloads.

## Security checklist

- Telegram initData HMAC, expiry and constant-time comparison are enforced.
- Owner authorization defaults on; feature flag defaults off.
- API routes are GET-only and rate-limited per validated user (IP fallback).
- CORS is an explicit HTTPS allowlist; request bodies are bounded.
- Security headers are applied by FastAPI and Caddy.
- Symbols/timeframes and deep links are allowlisted; traversal is rejected.
- SQLite is opened with URI `mode=ro` and `PRAGMA query_only=ON`.
- CSV caches only read bounded rows and never write runtime artifacts.
- No live execution, configuration mutation or trading control is exposed.
