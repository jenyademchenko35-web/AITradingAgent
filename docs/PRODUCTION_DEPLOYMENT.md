# TradeWatcher Mini App production deployment

## Scope and architecture

This package deploys only the read-only Mini App. It does not start, stop or
modify the trading agent, DecisionEngine, execution, risk, portfolio, research,
CSV or SQLite runtime artifacts.

```text
Internet / Telegram
  -> https://example.com
  -> Caddy: static Vite build and TLS
  -> FastAPI: 127.0.0.1:8081
  -> existing immutable AITradingAgent runtime data
```

FastAPI serves `miniapp/frontend/dist` when that directory is present. Caddy is
the production entry point: it terminates HTTPS, serves SPA deep links and
proxies only `/api/*`, `/healthz` and `/readyz` to the private loopback backend.

## Build

From the repository root, run:

```sh
scripts/build_miniapp.sh
```

It installs locked frontend dependencies, runs frontend tests and writes a
hashed Vite build to `miniapp/frontend/dist`. Confirm that `dist/index.html` and
its `/assets/*` references are present before exposing the service.

## Backend configuration

Copy `miniapp/backend/.env.example` to the ignored local
`miniapp/backend/.env`. Replace only placeholders locally:

```text
MINIAPP_ENABLED=true
MINIAPP_DEV_MODE=false
MINIAPP_OWNER_ONLY=true
MINIAPP_OWNER_USER_ID=<owner Telegram user id>
TELEGRAM_BOT_TOKEN=<bot token>
MINIAPP_HOST=127.0.0.1
MINIAPP_PORT=8081
MINIAPP_PUBLIC_URL=https://example.com
MINIAPP_ALLOWED_ORIGINS=https://example.com
MINIAPP_DATA_ROOT=<repository root>
```

Keep the token and owner ID in this ignored file or an equivalent protected
environment source. Never put them in frontend variables, Caddyfiles, plist
files, logs or Git. The launcher is fail-closed: disabled mode, a non-loopback
bind, a missing owner/token in owner mode, or a missing data root prevents start.

Start only the Mini App process with:

```sh
python -m miniapp.backend.run
```

Do not use reload mode in production. The app keeps `proxy_headers` constrained
to loopback, so Caddy must remain on the same host as the backend.

## Caddy

Copy `deploy/caddy/Caddyfile.example` outside Git and replace
`__PROJECT_ROOT__`; replace `example.com` with the verified public DNS name.
The template obtains HTTPS automatically through Caddy, enables gzip/zstd,
sets immutable caching only for hashed assets and keeps the backend private on
`127.0.0.1:8081`.

It sets HSTS, CSP, `X-Content-Type-Options`, `Referrer-Policy` and
`Permissions-Policy`. The CSP `frame-ancestors` allowlist is the protection for
embedding. Do **not** add `X-Frame-Options: SAMEORIGIN`: it conflicts with a
Telegram WebApp embedded by an approved Telegram origin. The backend and Caddy
both enforce the same restrictive CSP instead.

After validating the copied configuration, use the host's approved Caddy
service-management procedure. Do not open port 8081 to the Internet.

## LaunchAgent (macOS)

Copy `deploy/macos/com.tradewatcher.miniapp.plist.example` outside Git and
replace every `__PROJECT_ROOT__` placeholder. It provides `RunAtLoad`, restart
after an unsuccessful exit, a ten-second restart throttle, a working directory
and separate logs. It intentionally contains no `.env`, token, owner ID or
public URL.

Load it only during an approved deployment window using your normal `launchctl`
workflow. Confirm there is one Mini App backend process; do not use the template
to restart the trading agent or Telegram bot.

## Health and readiness

Use the public HTTPS endpoint:

```sh
curl -fsS https://example.com/healthz
curl -fsS https://example.com/readyz
```

`/healthz` means the backend can answer. `/readyz` returns success only when the
feature is enabled, the configured runtime root exists and a readable signal
source is available. Neither endpoint returns tokens, owner IDs, paths or trade
payloads. Keep `/readyz` as the rollout gate; an HTTP 503 means do not configure
BotFather yet.

## Telegram and BotFather

1. Build and publish the HTTPS site, then verify `/healthz` and `/readyz`.
2. In BotFather open the bot, then **Bot Settings** -> **Menu Button** (or the
   Mini App settings for the bot).
3. Set the verified `https://example.com` URL and save it.
4. Open the menu from Telegram as the configured owner.
5. Verify that the Mini App has the Telegram theme, authenticates `initData`,
   renders a read-only dashboard and rejects an invalid or expired initData.

Telegram WebApp `initData` is authenticated only by the backend HMAC/TTL and
owner checks. `initDataUnsafe` is display-only; never make authorization
decisions from it. The existing bot webhook/polling configuration is independent
of this URL and must not be changed by this rollout.

## Update and rollback

For an update: build the frontend, run tests, deploy the new static build and
code through the approved release process, then restart only the Mini App
backend. Check health, readiness and owner authentication before declaring the
release active.

For rollback: set `MINIAPP_ENABLED=false`, stop only the Mini App LaunchAgent
and remove the BotFather menu URL if needed. The Mini App fails closed; do not
restart, edit or roll back AITradingAgent runtime processes or data.
