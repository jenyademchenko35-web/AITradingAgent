# Telegram UI Current Audit

Audit target: `@TradeWatcherCrypto_Bot` as implemented in the current AITradingAgent worktree. This is a static, read-only code audit. No bot process, server process, formatter with write side effects, command, callback, or trading path was executed. Runtime CSV/JSON/SQLite files were not modified.

Inventory summary:

- 71 slash commands are registered in `build_app()`.
- 47 commands are published through the BotFather command menu; 24 registered commands are hidden from that menu.
- One unfiltered `CallbackQueryHandler` routes 18 recognized callback forms. Active keyboards emit 16 unique callback forms; two routes are latent.
- Three real keyboard layouts exist, plus one compatibility wrapper in `telegram_bot_v4.py`.
- All UI responses and proactive notifications are plain text. There are no photo, chart, animation, or document senders.

## 1. Current Architecture

The production entry point is `telegram_bot_v4.py`. `build_app()` creates a `python-telegram-bot` polling application, registers all 71 `CommandHandler` objects, one global `CallbackQueryHandler(handle_button)`, and `on_error`. Startup calls `register_bot_commands()`, which publishes `BOT_COMMANDS_V5` from `telegram_handlers.py`.

The active layers are:

| Layer | Files | Responsibility |
|---|---|---|
| Bot/controller | `telegram_bot_v4.py` | command registration, handlers, callback routing, file readers, many legacy formatters, truncation and polling |
| Menu definitions | `telegram_handlers.py` | BotFather menu and three inline keyboards |
| Compact UI v5 | `telegram_formatters.py` | Dashboard-adjacent Market, Opportunities, Watchlist, Trades, Statistics, Coach, Settings and Developer screens |
| Research dashboard | `ai_research_dashboard.py`, `research_dashboard.py`, `research_lab_v2/dashboard.py` | combined research dashboard and Research Lab v2 views |
| Domain formatters | `dashboard/dashboard_formatter.py`, `live_monitor/formatters.py`, `news_observer/formatter.py`, `research_orchestrator/formatter.py`, `research_consensus/consensus_formatter.py`, `adaptive_research/formatter.py` | live, news, research, consensus and adaptive sections |
| Proactive notifications | `notification_manager.py`, `trade_close_notifier.py`, `multi_timeframe_agent_v3.py` | open-signal and closed-trade Telegram messages |
| Inactive/plan material | `legacy/telegram_bot.py`, `telegram_webapp_plan.md` | one legacy signal-list formatter and a WebApp design document; neither is registered by v4 |

There is no `telegram_menu.py`. Callback definitions are concentrated in `telegram_handlers.py`; callback execution is concentrated in `telegram_bot_v4.handle_button`. No other production Python file registers Telegram callbacks.

Delivery behavior:

- Slash commands call shared `reply()`, which always sends a new `reply_text` and attaches the main keyboard unless the handler supplies Market or Developer keyboard.
- Button presses normally call `edit_message_text`, replacing the current screen. A non-"message is not modified" `BadRequest` falls back to a new `reply_text`.
- Text is sent without `parse_mode`. `truncate()` slices at 3,900 characters and appends a Russian truncation marker.
- `AIResearchDashboard.format_telegram()` independently slices at 4,096 characters, so Dashboard has two truncation layers.
- `on_error()` prints only to stdout. It does not acknowledge the failure to the user.

The BotFather menu is code-driven, not a separate external manifest: `register_bot_commands()` sends the 47 `BOT_COMMANDS_V5` entries on every bot startup.

## 2. Commands

Access classification is based on code. Only four Research Lab runtime overrides perform an owner check. Every other handler, including Developer and backfill/report-refresh commands, is reachable by any Telegram user who can contact the bot.

