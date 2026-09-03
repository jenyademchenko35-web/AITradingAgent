# AITradingAgent Backup System V2.1

Status: local design and synthetic implementation only. It is not installed,
scheduled or connected to production.

## Restore-set classification

### CORE_RECOVERY

The backup fails closed if either file is missing or is not regular:

- `research.db`
- `strategy_weights.json`

### CONDITIONAL_CORE

Source logic permits a healthy initial state before these generated files have
been written. If present, their inclusion and stability are mandatory; absence
is recorded explicitly in the manifest:

- `research_lab_v2_shadow_open.json`
- `research_lab_v2_pending_closes.json`
- `research_lab_shadow_history.csv`
- `research_lab_v2_h9_boundary.json`
- `research_lab_v2_h9_v2_boundary.json`
- `research_lab_v2_h10_boundary.json`
- `active_setups_v3.json` and `active_setups_v3.json.bak`
- `trades.csv` and `setup_history_v3.csv`
- `bot_config.json` and `last_notification.json`
- `trade_close_notifications.json`
- `decision_learning.json`
- `research_lab_v2_status.json`

The active-setup backup is last-known-good recovery input, but it legitimately
does not exist until a valid primary is replaced for the first time. Once
present, it must be retained. Boundaries are immutable experiment evidence once
created and cannot be regenerated with the same forensic meaning.

### VOLATILE_OPTIONAL

These are rolling/rebuildable operational projections, not canonical research
attribution or notification dedup evidence:
`decision_snapshot.json`, `runtime_snapshot.json`, `live_monitor_state.json`,
`live_price_history.csv`, `signals_v3.csv`, `agent_v3_stats.json`,
`candidate_readiness.json`, and `research_lab_v2_runtime_override.json`.
Each is captured in its own bounded stat/copy/hash/stat retry. A stable copy is
`CAPTURED`; a missing source is `ABSENT`; a continuously changing or unsafe
source is removed from staging and recorded as `SKIPPED_UNSTABLE`. None extends
the core stable window or aborts an otherwise valid core restore point.

### Diagnostic

Large derived decision telemetry and rotating logs are named in the manifest as
excluded diagnostics. They are not needed to continue the agent and are not in
the default restore set: `decision_debug.csv`, `decision_diagnostics.csv`,
`decision_explanations.csv`, `decision_features.csv`, and Research Lab logs.
Their separate research-retention policy must not be confused with operational
restore.

`.env`, private keys and credentials are never selected. Secret escrow is a
separate, explicitly-authorized concern.

## Consistency contract

V2.1 does not claim that unrelated files form a filesystem transaction. It
finds a stable application window for CORE_RECOVERY and CONDITIONAL_CORE only:

1. hash and stat every core/conditional state file;
2. keep one read-only SQLite connection open and record `PRAGMA data_version`;
3. create the DB with SQLite's online backup API;
4. copy the core/conditional state files;
5. hash/stat the sources again and reread `data_version`;
6. accept only an unchanged core file set and unchanged SQLite data version;
7. after that gate, capture each volatile projection independently.

The whole attempt is retried a bounded number of times. Exhaustion leaves an
unpublished partial directory and preserves every older verified point.

For every state path the manifest records `consistency_class` and
`capture_status`. Captured records include size, SHA-256, mtime, and source
device/inode/mode/size/mtime identity. Skipped records include reason and retry
count. Validation checks JSON types, requires disjoint
open and pending-close trade IDs, records the ledger header, and compares the
semantic summary to the manifest. This detects a changed or substituted member;
it does not pretend to supply cross-store transactional semantics that the live
application itself does not have.

## Atomic publication

The ordered write path is:

```text
shared flock
  -> disk guard
  -> .<timestamp>.<pid>.partial (0700)
  -> consistent SQLite online backup
  -> SQLite quick_check + integrity_check
  -> stable state copies
  -> SHA-256 manifest.v2.json (VERIFIED)
  -> validate complete staging set
  -> fsync every file and staging directory
  -> atomic same-filesystem directory rename
  -> fsync backup root
```

Only a non-hidden `YYYYMMDD_HHMMSS` directory containing a valid V2 manifest is
a published restore point. Partial, legacy and unknown directories are ignored
by listing and retention. Stale partials remain visible for an operator; V2 does
not silently delete them.

