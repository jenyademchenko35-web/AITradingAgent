# OOS Research Automation v1

This package runs the already-reviewed FX OOS collector and unified Crypto +
FX checkpoint once per day. It does not enable FX, call trading execution,
change a frozen cutoff or hypothesis, or manage any production process.

## Flow and failure policy

`research_oos_daily` obtains a non-blocking file lock, calls
`fx_research.oos_data_collector.collect`, and calls
`research_oos_checkpoint.build_checkpoint` only after collection succeeds and
its returned integrity state is safe. Collector, integrity, and checkpoint
failures are written as compact reports and return non-zero. A collector
failure never runs the checkpoint. A second overlapping invocation exits with
`ALREADY_RUNNING` and does not overwrite the active run's report.

The collector remains the only code allowed to publish canonical FX CSVs. It
retains its transactional two-symbol publication and fail-closed conversion.
The checkpoint resolves Crypto cutoff `CRYPTO-OOS-V1` internally and reuses the
immutable FX frozen hypotheses.

## Reports, logs, and retention

The latest compact result is `reports/oos/latest.json`. Timestamped results are
stored under `reports/oos/history/`; the default retention is 30 reports and
the CLI hard-caps it at 365. Writes use strict JSON and atomic replacement.
Internal collector buffers, canonical rows, downloaded payloads, and raw
subprocess output are never copied into these reports.

The dedicated rotating log is `logs/oos_research_daily.log`, capped at 512 KiB
with three backups. Error text is whitespace-normalized and bounded to 500
characters.

## Deterministic NVM path

The user service does not start an interactive shell and does not source NVM.
It receives the already-installed absolute `npx` path through the private
environment file `%h/.config/aitradingagent/oos-research.env`. No Node package
is installed by this package. The same file fixes `PATH` to the resolved NVM
bin directory so the `#!/usr/bin/env node` launcher works under systemd. The
orchestrator uses `npx --no-install dukascopy-node`; a missing local package
therefore fails closed.

## Staged manual rollout (not run automatically)

### Preflight — update, deterministic NVM discovery, and dry-run

Run as the `aitrading` user only after the automation commit has been reviewed,
merged, and pushed. A clean non-interactive shell may not have NVM on `PATH`,
so load the existing NVM environment before discovery. This does not install
or update Node packages.

```bash
cd /home/aitrading/AITradingAgent
git pull --ff-only origin telegram-ui-v2-miniapp
test -f research_oos_daily.py
venv/bin/python -m research_oos_daily --help >/dev/null

export NVM_DIR="$HOME/.nvm"
test -s "$NVM_DIR/nvm.sh"
. "$NVM_DIR/nvm.sh"
OOS_NPX_PATH="$(command -v npx)"
test -n "$OOS_NPX_PATH"
case "$OOS_NPX_PATH" in /*) ;; *) echo "npx path is not absolute" >&2; exit 1;; esac
test -x "$OOS_NPX_PATH"
OOS_NODE_BIN="$(dirname "$OOS_NPX_PATH")"
test -x "$OOS_NODE_BIN/node"
"$OOS_NPX_PATH" --no-install dukascopy-node --help >/dev/null

venv/bin/python -m research_oos_daily \
  --dukascopy-npx "$OOS_NPX_PATH" \
  --dry-run
/home/aitrading/AITradingAgent/venv/bin/python -m json.tool \
  /home/aitrading/AITradingAgent/reports/oos/latest.json
tail -n 100 /home/aitrading/AITradingAgent/logs/oos_research_daily.log
```

The `--no-install` probe verifies the already-installed local
`dukascopy-node` package while explicitly prohibiting `npx` from installing
it. A missing module, NVM installation, `npx`, Node executable, local package,
or failed dry-run stops the rollout. Do not install systemd files after a
failed preflight. Never replace discovery with a directory scan or an unverified
version-specific path.

### Stage A — install inert units; do not enable or start the timer