| Command | Purpose | Access | Handler | Primary data source | Response | Buttons | Delivery | Main error mode |
|---|---|---|---|---|---|---|---|---|
| /start | Register the current chat as the notification destination and open the research dashboard. | all | `start` | derived runtime/report data through format_dashboard | plain text via `format_dashboard` | main | new | uncaught → stdout only |
| /help | Show the hand-written command guide. | all | `help_command` | derived runtime/report data through help_text | plain text via `help_text` | main | new | uncaught → stdout only |
| /dashboard | Show the combined AI Research Dashboard; optional research/live drill-down section. | all | `dashboard_command` | trades.csv, agent_v3_stats.json | plain text via `format_dashboard` | main | new | missing/stale → N/A, zero or empty state |
| /coach | Show compact AI Coach observations and blockers. | all | `coach_command` | derived runtime/report data through format_coach | plain text via `format_coach` | main | new | uncaught → stdout only |
| /opportunities | Rank current setup and near-setup opportunities. | all | `opportunities_command` | decision_debug.csv, decision_diagnostics.csv | plain text via `format_opportunities` | main | new | missing/stale → N/A, zero or empty state |
| /settings | Show read-only runtime settings. | all | `settings_command` | derived runtime/report data through format_settings | plain text via `format_settings` | main | new | uncaught → stdout only |
| /developer | Open the developer/research menu. | all | `developer_command` | derived runtime/report data through format_developer | plain text via `format_developer` | developer | new | uncaught → stdout only |
| /v2 | Show v2 readiness status. | all | `v2_command` | derived runtime/report data through format_v2_readiness | plain text via `format_v2_readiness` | main | new | uncaught → stdout only |
| /symbols | Show per-symbol signal coverage. | all | `symbols_command` | derived runtime/report data through format_symbols_report | plain text via `format_symbols_report` | main | new | uncaught → stdout only |
| /equity | Show equity report. | all | `equity_command` | derived runtime/report data through format_equity_report | plain text via `format_equity_report` | main | new | uncaught → stdout only |
| /timeline | Show decision timeline. | all | `timeline_command` | derived runtime/report data through format_timeline_report | plain text via `format_timeline_report` | main | new | uncaught → stdout only |
| /status | Show legacy agent status summary. | all | `status_command` | derived runtime/report data through format_status | plain text via `format_status` | main | new | uncaught → stdout only |
| /market | Show compact market state and symbol drill-down buttons. | all | `market_command` | decision_debug.csv, signals_v3.csv | plain text via `format_market` | market | new | missing/stale → N/A, zero or empty state |
| /watchlist | Show actionable and near-actionable symbols. | all | `watchlist_command` | decision_debug.csv, signals_v3.csv | plain text via `format_watchlist` | main | new | missing/stale → N/A, zero or empty state |
| /diagnostics | Show decision diagnostics for a requested symbol. | all | `diagnostics_command` | decision_diagnostics.csv, decision_debug.csv | plain text via `format_diagnostics` | main | new | missing/stale → N/A, zero or empty state |
| /stats | Show closed-trade performance metrics. | all | `stats_command` | trades.csv | plain text via `format_stats` | main | new | missing/stale → N/A, zero or empty state |
| /riskstats | Show Risk Engine distribution and outcome statistics. | all | `riskstats_command` | decision_debug.csv, trades.csv | plain text via `format_riskstats` | main | new | missing/stale → N/A, zero or empty state |
| /trades | Show open live trades; the health argument redirects to data quality. | all | `trades_command` | trades.csv | plain text via `format_dataquality, format_trades` | main | new | missing/stale → N/A, zero or empty state |
| /dataquality | Show persisted Trade Registry/research data quality. | all | `dataquality_command` | data_quality_report.json | plain text via `format_dataquality` | main | new | missing/stale → N/A, zero or empty state |
| /coverage | Show field/data coverage. | all | `coverage_command` | data_quality_report.json | plain text via `format_coverage` | main | new | missing/stale → N/A, zero or empty state |
| /backfill | Run the research backfill pipeline and show recovered/unrecoverable fields. | all | `backfill_command` | derived runtime/report data through backfill_command | plain text via `backfill_command` | main | new | uncaught → stdout only |
| /snapshot | Show the latest decision snapshot, with an in-memory fallback build. | all | `snapshot_command` | decision_snapshot.json, current research inputs | plain text via `format_snapshot` | main | new | missing/stale → N/A, zero or empty state |
| /portfolio | Show Portfolio Manager report or compute a current summary. | all | `portfolio_command` | portfolio_report.json, trades.csv | plain text via `format_portfolio_status` | main | new | missing/stale → N/A, zero or empty state |
| /execution | Show execution simulator report; can compute normal/stress output. | all | `execution_command` | execution report/current simulator inputs | plain text via `format_execution_status` | main | new | uncaught → stdout only |
| /lossanalysis | Show loss-attribution report; can compute it if absent. | all | `lossanalysis_command` | loss attribution report, trades.csv | plain text via `format_lossanalysis_status` | main | new | missing/stale → N/A, zero or empty state |
| /decisionv2 | Show DecisionEngine v2 analytical report; can compute it if absent. | all | `decisionv2_command` | DecisionEngine v2 report/current decisions | plain text via `format_decisionv2_status` | main | new | uncaught → stdout only |
| /posttrade | Show post-trade analysis. | all | `posttrade_command` | post_trade_analysis_report.json, post_trade_analysis_trades.csv | plain text via `format_posttrade` | main | new | missing/stale → N/A, zero or empty state |
| /calibration | Show calibration report. | all | `calibration_command` | calibration_report.json | plain text via `format_calibration` | main | new | missing/stale → N/A, zero or empty state |
| /research | Show research orchestrator/dashboard overview or selected section. | all | `research_command` | research_orchestrator_report.json, research dashboard reports | plain text via `format_research` | main | new | missing/stale → N/A, zero or empty state |
| /experiments | Show experiment summary or full details. | all | `experiments_command` | strategy_experiments_report.json, strategy_experiments_results.csv | plain text via `format_experiments` | main | new | missing/stale → N/A, zero or empty state |
| /candidate | Show one Candidate Laboratory entry by id. | all | `candidate_command` | reports/candidate_laboratory.json | plain text via `format_candidate_lab_entry` | main | new | missing/stale → N/A, zero or empty state |
| /candidates | Show all Candidate Laboratory entries. | all | `candidates_command` | reports/candidate_laboratory.json | plain text via `format_candidate_lab_entry` | main | new | missing/stale → N/A, zero or empty state |
| /shadowstatus | Show isolated legacy candidate shadow validation status. | all | `shadowstatus_command` | candidate_shadow_trades.csv, candidate shadow state | plain text via `format_shadow_status` | main | new | missing/stale → N/A, zero or empty state |
| /datafeatures | Show decision feature coverage. | all | `datafeatures_command` | decision_features.csv | plain text via `format_datafeatures` | main | new | missing/stale → N/A, zero or empty state |
| /learn | Show auto-learning recommendation. | all | `learn_command` | auto_learning_recommendation.json | plain text via `format_learn` | main | new | missing/stale → N/A, zero or empty state |
| /quality | Show signal-quality analysis. | all | `quality_command` | signal quality report/current decisions | plain text via `format_signalquality_status` | main | new | uncaught → stdout only |
| /filters | Show filter effectiveness. | all | `filters_command` | filter_effectiveness_report.json | plain text via `format_filters` | main | new | missing/stale → N/A, zero or empty state |
| /blocked | Explain blocker distribution, optionally by component. | all | `blocked_command` | decision_diagnostics.csv, decision_explanations.csv | plain text via `format_blocked` | main | new | missing/stale → N/A, zero or empty state |
| /regime | Show current market-regime report. | all | `regime_command` | market_regime_report.json | plain text via `format_regime` | main | new | missing/stale → N/A, zero or empty state |
| /history | Show recent signal/trade history. | all | `history_command` | signals_v3.csv, trades.csv | plain text via `format_history` | main | new | missing/stale → N/A, zero or empty state |
| /report | Show daily report. | all | `report_command` | trades.csv, agent_v3_stats.json | plain text via `format_daily_report` | main | new | missing/stale → N/A, zero or empty state |
| /news | Show news overview or requested subsection/symbol. | all | `news_command` | market_news_feed.json, market_news_sources.json | plain text via `format_news` | main | new | missing/stale → N/A, zero or empty state |
| /heatmap | Show market heatmap. | all | `heatmap_command` | market_heatmap_report.json | plain text via `format_heatmap` | main | new | missing/stale → N/A, zero or empty state |
| /live | Show live monitor overview, symbol, trades, or setups. | all | `live_command` | live_monitor_state.json | plain text via `format_live_monitor` | main | new | missing/stale → N/A, zero or empty state |
| /system | Show live-monitor process/data health. | all | `system_command` | live_monitor_state.json | plain text via `format_live_system` | main | new | missing/stale → N/A, zero or empty state |
| /intelligence | Show Market Intelligence report. | all | `intelligence_command` | market_intelligence_report.json | plain text via `format_intelligence` | main | new | missing/stale → N/A, zero or empty state |
| /memory | Show trade-memory summary, optionally for a symbol. | all | `memory_command` | trade_memory_summary.txt, trade_market_context.csv | plain text via `format_memory` | main | new | missing/stale → N/A, zero or empty state |
| /context | Show current trade/market context. | all | `context_command` | trade_market_context.csv, market/news reports | plain text via `format_context` | main | new | missing/stale → N/A, zero or empty state |
| /lab | Show Strategy Lab report or section. | all | `lab_command` | strategy_lab_report.json, hypothesis_report.json | plain text via `format_lab` | main | new | missing/stale → N/A, zero or empty state |
| /replay | Show shadow replay overview, last trade, symbol, summary, or patterns. | all | `replay_command` | trade_replay_report.json, shadow_replay_report.json | plain text via `format_replay` | main | new | missing/stale → N/A, zero or empty state |
| /consensus | Show research-consensus overview or group. | all | `consensus_command` | research_consensus_report.json | plain text via `format_consensus` | main | new | missing/stale → N/A, zero or empty state |
| /adaptive | Show adaptive-research overview/status/recommendations/stages. | all | `adaptive_command` | adaptive_research_report.json, adaptive_research_state.json | plain text via `format_adaptive` | main | new | missing/stale → N/A, zero or empty state |
| /walkforward | Read and show the latest persisted walk-forward report. | all | `walkforward_command` | walk_forward_report.json, reports/walk_forward.json | plain text via `format_walkforward` | main | new | missing/stale → N/A, zero or empty state |
| /research_rank | Show Research Lab v2 strategy ranking. | all | `research_rank_command` | research.db | plain text via `format_research_lab_v2` | main | new | missing/stale → N/A, zero or empty state |
| /features | Show Research Lab v2 best/worst features. | all | `features_command` | research.db | plain text via `format_research_lab_v2` | main | new | missing/stale → N/A, zero or empty state |
| /strategies | Show Research Lab v2 strategy registry. | all | `strategies_command` | research.db | plain text via `format_research_lab_v2` | main | new | missing/stale → N/A, zero or empty state |
| /promotions | Show Research Lab v2 promotion decisions. | all | `promotions_command` | research.db | plain text via `format_research_lab_v2` | main | new | missing/stale → N/A, zero or empty state |
| /top | Alias of the Research Lab v2 top-strategy ranking. | all | `top_command` | research.db | plain text via `format_research_lab_v2` | main | new | missing/stale → N/A, zero or empty state |
| /researchlab | Show Research Lab v2 runtime state and diagnostics. | all | `researchlab_command` | research.db, research_lab_runtime_status.json | plain text via `format_research_lab_v2` | main | new | missing/stale → N/A, zero or empty state |
| /researchlab_trades | Show only the isolated Research Lab shadow ledger. | all | `researchlab_trades_command` | research_lab_shadow_trades.json | plain text via `format_research_lab_v2` | main | new | missing/stale → N/A, zero or empty state |
| /researchlab_on | Owner-only runtime override: enable Research Lab v2. | owner | `researchlab_on_command` | derived runtime/report data through researchlab_on_command | plain text via `researchlab_on_command` | main | new | uncaught → stdout only |
| /researchlab_off | Owner-only runtime override: disable Research Lab v2. | owner | `researchlab_off_command` | derived runtime/report data through researchlab_off_command | plain text via `researchlab_off_command` | main | new | uncaught → stdout only |
| /researchlab_dry_on | Owner-only runtime override: enable global Research Lab dry-run. | owner | `researchlab_dry_on_command` | derived runtime/report data through researchlab_dry_on_command | plain text via `researchlab_dry_on_command` | main | new | uncaught → stdout only |
| /researchlab_dry_off | Owner-only runtime override: disable global Research Lab dry-run. | owner | `researchlab_dry_off_command` | derived runtime/report data through researchlab_dry_off_command | plain text via `researchlab_dry_off_command` | main | new | uncaught → stdout only |
| /promotion | Show experiment promotion engine status/details. | all | `promotion_command` | experiment_promotion_report.json | plain text via `format_promotion` | main | new | missing/stale → N/A, zero or empty state |
| /ready | Synchronize reports and show the VPS promotion gate. | all | `ready_command` | reports/promotion_gate.json, trades.csv | plain text via `format_promotion_gate` | main | new | missing/stale → N/A, zero or empty state |
| /learning | Synchronize reports and show decision-learning summary. | all | `learning_command` | decision_learning.json, trades.csv | plain text via `format_decision_learning` | main | new | missing/stale → N/A, zero or empty state |
| /modules | Synchronize reports and show module accuracy. | all | `modules_command` | decision_learning.json, trades.csv | plain text via `format_decision_learning` | main | new | missing/stale → N/A, zero or empty state |
| /accuracy | Synchronize reports and show decision accuracy. | all | `accuracy_command` | decision_learning.json, trades.csv | plain text via `format_decision_learning` | main | new | missing/stale → N/A, zero or empty state |
| /rootcause | Build and show current decision root causes. | all | `rootcause_command` | decision logs/trades.csv | plain text via `format_telegram` | main | new | missing/stale → N/A, zero or empty state |
| /datasources | Show report provenance, freshness, source timestamps, and samples. | all | `datasources_command` | canonical report metadata and trades.csv timestamps | plain text via `format_datasources` | main | new | uncaught → stdout only |

