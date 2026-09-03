# Backup V2 pre-deploy and legacy-transition contract

Status: reviewed locally on 2026-09-03. Nothing in this document authorizes a
deployment, unit change, production write, or legacy deletion.

## Frozen restore-set policy

`research.db` and `strategy_weights.json` are `REQUIRED_ALWAYS`.

The following are `REQUIRED_IF_EXISTS`: shadow open book, pending-close outbox,
shadow history, H9/H9-V2/H10 boundaries, active-setups primary and last-known-
good backup, trades, setup history, bot configuration, notification dedup and
close-notification state, decision learning, and Research Lab status. Source
logic permits a healthy initial state before these generated files exist. If a
conditional file exists at either edge of the snapshot window it must remain
stable and be copied; otherwise the attempt aborts. Absence is explicit in the
manifest. In particular, `active_setups_v3.json.bak` is not mandatory: the
writer creates it only when replacing an existing valid primary.

Decision/runtime snapshots, live-monitor state/history, signals, agent stats,
candidate readiness, and the ephemeral Research Lab runtime override are
`OPTIONAL`. If present they are still hashed and stability-checked. Secrets are
excluded.

## Cross-file consistency

The stable-window contract is detection plus application recovery, not a claim
of a cross-filesystem transaction. Every selected file is hashed before and
after the SQLite online backup/copy window; the DB path identity and the open
connection's `PRAGMA data_version` must also remain unchanged.

The shadow-close protocol is deliberately crash-recoverable: pending outbox is
written before ledger, open-book removal precedes DB outcome persistence, DB
persistence is idempotent, and pending acknowledgement is last. Therefore the
old-DB/new-outbox and new-DB/old-outbox cuts are replayable. The validator also
rejects an open/pending overlap rather than publishing a transient set.
Boundaries are create-once. Active-setup primary/backup writes are individually
atomic and primary remains authoritative. Runtime projections are optional.

Telegram delivery and its local dedup file cannot be transactional with the
external Telegram side effect. A crash at send/ack can already cause one retry
independently of backup; V2 neither worsens nor claims to eliminate that
external ambiguity.

## Review severity and liveness evidence

After local hardening there are no open CRITICAL or HIGH findings. Resolved
findings include conditional active-setup backup semantics, manifest/sidecar
wrong-type crashes, path traversal, symlinked sources/lock/archive, DB path
replacement detection, kernel no-replace rename on Linux, clock rollback,
duplicate timestamps, interrupted cold publication, and restore-copy TOCTOU.

Two residual MEDIUM limitations are explicit: the same `aitrading` account owns
both source and backups, so SHA-256 manifests are corruption evidence rather
than protection from a compromised account; and Telegram send/ack is an
external-side-effect ambiguity. LOW: stale partial directories require operator
review and cleanup instead of automatic deletion.

Production evidence: current DB is 1.415 GB, WAL mode, 345,864 pages of 4096
bytes. The last V1 service ran for 35.708 seconds. Recent V1 runs are roughly
11-40 seconds. V2 adds a second full validation and hashes about 55 MB of state,
so budget approximately 60-120 seconds for HOT creation. Cold conversion adds
several sequential DB reads/compression passes; budget approximately 60-180
seconds, to be measured during canary. The agent sleeps 300 seconds after each
cycle and backup cadence is six hours. SQLite WAL readers do not require a
long-lived exclusive writer lock; the backup flock coordinates backup writers
only. A 30-minute service timeout leaves substantial margin.

## Legacy strategy: C — validated one-for-one cohort drain

Do not adopt legacy directories and do not synthesize V2 manifests for them.
Keep legacy and V2 in distinct sibling roots: the existing
`/home/aitrading/backups/AITradingAgent` and a future explicitly approved
`/home/aitrading/backups/AITradingAgent-v2`. After each new V2 point is fully
published, fully validated, and any V2 HOT/COLD conversion is complete, a
separate transition action may validate the oldest legacy point with its
existing cold-conversion evidence and remove **at most one** legacy directory.
That action is disabled until canary phase 6 and requires separate approval.

Transition invariant after run `n` (1..20): `legacy=20-n`, `V2=n`, total=20.
V2 itself never counts or deletes legacy. Failure to validate/delete legacy is
a warning and leaves 21 points; it never rolls back the new V2 point.

Production observed on 2026-09-03 differs from the earlier checkpoint: the
active V1 timer has produced another raw backup, so there are 5 HOT and 15 COLD
legacy points, 8.086 GiB total, and 23.72 GiB free. The estimates below use the
observed per-directory sizes, 15.75 MB DB growth per six-hour run, 55 MB V2
non-DB state, and a 4.2% cold DB ratio.

| V2 run | Legacy | V2 HOT | V2 COLD | Total | Post-drain backup GiB | Transient GiB |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 19 | 1 | 0 | 20 | 9.37 | 9.47 |
| 2 | 18 | 2 | 0 | 20 | 10.66 | 10.76 |
| 4 | 16 | 4 | 0 | 20 | 13.30 | 13.40 |
| 8 | 12 | 4 | 4 | 20 | 13.57 | 14.99 |
| 16 | 4 | 4 | 12 | 20 | 12.98 | 15.71 |
| 20 | 0 | 4 | 16 | 20 | 8.35 | 11.19 |

Worst modeled transition footprint is about 15.71 GiB while the new raw point
exists before conversion/drain. With current non-backup disk use this is about
57% root usage, not 80%. The per-run disk guard remains authoritative.

At every completed transition step a newest V2 point exists, total retained
history does not fall below 20, the oldest point advances by one normal
retention interval, and legacy points remain usable by their documented
HOT/COLD restore procedure until removed.

## systemd and rollback design

Use the existing service/timer rather than a parallel timer. Preserve the V1
executable as a versioned rollback file, install V2 separately, then change only
`ExecStart` after canary validation. The reviewed unit must make these explicit:

- `User=aitrading`, `Group=aitrading`;
- `WorkingDirectory=/home/aitrading/AITradingAgent`;
- absolute venv Python, script, source, V2 sibling backup-root and lock arguments;
- `Environment=PATH=/home/aitrading/AITradingAgent/venv/bin:/usr/bin:/bin`;
- `/usr/bin/zstd`, shared `/tmp/aitrading-backup.lock`;
- bounded `TimeoutStartSec` (30 minutes), `Nice=10`, best-effort IO priority 7;
- `PrivateTmp=no`, because V1/V2 must see the same lock.

Rollback first disables/stops only the timer/service scheduling path, inspects
the shared lock, restores V1 `ExecStart`, reloads units, and only then re-enables
the timer. Legacy drain must remain disabled. Existing V2 directories remain
untouched in the sibling root. Source proof shows V1 counts every directory in
its hard-coded legacy root and deletes lexicographically oldest entries beyond
20. The separate V2 root is therefore mandatory: it makes rollback safe without
patching V1 or teaching either retention engine about the other cohort.

## Canary phases

1. Install tooling only; timer unchanged.
2. One manual V2 backup under the shared lock, with legacy drain disabled.
3. Validate it and run `prepare-restore` into disposable staging.
4. Switch the existing service `ExecStart`; do not create another timer.
5. Observe 2-4 scheduled runs, duration, disk guard, manifests and agent health.
6. With separate approval, enable one-for-one validated legacy drain.

Any canary failure stops the timer transition before rollback. No V2 or legacy
restore point is deleted merely because rollback is requested.