## HOT/COLD and retention

- total verified restore points: 20;
- newest four: HOT with raw `research.db`;
- older sixteen: COLD with `research.db.zst`, zstd level 3, one thread;
- every other member remains unchanged.

Cold publication is raw -> exclusive `.zst.partial` -> `zstd -t` -> streaming
decompressed hash/size -> archive fsync -> atomic publication -> fsynced
`cold_conversion.json` -> full second validation -> raw deletion -> directory
fsync. A COLD validator compares the decompressed bytes to the original DB entry
in the immutable V2 manifest.

Policy order is publish the new HOT point, rebalance HOT/COLD, validate all V2
points, and only then remove verified points beyond 20. A retention deletion
failure is a warning: the new verified point is retained and is not rolled back.

The same `/tmp/aitrading-backup.lock` and non-blocking exclusive `flock` protocol
is used by V1. If the timer fires while V2 owns it, the existing V1 job exits
without running; there is no concurrent writer. V2 never starts a replacement
backup manually.

## Disk guard

Before staging exists, the guard requires space for:

```text
new raw DB
+ all selected state files
+ worst-case cold candidate (102% of raw + 1 MiB)
+ configured safety reserve (default 2 GiB)
```

It never deletes an existing verified point to satisfy the guard. Unknown and
partial directories consume real free space naturally through the filesystem's
free-space measurement.

Using the production measurements from 2026-09-03:

- observed legacy footprint after a subsequent V1 run: 8.086 GiB;
- added V2 continuation state is about 17-18 MB per point at current sizes;
- expected V2 steady-state after the 20-run transition: approximately 8.35 GiB;
- expected incremental backup peak: approximately 4.7 GiB including the 2 GiB
  reserve;
- current free space after one-time conversion: approximately 24.98 GiB;
- expected DB-driven backup growth: approximately 0.30 GB/day, or about
  0.36 GB/day including the live DB, before unrelated logs/data growth.

The count is bounded, but DB size is not. The guard therefore remains mandatory
on every invocation; these trend numbers are planning estimates, not a quota.

## Failure behavior

| Failure | Deterministic result |
|---|---|
| lock contention | exit before staging |
| disk guard | exit before large writes or retention |
| missing/changed core/conditional file | retry stable window, then fail with partial |
| changing volatile projection | bounded individual retry, then warning and verified core point |
| DB backup/integrity failure | no publication, old points retained |
| manifest/rename failure | partial remains; no retention |
| zstd/hash failure | raw remains; partial/final is not a replacement |
| kill/power loss before directory publication | hidden partial is ignored |
| kill during cold conversion | original manifest and raw DB remain authoritative |
| oldest deletion failure | warning; new verified point remains |

Filesystem fsync/rename protects software and ordinary crash boundaries. It
cannot replace an independent disk/off-host copy for hardware loss.

## Validation and restore helper

The inert CLI supports:

```text
python backup_v2.py list --backup-root <root>
python backup_v2.py validate <restore-point>
python backup_v2.py prepare-restore <restore-point> <new-staging-directory>
```

`prepare-restore` requires a new directory, validates all captured hashes and semantic
state, runs `zstd -t` for COLD, materializes the DB, verifies its original
SHA-256/size, runs full SQLite checks, and writes `RESTORE_READY.json` with
`RESTORE_READY=true`, `production_installed=false`, and explicit
`VOLATILE_OPTIONAL_MISSING` / `VOLATILE_OPTIONAL_SKIPPED` warnings. It has no
command that replaces production files.
Actual installation requires a separate procedure, explicit authority and an
application-level quiescence decision.

## Deployment preconditions

This implementation must not be deployed as-is merely because local tests pass.
Before review for deployment:

1. install the module/wrapper in an approved production tooling path;
2. use the separately reviewed sibling-root, one-for-one cohort-drain strategy;
   V2 intentionally does not adopt legacy points automatically;
3. verify and record whether `active_setups_v3.json.bak` exists; its absence is
   a valid conditional state rather than a preflight failure;
4. test disk guard and one disposable restore point on a non-production clone;
5. review the service environment PATH for `zstd`;
6. only then prepare a separate systemd/timer change for explicit approval.

No trading process restart is part of backup creation or policy maintenance.