Important command-level observations:

- `/start` is not merely a welcome screen: it overwrites `bot_config.json` with the current chat id, then opens the full AI Research Dashboard.
- `/backfill` invokes `run_backfill_pipeline()` and is not owner-only. `/ready`, `/learning`, `/modules`, and `/accuracy` invoke `synchronize_reports()`. These are operational actions embedded in a mostly read-oriented UI.
- `/dashboard` and the Dashboard button are not equivalent: the button runs `synchronize_reports()` first, while the slash command directly formats the dashboard.
- `/walkforward` explicitly reads persisted reports and does not run validation.
- `/researchlab`, `/researchlab_trades`, ranking and feature views read Research Lab storage. The four on/off commands write runtime override state and are the only guarded commands.
- `/research_rank`, `/features`, `/strategies`, `/promotions`, `/top`, and the four Research Lab override commands are registered but not present in the BotFather menu. `/help` also omits several registered commands.
- There is no catch-all unknown-command handler. An unknown slash command normally receives no answer.

The 24 registered commands absent from BotFather are: `/v2`, `/symbols`, `/equity`, `/timeline`, `/status`, `/diagnostics`, `/posttrade`, `/calibration`, `/experiments`, `/learn`, `/filters`, `/blocked`, `/regime`, `/history`, `/report`, `/research_rank`, `/features`, `/strategies`, `/promotions`, `/top`, `/researchlab_on`, `/researchlab_off`, `/researchlab_dry_on`, `/researchlab_dry_off`.

