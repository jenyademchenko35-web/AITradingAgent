# Telegram UI v2 Experience

## Scope and safety

Telegram UI v2 Experience is an opt-in, read-only presentation layer built on
top of the Foundation package. It does not alter LIVE_BASELINE, DecisionEngine,
execution, risk, Portfolio Manager, Research Lab strategy behavior, or legacy
commands.

Safe defaults remain:

```text
TELEGRAM_UI_V2_ENABLED=false
TELEGRAM_UI_V2_OWNER_ONLY=true
```

With both rollout flags enabled, only `TELEGRAM_OWNER_USER_ID` receives v2.
Other users continue to receive the legacy `/start`. Research Lab screens keep
an explicit owner check even if a later rollout makes the rest of v2 public.

## Screens

### Home

The owner sees a compact server card with the number of current actionable
saved decisions and an MSK timestamp. The inline menu contains:

- Signals and Market;
- Trades and Statistics;
- Analytics and Research Lab;
- Settings and Help.

### Signals and timeframe selection

The symbol list is supplied by the existing production market/watchlist helper.
No second symbol allowlist is maintained by UI v2. If the saved decision rows
are the only available source, their unique symbols are used as a fallback.

The timeframe selector is data-driven. A decision row without an explicit
timeframe is the existing production `1h` decision snapshot. `15m`, `4h`, and
`1d` are shown only when corresponding persisted snapshots exist.

### Signal Card

`telegram_ui/signal_cards.py` accepts only a frozen `SignalCardPayload`.
Entry, Stop Loss, and Take Profit come from persisted decision/trade rows. The
builder never calculates these levels. A missing accepted trade plan produces a
card without prices instead of an ATR-derived or otherwise synthetic plan.

Status mapping:

| Source | UI |
|---|---|
| HIGH PRIORITY | 🔥 СИЛЬНЫЙ СИГНАЛ |
| SETUP | 🟢 ГОТОВ К ВХОДУ |
| WATCH | 🟡 НАБЛЮДЕНИЕ |
| WAIT | 🔵 ОЖИДАНИЕ |
| NO TRADE | ⚪ НЕТ СДЕЛКИ |
| INVALID | 🔴 СИГНАЛ ОТМЕНЁН |

The Why screen renders only component scores, ADX, ATR percentage, market
regime, confirmations, reasons, limitations, and blockers already present in
the immutable payload. Missing fields are omitted.

### Market

Market displays one latest saved status per production symbol. It deliberately
does not reuse the long legacy `/market` response. Symbol buttons lead to the
same data-driven timeframe selector.

### Trades and Statistics

These are compact read-only projections over `trades.csv`:

- Trades: open count and the five latest rows;
- Statistics: Closed, Winrate, PF, Net R, Drawdown, and ten recent results.

The Details and Full report buttons reuse the existing legacy formatters. The
legacy `/trades` and `/stats` commands are unchanged.

### Research Lab

The compact owner-only screen reads `research.db`, runtime status, and the
separate Research Lab shadow ledger. It shows enabled/dry-run/real-order flags,
open and closed shadow counts, and the three configured strategy modes.

The screen exposes only navigation to Research Lab shadow trades and ranking.
It provides no runtime or trading switches.

### Settings and Help

Settings is read-only and shows rollout flags, Europe/Moscow, notification
availability, and a masked notification chat id. Help describes six user-facing
areas and links to the 22 recommended commands instead of listing the complete
legacy command surface.

## User flow

```text
/start
  -> Home
     -> Signals
        -> Symbol
           -> available timeframe
              -> Signal Card
                 -> Refresh / Why / Statistics / Chart placeholder
                 -> Back / Home
     -> Market -> Symbol -> timeframe -> Signal Card
     -> Trades -> Details
     -> Statistics -> Full report
     -> Analytics
     -> Research Lab -> Shadow trades / Ranking
     -> Settings
     -> Help -> Recommended commands
```

Every v2 transition edits the current Telegram message through the shared
pagination adapter where possible. Long legacy detail responses are split
without silent truncation.

## Callback map

All callbacks use the validated `ui:v2:` namespace and remain under Telegram's
64-byte limit.

| Callback | Destination |
|---|---|
| `ui:v2:home` | Home |
| `ui:v2:signals` | Symbol selector |
| `ui:v2:market` | Compact market |
| `ui:v2:trades` | Compact trades |
| `ui:v2:stats` | Compact statistics |
| `ui:v2:analytics` | Analytics summary |
| `ui:v2:researchlab` | Research Lab summary |
| `ui:v2:settings` | Read-only settings |
| `ui:v2:help` | Sectioned help |
| `ui:v2:symbol:<symbol>` | Available timeframes |
| `ui:v2:timeframe:<symbol>:<tf>` | Signal Card |
| `ui:v2:refresh:<symbol>:<tf>` | Refresh Signal Card |
| `ui:v2:why:<symbol>:<tf>` | Saved snapshot explanation |
| `ui:v2:signalstats:<symbol>:<tf>` | Compact statistics |
| `ui:v2:chart:<symbol>:<tf>` | Chart placeholder |
| `ui:v2:tradedetails` | Legacy trade details |
| `ui:v2:fullstats` | Legacy full statistics |
| `ui:v2:researchlab_trades` | Separate Research Lab ledger |
| `ui:v2:research_rank` | Research ranking |
| `ui:v2:commands` | Recommended commands |
| `ui:v2:back:<screen>` | Previous explicit screen |

## Payload mapping

`telegram_ui/data.py` normalizes aliases from persisted rows into
`SignalCardPayload`:

- symbol, direction/side, signal/decision/status;
- current price when explicitly persisted;
- accepted Entry/SL/TP from decision or matching trade rows;
- confidence, quality, score;
- component long/short scores and explicit component totals;
- reasons, confirmations, blockers, limitations;
- ADX, ATR percentage, market regime;
- cycle id, snapshot id, timestamp and stable fingerprint.

Risk percentage, target percentage, and RR are presentation values derived only
when all persisted plan levels exist. The adapter never creates a level.

## Rollout and fallback

Recommended owner-only test configuration:

```text
TELEGRAM_UI_V2_ENABLED=true
TELEGRAM_UI_V2_OWNER_ONLY=true
TELEGRAM_OWNER_USER_ID=<telegram-user-id>
```

Default configuration remains OFF. A v2 `/start` or `/menu` rendering exception
is logged and immediately falls back to the existing legacy dashboard. A v2
callback exception edits the current message to the legacy dashboard and does
not escape into polling. Missing symbol/timeframe data returns a safe data
unavailable screen.

## Current limitations

- The production decision history currently defaults to the real `1h` snapshot;
  other timeframe buttons appear only if future rows persist them.
- A Signal Card cannot show a plan when no accepted Entry/SL/TP was persisted.
- Chart returns a documented placeholder and generates no PNG.
- Navigation state is in-memory with a 30-minute TTL.
- UI v2 does not mutate notification, research, shadow, or trading settings.

## Next stage

The next stage may add charts from persisted OHLCV snapshots and a Telegram Mini
App. It must retain immutable payload contracts, data-driven timeframe
availability, owner rollout, and the strict separation from trading execution.
