# Research Lab v2

Research Lab v2 is a shadow-only, multi-strategy research platform. It does not
import, replace, or mutate the live `DecisionEngine`, risk settings, portfolio
manager, or order execution path.

## Architecture

```text
completed market snapshot
        |
        +--> LIVE_BASELINE ----------------------> existing live path (unchanged)
        |
        +--> CandidateLaboratory (isolated observer)
                |
                +--> strategies.registry
                |      +--> enabled config-backed shadow strategies
                |      +--> immutable evaluate(context)
                |
                +--> CandidateShadowTracker
                |      +--> existing CSV schema (unchanged)
                |      +--> full feature snapshot in open JSON state
                |
                +--> ResearchLab runtime (master switch, allowlist, limits)
                       +--> ResearchLab.process_cycle
                       +--> research.db
                              +--> strategies
                              +--> strategy_runs
                              +--> strategy_metrics
                              +--> feature_statistics
                              +--> walk_forward_results
                              +--> candidate_history
                                      |
                                      +--> Research Dashboard v2
                                      +--> Telegram read-only views
```

## Strategy contract

Every registered strategy defines `id`, `name`, `description`, `version`,
`enabled`, `risk_profile`, `shadow_only`, optional parameters, and an immutable
`evaluate(context)` function. Importing `strategies` auto-registers built-ins.

`LIVE_BASELINE` is registry metadata only and is marked `shadow_only=False`.
The registry never evaluates it through `evaluate_all`. All other built-ins are
shadow-only and remain disabled in registry metadata so legacy pipelines cannot
activate them. The production Research Lab allowlist is exactly
`MOMENTUM_STRICT`, `TREND_CONFIRM`, and `RISK_CONSERVATIVE`; it is effective only
when the independent `RESEARCH_LAB_ENABLED` master switch is true.

## Feature snapshots and CSV compatibility

The full snapshot is carried in memory and in the open-trade JSON state under
`feature_snapshot`. It is stored as JSON in `strategy_runs`. Closed-trade CSV
columns are not changed; its stable legacy schema remains consumable by current
walk-forward and reporting tools. New features require no SQL migration because
the snapshot is JSON.

Typical features include ATR, ADX, volume ratio, EMA distance/slope, regime,
trend, momentum, structure/risk scores, spread, volatility, and signal score.

## Metrics and ranking

The platform calculates PF, winrate, Net R, max drawdown, Sharpe, Sortino and
expectancy, then combines them with walk-forward status, better windows and
profitable windows into a deterministic final score.

Promotion gates are strict: PF and Net R must beat LIVE, at least two better and
profitable windows are required, the strategy needs 100 closed trades,
walk-forward must be `PASS`, and confidence cannot be `LOW`. Failing any gate
produces `REJECT`. Passing creates a research candidate only; automatic live
promotion is always false.

## Parameter search

`research_lab_v2.parameter_search.generate_variants` creates stable, bounded
shadow-only variants from a Cartesian parameter grid. A hash of the complete
parameter set is included in each strategy ID for reproducibility.

## Runtime integration safety

The production loop calls the observer only after normal market analysis and
`update_open_trades`. The default configuration is `RESEARCH_LAB_ENABLED=false`
and `RESEARCH_LAB_DRY_RUN=true`. In dry-run it evaluates and persists
`strategy_runs`/feature snapshots, but never creates a shadow trade or changes
candidate CSV. With dry-run explicitly disabled, trades go to a separate
`research_lab_v2_shadow_open.json` shadow book and are bounded by total,
per-strategy, per-symbol, duplicate-symbol, allowlist and 2R plan checks.

`ResearchLab.process_cycle(...)` is idempotent for the same
cycle/strategy/symbol/timeframe. SQLite uses WAL, a busy timeout, transactions,
additive migrations and fail-open lock handling. Runtime status is projected to
the main dashboard and `/researchlab`; detailed per-cycle summaries go only to
`logs/research_lab_v2.log`.

Environment settings:

- `RESEARCH_LAB_ENABLED=false`
- `RESEARCH_LAB_DB_PATH=research.db`
- `RESEARCH_LAB_PROCESS_EVERY_N_CYCLES=1`
- `RESEARCH_LAB_RANK_EVERY_N_CYCLES=12`
- `RESEARCH_LAB_FEATURE_ANALYSIS_EVERY_N_CLOSED=100`
- `MAX_ENABLED_SHADOW_STRATEGIES=3`
- `MAX_OPEN_SHADOW_TRADES_TOTAL=12`
- `MAX_OPEN_SHADOW_TRADES_PER_STRATEGY=4`
- `MAX_OPEN_SHADOW_TRADES_PER_SYMBOL=2`
- `RESEARCH_LAB_DRY_RUN=true`
- `RESEARCH_LAB_FAIL_OPEN=true`

Telegram read-only commands: `/researchlab`, `/research_rank`, `/features`,
`/strategies`, `/promotions`, and `/top`. Owner-only runtime overrides are
`/researchlab_on`, `/researchlab_off`, `/researchlab_dry_on`, and
`/researchlab_dry_off`. The agent clears overrides on restart, restoring env.

Safe first server run:

```bash
cd ~/AITradingAgent
RESEARCH_LAB_ENABLED=true RESEARCH_LAB_DRY_RUN=true venv/bin/python -c \
  'from research_lab_v2.config import settings_from_env; print(settings_from_env())'
# Apply those two env values to the existing LaunchAgent/watchdog and restart
# only through that supervisor. Do not launch a second agent process manually.
tail -100 logs/research_lab_v2.log
sqlite3 research.db 'select strategy_id, symbol, timeframe, status, would_open_trade from strategy_runs order by id desc limit 20;'
```