## 3. Menus and Callbacks

There are three actual keyboard builders:

1. `main_keyboard()` — 9 root actions in five rows; no Back button because it is the root.
2. `developer_keyboard()` — 6 developer actions and one `⬅️ Dashboard` button.
3. `market_keyboard(symbols)` — dynamic symbol buttons, three per row, plus `⬅️ Dashboard`.

`telegram_bot_v4.main_keyboard()` is only a compatibility wrapper around the imported v5 main keyboard and is not a fourth layout.

| Button text | callback_data | Handler | Destination | Back path | Status / issue |
|---|---|---|---|---|---|
| 📊 Dashboard | `dashboard` | `handle_button` | AI Research Dashboard | yes | synchronize_reports is run before display |
| 📈 Market | `market` | `handle_button` | Market list | no | active |
| 🎯 Opportunities | `opportunities` | `handle_button` | Opportunity ranking | no | active |
| 📋 Watchlist | `watchlist` | `handle_button` | Watchlist | no | active |
| 📂 Trades | `trades` | `handle_button` | Open trades | no | active |
| 📊 Statistics | `stats` | `handle_button` | Trade statistics | no | active |
| 🧠 AI Coach | `coach` | `handle_button` | AI Coach | no | active |
| ⚙️ Settings | `settings` | `handle_button` | Read-only settings | no | active |
| 🛠 Developer | `developer` | `handle_button` | Developer submenu | yes | active |
| Replay | `dev:replay` | `handle_button` | Replay overview | yes | active |
| Diagnostics | `dev:diagnostics` | `handle_button` | Diagnostics usage hint | yes | does not open symbol diagnostics |
| Experiments | `dev:experiments` | `handle_button` | Experiment summary | yes | active |
| Research | `dev:research` | `handle_button` | Research overview | yes | active |
| Dry Runs | `dev:dryrun` | `handle_button` | Dry-run file counts | yes | active |
| Reports | `dev:reports` | `handle_button` | Report availability | yes | active |
| {symbol without /USDT} | `symbol:{symbol}` | `handle_button` | Symbol detail | yes | retains the symbol grid; no explicit Back to Market button |
| — (not emitted) | `blocked:{blocker}` | `handle_button` | Blocker detail | yes | latent/stale route; handler attaches the main keyboard |
| — (not emitted) | `regime:view` | `handle_button` | Market regime detail | yes | latent/stale route; handler attaches the main keyboard |

