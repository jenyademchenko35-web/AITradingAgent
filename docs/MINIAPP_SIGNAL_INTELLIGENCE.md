# TradeWatcher Mini App — Signal Intelligence

## Scope

Signal Intelligence is a read-only projection of saved AITradingAgent evidence.
It does not call DecisionEngine, execution, risk, Portfolio Manager, exchange
adapters, or Research Lab evaluation. It cannot create a signal, synthesize a
trade plan, change configuration, or write a runtime artifact.

The explicit guard is:

```python
SIGNAL_INTELLIGENCE_READ_ONLY = True
```

Mini App rollout remains controlled by the existing safe defaults:

```text
MINIAPP_ENABLED=false
MINIAPP_OWNER_ONLY=true
```

## Sources

Signal snapshot and history sources are read independently:

- `decision_debug.csv`;
- `signals_v3.csv`;
- `setup_history_v3.csv`;
- `research.db.strategy_runs.feature_snapshot_json`, opened with SQLite
  `mode=ro` and `PRAGMA query_only=ON`;
- matching immutable signal/trade data already exposed by the Telegram UI v2
  payload adapter.

Similar trades require one explicit source and never merge ledgers:

| Source | Artifact |
|---|---|
| `LIVE` | `trades.csv` |
| `LEGACY_SHADOW` | `candidate_shadow_trades.csv` |
| `RESEARCH_LAB` | `research_lab_shadow_history.csv` |

OHLCV, signals, trades, Research Lab ledgers, and SQLite are never modified.

## Contract

`SignalIntelligencePayload` is a frozen Pydantic contract. Optional fields stay
`null` when the corresponding source value is absent. The contract contains:

- identity: symbol, timeframe, side, status, strategy, fingerprint and cycle;
- persisted plan: current price, Entry, SL, TP, RR, risk and target percent;
- component scores and explicitly persisted maximum scores;
- RSI, ADX, ATR, volume, spread, regimes and session;
- multi-timeframe/component directions;
- confirmations, warnings, blockers, veto reasons and failed filters;
- similar-setup aggregates only after the sample-size guard passes.

The adapter does not create a missing fingerprint or timestamp. The Execution
section is hidden unless Entry, SL and TP all exist in the saved payload.

## Evidence-only explanations

`build_evidence_explanation()` accepts only `SignalIntelligencePayload`. It
deduplicates and groups saved evidence:

- confirmations come only from `confirmations`/saved reasons;
- limitations come only from warnings, failed filters and veto reasons;
- blockers come only from saved blockers.

It does not infer text from a status, fabricate an LLM explanation, or claim
that a threshold passed without evidence. For `NO TRADE`, `WAIT`, or `WATCH`,
the UI labels the same evidence as “Почему нет сделки”.

## History

History is scoped by normalized `symbol + timeframe` and optional strategy
identity. Every point is a real persisted row and includes its source label.
The service sorts by the saved timestamp, removes only exact duplicate source
records, caps source reads, and returns paginated results.

When no rows exist:

```json
{
  "status": "NO_HISTORY",
  "items": [],
  "count": 0,
  "total": 0,
  "page": 1,
  "page_size": 50
}
```

No interpolation or synthetic chart points are added.

## Changes

The changes projection compares the current and previous real point and
exposes the point three cycles ago when at least four points exist. It returns
numeric deltas for available values, status/side transitions, and evidence set
changes.

The UI intentionally avoids “better” or “worse” labels. Risk and other metrics
can have strategy-dependent semantics, so the interface shows numeric deltas,
transitions, and added/removed evidence only.

## Requirements

A missing requirement is returned only when its exact threshold is saved:

1. an evidence string contains a numeric comparison, such as `ADX:17<20`; or
2. the snapshot contains an explicit threshold field, such as `required_adx`,
   `min_rr`, `momentum_threshold`, or `max_spread`.

Each item reports `metric`, `current_value`, `required_value`, `comparison`,
`status`, and the exact evidence source. Unknown thresholds produce
`NO_KNOWN_REQUIREMENTS`; no registry defaults or guessed numbers are shown.

## Similar setup matching

Matching uses only features available on both the current snapshot and a saved
trade: strategy, exact symbol, side, timeframe, market regime, trend direction,
volatility regime, score range, and confidence range. `matched_features` makes
the score auditable.

Results are bounded and paginated. Aggregate winrate, Profit Factor and average
R stay `null` until at least `MINIAPP_SIMILAR_MIN_SAMPLE` rows with numeric
`pnl_r` exist. The default minimum is 20. Below it the frontend displays:

```text
Недостаточно похожих сделок для статистики.
```

## API

All endpoints require validated Telegram WebApp `initData` and owner access
when `MINIAPP_OWNER_ONLY=true`:

| Method | Endpoint |
|---|---|
| GET | `/api/signal/{symbol}/{timeframe}/intelligence` |
| GET | `/api/signal/{symbol}/{timeframe}/history?page=1&page_size=50` |
| GET | `/api/signal/{symbol}/{timeframe}/changes` |
| GET | `/api/signal/{symbol}/{timeframe}/requirements` |
| GET | `/api/signal/{symbol}/{timeframe}/similar?source=LIVE&page=1&page_size=20` |

POST, PUT, PATCH, and DELETE are not registered and return HTTP 405.

## Frontend routes

```text
/signals/:symbol/:timeframe
/signals/:symbol/:timeframe/intelligence
/signals/:symbol/:timeframe/history
/signals/:symbol/:timeframe/similar
```

The existing Mini App signal card gains links for “Почему?”, “История”, “Что
изменилось?”, “Что нужно?” and “Похожие сделки”. The Telegram bot signal card
is not changed.

The Intelligence page contains overview, components, indicators, optional
execution plan, evidence, changes, requirements, real history charts and an
explicitly selected similar-trade source. Dates are displayed in MSK while the
saved timestamp remains unchanged in the API.

## Caching and performance

CSV reads use an mtime+size cache with TTL. Cache entries contain bounded tails
and invalidate immediately when file mtime or size changes. Each CSV read has a
deadline and safely returns an empty result for malformed or unreadable data.

SQLite uses:

- URI `mode=ro`;
- `PRAGMA query_only=ON`;
- bounded `LIMIT`;
- a progress-handler deadline;
- no project migration helper, because that helper has write behavior.

Frontend requests at most 50 history points and 20 similar trades per page. It
never loads the entire historical ledger.

Configuration:

```text
MINIAPP_CACHE_TTL_SECONDS=5
MINIAPP_QUERY_TIMEOUT_SECONDS=2.0
MINIAPP_MAX_SOURCE_ROWS=10000
MINIAPP_SIMILAR_MIN_SAMPLE=20
```

## Security properties

- Telegram initData HMAC and age validation remain mandatory;
- owner-only behavior remains controlled by the existing feature flag;
- API code has no DecisionEngine, execution, risk, Portfolio Manager, or
  configuration-writer imports;
- source paths are fixed by the backend, while symbols/timeframes and source
  enums are validated;
- no route can open/close a trade, enable a strategy, or change Research Lab
  mode;
- tests verify GET-only routes and unchanged runtime mtimes.

## Current limitations

- legacy baseline CSV rows may not contain RSI, ATR, volume, cycle IDs, maximum
  scores, or exact thresholds; those fields remain null;
- TP2/TP3 are outside this contract and remain available only on the basic
  signal card when explicitly stored;
- same-asset-class matching is used only when an asset-class field exists; no
  asset class is inferred from a symbol;
- similarity is descriptive research output and does not influence trading;
- malformed source files fail to an empty state and are not repaired by the
  Mini App.