Continue in the same reviewed shell, preserving the verified
`OOS_NPX_PATH`/`OOS_NODE_BIN` values:

```bash
mkdir -p "$HOME/.config/aitradingagent" "$HOME/.config/systemd/user"
printf 'OOS_NPX=%s\nPATH=%s:/usr/local/bin:/usr/bin:/bin\n' "$OOS_NPX_PATH" "$OOS_NODE_BIN" > "$HOME/.config/aitradingagent/oos-research.env"
chmod 600 "$HOME/.config/aitradingagent/oos-research.env"
install -m 0644 deploy/systemd/aitrading-oos-research.service.example "$HOME/.config/systemd/user/aitrading-oos-research.service"
install -m 0644 deploy/systemd/aitrading-oos-research.timer.example "$HOME/.config/systemd/user/aitrading-oos-research.timer"
systemctl --user daemon-reload
```

Stage A deliberately does **not** start the service and does **not** enable or
start the timer.

### Stage B — manually run one service invocation and inspect it

```bash
systemctl --user start aitrading-oos-research.service
systemctl --user status aitrading-oos-research.service --no-pager
journalctl --user -u aitrading-oos-research.service -n 100 --no-pager
/home/aitrading/AITradingAgent/venv/bin/python -m json.tool /home/aitrading/AITradingAgent/reports/oos/latest.json
tail -n 100 /home/aitrading/AITradingAgent/logs/oos_research_daily.log
```

Review collection integrity, the unified checkpoint, `reports/oos/latest.json`,
and the bounded logs before proceeding. A failed service remains fail-closed
and the timer must stay disabled until the cause is reviewed.

### Stage C — inspect persistence, then request activation approval

```bash
systemctl --user cat aitrading-oos-research.timer
systemctl --user is-enabled aitrading-oos-research.timer
loginctl show-user aitrading -p Linger
```

The expected pre-activation timer state is `disabled`. Its proposed schedule is
daily at **06:20 UTC** with `Persistent=true`. If `Linger=no`, the user manager
and timer may not remain active while `aitrading` has no login session. Decide
how to handle that operational constraint before requesting timer activation.

Timer activation is a separate operator-approved action. Only after the Stage B
manual invocation passes, Stage C is reviewed, and explicit approval is given may the operator
run:

```bash
systemctl --user enable --now aitrading-oos-research.timer
systemctl --user list-timers aitrading-oos-research.timer
```

## User-systemd persistence check

Check, but do not change, the user-manager persistence policy:

```bash
loginctl show-user aitrading -p Linger
```

Enabling linger is a separate administrator-approved operational action; it is
not performed by installation or timer activation. Only after explicit later
approval, an administrator may run:

```bash
sudo loginctl enable-linger aitrading
```

## Standalone manual one-shot dry run

This downloads only the bounded incremental window into temporary staging,
does not publish canonical datasets, and does not install or enable anything:

```bash
cd /home/aitrading/AITradingAgent
export NVM_DIR="$HOME/.nvm"
test -s "$NVM_DIR/nvm.sh"
. "$NVM_DIR/nvm.sh"
OOS_NPX_PATH="$(command -v npx)"
test -n "$OOS_NPX_PATH"
case "$OOS_NPX_PATH" in /*) ;; *) echo "npx path is not absolute" >&2; exit 1;; esac
test -x "$OOS_NPX_PATH"
OOS_NODE_BIN="$(dirname "$OOS_NPX_PATH")"
test -x "$OOS_NODE_BIN/node"
"$OOS_NPX_PATH" --no-install dukascopy-node --help >/dev/null
venv/bin/python -m research_oos_daily \
  --dukascopy-npx "$OOS_NPX_PATH" \
  --dry-run
```

This command is also the Stage A dry-run. Do not remove `--dry-run` during
initial deployment validation. The first non-dry-run execution is the reviewed
Stage B service invocation.