Counts and routing details:

- One callback handler catches all callback queries because no regex pattern is specified.
- Static source contains 18 button placements; `dashboard` is repeated in all three layouts. Market adds a dynamic number of `symbol:*` buttons.
- Active keyboards emit 16 unique callback forms: 15 fixed values plus `symbol:{symbol}`.
- `blocked:{blocker}` and `regime:view` are still recognized but no active keyboard creates them. They are stale/latent callback routes.
- Unknown callback data silently shows `/help` content because `actions.get(query.data, help_text)` is the fallback.
- Market detail does not have a literal "Back to Market" button, but it retains the complete market symbol keyboard and Dashboard button. Developer has a clear Dashboard return. Other callback screens revert to the main keyboard.
- Slash-command screens are not dead ends because `reply()` attaches a keyboard, but deep research commands have no contextual Back button or drill-down buttons—only the generic root keyboard.

## 4. User Flows

### First launch

`/start` stores the current chat id in `bot_config.json`, builds the full AI Research Dashboard, and sends a new message with the main keyboard. There is no onboarding, access explanation, privacy warning, or choice of notification destination. A later user calling `/start` replaces the stored chat id.

### View the market and a coin

`/market` sends a compact symbol status list with the dynamic Market keyboard. Pressing a symbol edits that message into a detail screen with decision, direction, confidence, weighted score, edge, missing factors, news status and reason. The same symbol grid remains attached. A user can select another coin or return directly to Dashboard.

### View open trades

`/trades` reads `trades.csv` and shows each open trade as symbol/direction plus Entry, SL and TP. Empty state shows the last trade label. `/trades health` unexpectedly switches to Data Quality rather than trade health, which is discoverable only from help text.

### View statistics

`/stats` reads normalized closed trades and shows closed count, winrate, PF, Net R, max drawdown, incomplete metrics and the last 20 outcomes. The main keyboard remains attached.

### View Research Dashboard

`/dashboard` builds the large combined report: agent, trading, research, candidate, legacy shadow validation, walk-forward, Research Lab v2, root cause, feature coverage, news, freshness and health. The message routinely approaches or exceeds the limit and is sliced, so lower sections can disappear. Pressing the Dashboard button first synchronizes reports and then edits the existing message; this path can have different freshness and side effects from `/dashboard`.

### View Research Lab

