# Telegram UI v2 Foundation

## Purpose

Telegram UI v2 Foundation is an opt-in presentation and safety layer around the existing AITradingAgent Telegram bot. It does not replace the legacy UI, modify LIVE_BASELINE, call DecisionEngine, calculate trade decisions, change risk, or access live execution.

The default rollout is deliberately inert:

```text
TELEGRAM_UI_V2_ENABLED=false
TELEGRAM_UI_V2_OWNER_ONLY=true
```

With these defaults, every existing command and legacy callback continues through its previous formatter and keyboard. The only immediate behavioral safety changes are separated owner/notification identity, access control for mutating commands, user-facing errors, stable notification deduplication, and non-destructive message pagination.

## Architecture

```text
telegram_bot_v4.py
  ├── legacy CommandHandlers and legacy CallbackQueryHandler
  ├── ui:v2 CallbackQueryHandler (^ui:v2:)
  ├── owner-guarded operational handlers
  ├── unknown-command handler
  └── paginated Telegram delivery

telegram_ui/
  ├── models.py       immutable payload contracts and signal fingerprint
  ├── callbacks.py    namespaced callback builder/parser/validation
  ├── navigation.py   in-memory TTL navigation state
  ├── permissions.py  owner identity, feature flags, require_owner
  ├── formatters.py   Russian formatting and pure signal-card rendering
  ├── keyboards.py    v2 Home/Back/pagination keyboards
  ├── errors.py       errors, correlation ids and pagination
  └── __init__.py     stable public exports
```

The `telegram_ui` package has no imports from DecisionEngine, execution, risk, Portfolio Manager, exchange adapters, LIVE trade storage, or either shadow tracker.

## Identity and permissions

Three identities are independent:

| Setting | Source | Mutable through `/start` | Purpose |
|---|---|---:|---|
| `TELEGRAM_OWNER_USER_ID` / `owner_user_id` | environment first, static config fallback | no | authorization based on `effective_user.id` |
| `TELEGRAM_NOTIFICATION_CHAT_ID` / `notification_chat_id` | environment first, owner-approved config | no | destination for proactive signal/trade notifications |
| `TELEGRAM_LAST_ACTIVE_CHAT_ID` / `last_active_chat_id` | environment/readable config | yes | last chat that opened the UI |

`/start` updates only `last_active_chat_id`. It preserves every other config field. A group chat id is never compared with the owner id.

Only the owner may run `/set_notification_chat`, which stores the current `effective_chat.id` as `notification_chat_id`. Existing deployments with legacy `bot_config.json: {"chat_id": ...}` retain that value as a read-only compatibility fallback until the owner explicitly migrates it.

`require_owner` returns exactly:

```text
Эта команда доступна только владельцу бота.
```

The guarded command set is:

- `/backfill`;
- `/ready`;
- `/learning`;
- `/modules`;
- `/accuracy`;
- `/rootcause`;
- `/posttrade`;
- `/calibration`;
- `/research`;
- `/experiments`;
- `/learn`;
- `/filters`;
- `/blocked`;
- `/regime`;
- `/researchlab_on`;
- `/researchlab_off`;
- `/researchlab_dry_on`;
- `/researchlab_dry_off`;
- `/set_notification_chat`;
- `/help_admin`.

The Dashboard callback no longer synchronizes or writes reports. It now uses the same read/format path as `/dashboard`.

## Payload contracts

All contracts are frozen dataclasses. `SignalCardPayload` contains:

```text
symbol, side, status, timeframe, strategy_id,
current_price, entry, stop_loss, take_profit,
risk_reward, risk_percent, target_percent,
confidence, quality, score,
blockers, reasons,
trend_1h, trend_4h, trend_1d,
timestamp, cycle_id, snapshot_id, signal_fingerprint
```

The formatter consumes these values without calculating a trade plan. The active legacy notification path now receives the Entry/SL/TP values already calculated and accepted by the agent. The values are passed to the notification adapter after `open_trade`; the decision, eligibility, risk and execution branches remain unchanged.

Other contracts are `MarketOverviewPayload`, `TradeCardPayload`, `ResearchSummaryPayload`, `NavigationContext`, and `UserContext`.

## Callback protocol

All new callback data is ASCII, validated, limited to Telegram's 64-byte maximum, and routed by a dedicated handler pattern `^ui:v2:`.

Supported forms:

```text
ui:v2:home
ui:v2:signals
ui:v2:market
ui:v2:symbol:BTCUSDT
ui:v2:timeframe:BTCUSDT:1h
ui:v2:research
ui:v2:researchlab
ui:v2:back:<screen>
ui:v2:page:<screen>:<number>
```

