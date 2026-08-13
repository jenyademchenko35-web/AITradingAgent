# Production Reliability v1

This package prepares five independent macOS LaunchAgents for the production
checkout at `/Users/jeynademcenko/AITradingAgentUpdated`. It is a supervision
layer only: it does not change trading, Research Lab, database data, strategy
parameters, or runtime files.

## Architecture

Each long-running process has one launchd label. `RunAtLoad` starts it after
the production user logs in. `KeepAlive.SuccessfulExit=false` restarts an
unexpected failure but does not immediately relaunch an intentional clean exit.
`ThrottleInterval=15` avoids an unbounded crash loop.

| Service | Label | Command |
| --- | --- | --- |
| Agent | `com.aitradingagent.production.agent` | `multi_timeframe_agent_v3.py --loop --interval 300` |
| Telegram | `com.aitradingagent.production.telegram` | `telegram_bot_v4.py` |
| Market | `com.aitradingagent.production.market` | `live_market_monitor.py --interval 3` |
| News | `com.aitradingagent.production.news` | `market_news_observer.py --loop --interval 1800` |
| Runtime publisher | `com.aitradingagent.production.runtime-publisher` | `runtime_publisher.py` |

The templates have absolute Python/script paths, `WorkingDirectory` set to the
production checkout, and separate `logs/launchd_*.log` stdout/stderr files.
They do not use a shell activation, `nohup`, or embedded credentials.

## Environment and secrets

The agent and Telegram entrypoints load `.env` from their working directory;
the runtime publisher explicitly loads `PROJECT_ROOT/.env`. The market and news
entrypoints do not require secrets directly. Keep the ignored production `.env`
only in `/Users/jeynademcenko/AITradingAgentUpdated`; never copy values into a
plist or Git.

## Preflight (read-only)

Run this on the server Mac before any migration:

```bash
cd /Users/jeynademcenko/AITradingAgentUpdated
plutil -lint deploy/macos/com.aitradingagent.production.*.plist.example
test -x /Users/jeynademcenko/AITradingAgentUpdated/venv/bin/python
launchctl print gui/$(id -u)/com.aitradingagent.watchdog
ps -axo pid=,command= | grep '[t]elegram_bot_v4.py'
scripts/production_stack_status.sh
scripts/production_observability.sh
```

The old `com.aitradingagent.watchdog` must be **OFF** before the new Telegram
job is installed. It belongs to the deprecated `~/AITradingAgent` checkout and
must never supervise this stack.

## One-time activation (not performed by this package)

Activation requires a reviewed maintenance window. Do not run it while a manual
Telegram process or the old watchdog can still poll Telegram.

1. Confirm the old watchdog is unloaded and disabled for the login domain.
2. Stop each of the five manually launched target processes by its exact PID;
   never use `pkill python` or an unscoped pattern.
3. Confirm `ps` finds zero `telegram_bot_v4.py` processes.
4. Create the log directory and copy the five reviewed templates to the user
   LaunchAgents directory without editing any secret values:

```bash
cd /Users/jeynademcenko/AITradingAgentUpdated
mkdir -p logs "$HOME/Library/LaunchAgents"
for template in deploy/macos/com.aitradingagent.production.*.plist.example; do
  cp "$template" "$HOME/Library/LaunchAgents/${template##*/}"
  mv "$HOME/Library/LaunchAgents/${template##*/}" \
     "$HOME/Library/LaunchAgents/${template##*/%.example}"
done
```

5. Bootstrap each new plist once, checking Telegram before continuing:

```bash
scripts/production_stack_start.sh
scripts/production_stack_status.sh
ps -axo pid=,command= | grep '[t]elegram_bot_v4.py'
```

Expected result: exactly one Telegram polling process and five running scoped
labels. `production_stack_start.sh` only bootstraps the five exact labels; it
does not copy templates, stop processes, or interact with the old watchdog.

## Management

All scripts use explicit labels, never broad Python process matching:

```bash
scripts/production_stack_status.sh
scripts/production_stack_stop.sh
scripts/production_stack_start.sh
scripts/production_stack_restart.sh
```