`/researchlab` sends runtime flags, strategy modes, latest-cycle counters, isolated shadow counts, last opened/closed, blocked reasons, database state, errors and per-strategy dry-run diagnostics. It can grow linearly with strategies and blocked reasons. `/research_rank`, `/top`, `/features`, `/strategies`, and `/promotions` are separate text views without Research Lab-specific navigation.

### View shadow trades

There are two distinct flows. `/shadowstatus` shows the legacy candidate shadow tracker. `/researchlab_trades` shows only the isolated Research Lab ledger (open plus last ten closed). The separation is correct in data access, but names and navigation do not visually explain the difference to a casual user.

### Return to the main menu

Button navigation returns with `⬅️ Dashboard` from Developer and Market. Most other button screens already use the root keyboard. Slash commands always send a new message with a keyboard, creating a growing chat history rather than a stable single-message app shell.

### Unknown command and errors

Unknown slash commands are not handled and normally produce silence. Known commands often degrade missing files into `N/A`, zero, `NOT_AVAILABLE`, or empty state, which can make missing data look like a valid zero. Uncaught exceptions go to `on_error()` and are printed to process stdout; no user-facing retry/error card is sent. Polling conflict and network failures are only printed by `main()`.

## 5. Message Formats

| Message family | Typical structure and size | Style observations |
|---|---|---|
| Core v5 Market/Watchlist/Opportunities | 7–many lines; empty-state outputs measured at 108–136 characters | compact, mixed RU/EN, common footer, mobile-friendly until candidate list grows |
| Core v5 Statistics/Trades/Coach/Settings | 12–20 empty-state lines; 238–311 characters locally | clearer cards, but numeric precision ranges from 0 to 4 decimals |
| `/help` | about 45 lines | command dump rather than task-oriented navigation; incomplete relative to registration |
| AI Research Dashboard | source builds roughly 200+ logical lines, then slices at 4,096 and again at 3,900 | heavily overloaded; label and value often occupy separate lines with blank lines between them |
| Research Lab runtime | about 20 base lines plus roughly 12 lines per strategy | useful diagnostics but developer-centric names and unbounded growth |
| Signal-open notification | about 25 lines | has symbol, LONG/SHORT, signal, quality, price, score, confidence, Entry/SL/TP, RR, trends, ATR and reason |
| Trade-close notification | roughly 18–26 lines | clear outcome card, then a second statistics block; useful but long for a single event |
| Legacy/research analytical screens | commonly 20–100+ lines | styles vary by module; truncation may remove conclusions |

Formatting is not unified:

- Emoji are widespread but semantics are inconsistent: the same target/chart emoji is reused for signal, confidence, Entry and ranking.
- Separators vary (`────────────`, `──────────────`, blank lines, or none).
- Russian and English are mixed within the same screen: `Open Trades`, `Would Open`, `Blocked`, `Причина`, `Статус`, `No ranked strategies`.
- Number formats vary: winrate can be integer, one decimal or two decimals; PF and Net R range from raw strings to four decimals; prices in live signal always use two decimals, which is unsuitable for low-price coins, while trade-close formatting is adaptive.
- Time is inconsistent. Some readers normalize timestamps to UTC; UI v5 `local_time()` displays normalized UTC as bare `HH:MM`; `telegram_bot_v4` also has explicit MSK formatting; footers and signal notifications use naive `datetime.now()` in the host timezone. The user is rarely told which timezone is shown.
- The common v5 footer adds Updated, version, Git and `Server: ONLINE` to every compact screen. `Server: ONLINE` is a literal, not a verified health result.
- No Markdown/HTML parse mode is used. Alignment depends entirely on newlines, emoji and Unicode rules.
- Long responses are cut rather than paginated. There are no `Next`, `Previous`, section, refresh, or compact/expanded controls.
- No photographs, charts, rendered dashboards, files, CSV exports, or documents are sent.

## 6. Available Signal Data

The live signal path is:

`run_loop → run_once → analyze_symbol → load_market/analyze_market → DecisionEngine.calculate → mandatory filters/cooldown/open-trade checks/higher-TF/Portfolio Manager → open_trade → send_notification → notification_manager.format_signal → Bot.send_message`.

The proactive signal notification is therefore a post-open notification, not a card for every `WATCH`, `WAIT`, or blocked setup. It is sent only after a trade is opened and only for quality A/B with confidence at least 75.