The parser rejects unknown actions, missing/extra arguments, unsafe tokens, negative pages, and oversized data. An unknown or stale v2 button receives:

```text
Эта кнопка устарела. Откройте главное меню заново.
```

The legacy callback handler remains registered after the v2 handler. Existing values such as `dashboard`, `market`, `symbol:*`, and `dev:*` keep working. Unknown legacy callback values now receive the same stale-button response instead of silently opening Help.

## Navigation

`NavigationStore` is an in-memory, lock-protected TTL store. It records:

- current and previous screen;
- selected symbol;
- selected timeframe;
- page;
- last message id.

The default TTL is 30 minutes. Expired state returns to Home and is removed. Deep-screen and pagination keyboards always include `Назад` and `Главное меню`. No database or runtime CSV is used for navigation.

## Formats

The shared presentation rules are:

- Russian user-facing labels;
- Europe/Moscow timestamps with an explicit `MSK` suffix;
- UTC only when `diagnostics_utc=True`;
- adaptive price precision without trailing zeroes;
- signed percentages such as `+2.00%`;
- signed R values such as `-1.00R` and `+2.00R`;
- `LONG` → `ЛОНГ`, `SHORT` → `ШОРТ` in user cards.

Strategy ids remain available in research payloads but are not forced into ordinary user screens.

## Errors and pagination

Unknown commands receive:

```text
Команда не найдена. Откройте /menu или /help.
```

Data adapters may use:

```text
Данные временно недоступны. Попробуйте ещё раз через минуту.
```

Unhandled errors produce a short response with a random correlation id. Full exception details are emitted only through the Python logger.

`split_text`, `send_paginated_text`, and `edit_paginated_text` keep every chunk below 3,900 characters and preserve the final keyboard. `AIResearchDashboard` returns its full report instead of slicing at 4,096 characters, so lower report sections are no longer silently lost.

## Stable signal deduplication

The setup fingerprint is derived only from:

```text
symbol + side + timeframe + entry + stop_loss + take_profit + status
```

Timestamp, current price, localized text and message rendering are excluded. If a decision supplies an explicit `signal_fingerprint`, it takes precedence.

Cooldown is stored separately from identity. `TELEGRAM_SIGNAL_COOLDOWN_SECONDS` defaults to 3,600 seconds. `last_notification.json` now contains a bounded map of fingerprint → sent timestamp and remains able to read the old `{ "signal": ... }` shape.

## Help and command inventory

The new compact help is split into:

- `/help` — primary tasks;
- `/help_signals` — market and signals;
- `/help_trading` — trades and performance;
- `/help_research` — Research and Research Lab;
- `/help_admin` — owner-only operational commands.

BotFather registration is intentionally unchanged during the foundation rollout. `RECOMMENDED_BOTFATHER_COMMANDS` contains 22 proposed primary commands. The complete 71-command legacy inventory remains in `reports/telegram_ui_inventory.json`.

## Feature flags and rollout

### Default

```text
TELEGRAM_UI_V2_ENABLED=false
TELEGRAM_UI_V2_OWNER_ONLY=true
```

All users receive legacy UI. No v2 keyboard is emitted.

### Owner canary

```text
TELEGRAM_OWNER_USER_ID=<telegram user id>
TELEGRAM_UI_V2_ENABLED=true
TELEGRAM_UI_V2_OWNER_ONLY=true
```

Only the configured owner receives v2 Home and can follow `ui:v2:*` buttons. Other users continue receiving the legacy UI.

### Broader UI rollout

```text
TELEGRAM_UI_V2_ENABLED=true
TELEGRAM_UI_V2_OWNER_ONLY=false
```

This should happen only after owner canary verification. It still does not grant operational commands: `require_owner` remains active independently of the UI rollout flags.

## Backward compatibility

- No legacy command was removed or renamed.
- BotFather commands are not automatically replaced.
- Legacy callback data and keyboards remain active.
- Legacy notification chat configuration remains readable.
- Existing signal message formatting remains available.
- Existing report calculations and trading logic are unchanged.
- Feature flags default to the legacy interface.
- The new package does not access live execution.

## Verification boundaries

Tests cover immutable contracts, owner user-id authorization, `/start` isolation, guarded operations, callback validation, legacy callback compatibility, stale callbacks, Back/Home navigation, TTL, Russian number/time formats, pagination, safe feature defaults, owner-only rollout, stable fingerprints, cooldown persistence, and setup-field changes.