`stop` unloads only the five new labels. `restart` uses `launchctl kickstart -k`
only for labels already loaded. It is intentionally not a watchdog.

## Boot verification

After a user-login reboot, run:

```bash
scripts/production_stack_status.sh
```

Verify all five labels are running, Telegram instances equals `1`, old watchdog
is `OFF`, `research.db` is present, and the runtime snapshot freshness advances.
The read-only status script reports `Overall: HEALTHY` only when all of those
conditions hold and `runtime_snapshot.json` is no older than 900 seconds. It
also requires available Git branch and HEAD metadata. A missing, unreadable, or
stale snapshot produces `DEGRADED` with a reason instead of aborting the
read-only report. It does not inspect Research outcome debt, so historical unresolved outcomes do
not make the process-stack result degraded.

## Production observability (read-only)

`scripts/production_observability.sh` is the compact diagnostic companion to
the stack status command. It reads bounded log tails, process snapshots, file
freshness, and the existing read-only Research Evidence Watch projection. It
does not call the network, Telegram, a management script, or any Research Lab
write/reconciliation path. Untracked generated runtime artifacts do not mark
the deployment dirty; only tracked Git changes are reported.

The report separates `Historical debt` from the current attribution epoch.
The known 22 unresolved historical outcomes therefore remain migration debt,
while a new `PARTIAL` or `BROKEN` canonical outcome is reported as
`CURRENT_PIPELINE_REGRESSION`.

The command can be invoked by absolute path: its Research projection changes
only its own subprocess working directory to the production checkout before
importing the existing read-only helper. A projection failure is displayed as
`Research: UNAVAILABLE`, never as an absence of evidence. `Code dirty` is
`UNKNOWN` if Git cannot answer, and that condition degrades the production
report rather than being treated as clean.

Log tails are bounded by `OBSERVABILITY_LOG_TAIL_LINES`: invalid values use the
safe default of 80 lines and values above 500 are capped. Incomplete or invalid
market telemetry is reported as `Performance: OBSERVE`; it is not presented as
normal performance data.

The monitor telemetry source is `live_monitor.log` in the production root,
beside `live_monitor_state.json`; `logs/live_monitor.log` remains a legacy
fallback. The report parses only the newest bounded telemetry line containing
the monitor's `status`, cycle, ticker, history, and cache fields. Decimal
`cycle_ms` values are valid; if the newest line is malformed, the report uses
the newest fully valid event in the bounded tail. Process CPU is explicitly a
snapshot, and does not by itself set the Performance verdict.

The report distinguishes `Code dirty` from `Core runtime tracked changes` and
`Research runtime tracked changes`. Some older deployments still track
generated snapshots, dry-run CSVs, notification state, journals, and logs.
Those documented runtime artifacts are listed separately and do not degrade
the stack; unknown tracked paths remain code/config changes and do degrade the
report. `trades.csv` remains visible as `Trade journal modified`, rather than
being hidden. A changed `walk_forward_windows.csv` emits
`VERIFY_AUTHORIZED_RUN`; it does not start Walk-Forward or mark a current
pipeline regression. All path lists are bounded and never changed.

Research status freshness is printed once with a seconds suffix. Evidence
milestones are supplied by the canonical `evidence_watch_progress` helper:
`0/1`, `1/5`, `5/20`, `20/50`, and `50/100` are informational only and do not
enable any Research Lab action.

Recent error tails are timestamp-aware within a 30-minute window. Explicit
UTC offsets are respected; timestamp-less application log lines use the
server-local timezone unless `OBSERVABILITY_LOG_TIMEZONE` is set to an IANA
timezone. Fresh news
parser warnings produce `News: DEGRADED` but do not by themselves fail a
healthy production stack. Old timestamped messages are excluded from current
findings; entries without a trustworthy timestamp are shown separately as
unverified historical tail data.

## Rollback

1. Run `scripts/production_stack_stop.sh` to unload only the new labels.
2. Leave `com.aitradingagent.watchdog` disabled; do not re-enable it as part of
   rollback.
3. Resume the five manual commands from
   `/Users/jeynademcenko/AITradingAgentUpdated` if required.

No database, runtime file, LaunchAgent unrelated to this stack, or trading
configuration is changed by rollback.