| Future card field | Available now | Current source/use | UI v2 work needed |
|---|---|---|---|
| Symbol | yes | `symbol` argument and `MarketSnapshot.symbol` | normalize `/USDT` display only |
| Side | yes | `DecisionResult.direction`: LONG, SHORT or NEUTRAL | use one consistent icon/color vocabulary |
| Decision state | yes | `signal`: HIGH PRIORITY, SETUP, WATCH, WAIT, NO TRADE | map technical states to user language |
| Timeframe | partial | market carries 1H, 4H and 1D; entry is implicitly 1H | add an explicit trade timeframe field/label |
| Current price | yes | `market.tf1h.close` | adaptive price precision |
| Entry | yes | current 1H close | avoid calculating it separately in multiple modules |
| Stop Loss | yes | entry ± 1H ATR | pass the accepted trade plan, not recalculate in the formatter |
| Take Profit | yes | entry ± 2 × 1H ATR | pass the accepted trade plan, not recalculate in the formatter |
| Risk/Reward | yes/calculated | absolute TP distance divided by SL distance | consistent precision and invalid-plan guard |
| Risk percent | calculable | `abs(entry-SL)/entry × 100` | calculate in a presentation/view-model layer |
| Target profit percent | calculable | `abs(TP-entry)/entry × 100` | calculate in a presentation/view-model layer |
| Confidence | yes | `DecisionResult.confidence` | explain scale and threshold |
| Quality | yes | A–E | provide user meaning, not just a letter |
| Score / edge | yes | score, long/short totals and diff | display denominator/threshold consistently |
| Reason | yes | `summary`; detailed `explanation` has component scores | shorten to a user reason and optional details |
| Blockers | available before notification | `veto_reasons`, `failed_filters`, `execution_status`, diagnostics CSV | include on WAIT/blocked cards; current open card does not receive/display them |
| Trend context | yes | 1H/4H/1D EMA trends | compact one-line display |
| Component scores/reasons | yes | Trend, Structure, Momentum, Risk engine results and `explanation` | use typed fields instead of parsing explanation text |
| Timestamp/cycle/snapshot | yes | UTC decision timestamp, cycle id, optional snapshot id | show timezone and freshness explicitly |
| Feature snapshot | conditional | attached as `research_feature_snapshot` when Research Lab is enabled | expose only selected, user-readable features |
| Strategy | not explicit in live card | LIVE path is implicitly LIVE_BASELINE | add explicit immutable strategy id to the notification view model |
| Setup id/fingerprint | partial | local setup/cooldown state; Research Lab has fingerprints | pass a stable id for reliable Telegram deduplication |
| Position size / monetary risk | not in formatter input | Portfolio Manager evaluates the plan, but result is not passed to `format_signal` | join/pass the final accepted position plan if approved for UI |
| Spread, fees, slippage | not in current card input | may exist in research/execution reports, not the notification contract | calculate from authoritative execution context, not the formatter |

The existing open-signal card already places Entry/SL/TP and RR in one block and already shows a clear LONG/SHORT label. The missing pieces are consistency, explicit strategy/timeframe, percent risk/reward, accepted-plan provenance, blocker display for non-open states, and reliable setup-level deduplication. Current duplicate detection compares the entire message, but the message includes `datetime.now()`, so an unchanged setup in a later minute is considered new.

## 7. UX Problems

The ten highest-impact issues are:

1. **Dashboard overload and data loss.** The combined dashboard is hundreds of logical lines and is silently sliced; recommendations and health near the bottom can vanish.
2. **Command discovery drift.** There are 71 handlers, 47 BotFather entries, an incomplete `/help`, and only 9 root buttons.
3. **Unsafe audience model.** Almost all commands are public; `/backfill` and report synchronization have write side effects. Any `/start` replaces the notification chat/owner basis.
4. **Inconsistent navigation.** Only Market and Developer are real submenus; deep research screens lack contextual Back/section buttons and slash commands create message clutter.
5. **Fragmented visual language.** Russian/English labels, emoji meanings, separators and label/value layouts differ by formatter family.
6. **Inconsistent time and numbers.** UTC, MSK and host-local time are mixed without labels; price and metric precision varies by screen.
7. **Signal-event semantics are weak.** Telegram deduplication uses full text containing current time, not a stable setup fingerprint; repeated conditions can generate new-looking notifications.
8. **Signal card lacks final context.** Entry/SL/TP exists, but strategy, explicit timeframe, risk %, target %, final accepted position data and blocker details are absent.
9. **No visual analytics.** There are no charts, spark lines, equity/drawdown images, heatmap images, downloadable reports or document exports.
10. **Failures are ambiguous or silent.** Missing files often look like legitimate zero values; unknown commands and uncaught handler errors give no user reply.

Additional friction includes duplicated information across `/dashboard`, `/research`, `/researchlab`, `/top` and `/research_rank`; technical identifiers such as `MOMENTUM_TOO_WEAK`, `LIVE_BASELINE`, `WF`, `NetR`, and database status shown without explanation; and the distinction between legacy shadow tracking and Research Lab shadow ledger being meaningful to developers but not clearly taught to users.

## 8. Technical Debt

- `telegram_bot_v4.py` is a controller, repository/file-access layer and formatter collection in one module of roughly 3,800 lines.
- There are overlapping formatter names (`format_dashboard`, `format_market`, `format_status`, `format_overview`) across modules, with aliases at import sites.
- Some "format" functions only read persisted data, while others compute reports or instantiate analytical engines. Button callbacks can synchronize/write reports.
- Most screens directly read global file paths. There is no shared typed Telegram view model, freshness contract, or explicit missing-data state.
- Authorization is ad hoc and only used for four commands. Stored chat id is used as both notification destination and owner identity.
- One unpatterned callback router has high branching complexity and silently maps unknown callbacks to Help.
- Callback identifiers are free-form strings with two latent routes and no central enum/versioning.
- Formatting, truncation and localization are decentralized; the large dashboard also implements its own truncation.
- Open-signal trade-plan calculation is duplicated in `analyze_symbol` and `notification_manager.format_signal`, allowing display drift from the accepted plan.
- Notification deduplication stores one full message in `last_notification.json`; it is neither per-symbol nor setup-aware.
- Test coverage exists for some Telegram/news and notification behavior, but there is no exhaustive registration/menu/callback/navigation snapshot contract.

## 9. Safe Refactoring Boundaries

UI v2 can be developed without changing LIVE_BASELINE logic if these boundaries are enforced:

- Treat DecisionEngine, filters, risk, Portfolio Manager, execution, live trade lifecycle and all shadow trackers as read-only data producers.
- Introduce typed immutable presentation models outside trading code. Map existing decision/trade/report objects into those models after decisions are complete.
- Pass the already accepted Entry/SL/TP plan into Telegram; never recalculate or alter it inside UI code.
- Keep existing slash commands as backward-compatible aliases while adding navigation in the UI layer only.
- Separate pure reads from explicit operational commands. Any write/recompute action needs owner authorization and confirmation; UI rendering itself must remain pure.
- Keep legacy candidate shadow and Research Lab shadow sources explicitly separated and labeled.
- Centralize callback constants, access policy, timezone, number formatting, missing-data states, pagination and error cards.
- Do not let callback handlers call trading/execution adapters. Telegram should consume reports and immutable state only.
- Preserve proactive notification delivery independently from menu rendering so a UI failure cannot block the agent.
- Add contract/snapshot tests before replacing legacy formatters.

Likely production files for UI v2 are:

- `telegram_bot_v4.py` — thinner routing, access middleware, unknown/error handling and pagination.
- `telegram_handlers.py` — canonical commands, keyboards and callback identifiers.
- `telegram_formatters.py` — common cards, localization, precision and compact views.
- `notification_manager.py` — signal-card view model and stable fingerprint deduplication.
- `trade_close_notifier.py` — shared trade-card presentation.
- `ai_research_dashboard.py` and `research_lab_v2/dashboard.py` — split oversized dashboards into sections/pages without changing their data logic.
- Domain formatters under `dashboard/`, `live_monitor/`, `news_observer/`, `research_orchestrator/`, `research_consensus/`, and `adaptive_research/` — only if their output is migrated to shared UI components.
- Telegram-specific tests/inventory snapshots.

`multi_timeframe_agent_v3.py` should not need behavioral changes. At most, a later UI task may pass an immutable copy of already-calculated signal/trade-plan fields to the notification adapter; the decision and execution branches must remain untouched.

## 10. Recommendations for UI v2

1. Make Dashboard a compact 8–12 line home card: agent health, market state, open trades, best opportunity and last update, with buttons to separate pages.
2. Publish one canonical command catalog and generate BotFather entries, `/help`, routing documentation and inventory from it.
3. Add an authorization middleware based on configured Telegram user ids, not notification chat id. Require owner access and confirmation for every state-changing operation.
4. Use a versioned callback scheme such as `v2:market`, `v2:symbol:BTCUSDT`, `v2:research:lab`, with explicit Back/Home/Refresh on every page.
5. Add pagination and section navigation before adding more data. Never truncate a conclusion or silently convert an unavailable metric to zero.
6. Standardize Russian-first labels with optional technical details, one timezone (explicitly `MSK` or `UTC`), adaptive prices and fixed metric precision.
7. Define reusable cards: Signal, Open Trade, Closed Trade, Strategy Rank, Research Status, Error/Missing Data and Confirmation.
8. Build signal cards from an immutable accepted-plan payload: strategy, symbol, side, timeframe, current/entry/SL/TP, risk %, target %, RR, confidence, quality, concise reason, blockers and timestamp.
9. Deduplicate by stable setup/fingerprint per strategy+symbol, never by rendered text or current display time.
10. Add optional charts only where they improve decisions: equity/drawdown, PF/Net R trend, feature importance and compact market heatmap. Keep text fallbacks.
11. Return a friendly response for unknown commands and exceptions with Home/Retry buttons and a correlation id logged server-side.
12. Add tests that assert command/menu parity, access rules, callback reachability, Back paths, message length, missing-data semantics, timezone labels and no access from Telegram to live execution.

The machine-readable companion inventory is `reports/telegram_ui_inventory.json`. It contains all commands, BotFather exposure, callbacks, keyboards, handlers, message builders, sources, owner-only actions and conflicts used in this audit.
