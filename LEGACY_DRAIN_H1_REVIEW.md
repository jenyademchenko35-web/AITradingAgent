# H1 legacy drain hardening — local security/reliability review

Date: 2026-09-05. Base HEAD: `706e84c09f44fe6c9a0c7e4aae06b72195758594`.

## Executive summary

H1 hardened locally; no commit, push, SSH, deployment or production mutation.
The final targeted suite passed 146 tests. The full safe suite passed 1123 tests
and 4 subtests, with 8 existing dependency/datetime deprecation warnings.

Verdict: `LEGACY_DRAIN_H1_HARDENING_SAFE_FOR_DEPLOY` within the explicit trust
boundary below. This is readiness for a separately authorized controlled deploy,
not authorization to enable drain. The new `legacy_drain_safety.py` module must
be deployed together with `legacy_drain.py`.

Review was a separate post-implementation pass by the same agent, not a second
human/agent attestation. No dedicated Python filesystem/CLI reference exists in
the security-best-practices skill; its general fail-closed review guidance was
used. Other audit findings, including notification H2, were out of scope.

## Threat boundary

The mutable transition JSON is UNTRUSTED. It cannot choose arbitrary paths,
redirect an existing intent to a second candidate, forge terminal completion,
or authorize partial deletion merely by changing state. Root paths come only
from explicit caller configuration. Existing valid schema-1 terminal records
are historical data only; schema-1 pending records block recovery.

Independent write-once intent, delete-permit and terminal receipts live in
`.legacy-drain-transitions/.intents`, outside VERIFIED point contents. They are
0400 files in a private directory; group/other-writable receipt/journal parents,
files, symlinks and hardlinks are rejected. Receipt immutability is enforced by
the application and atomic no-replace publication, not by a cryptographic or
OS privilege boundary. Root or an attacker with the backup UID can alter both
receipts and data. Protect the configured roots and exclude uncooperative
writers; the caller must hold the existing shared backup lock throughout.

## Changes reviewed

- `legacy_drain_safety.py:25`: each directory component opened using
  O_NOFOLLOW/O_DIRECTORY; traversal and symlink ancestors fail closed.
- `legacy_drain.py:473,512`: exact configured roots, trigger path/name, candidate
  timestamp, deterministic tombstone name, parent and evidence comparisons.
  Basename construction uses ASCII timestamp fullmatch and calendar validation.
- `legacy_drain_safety.py:74`: dirfd-relative no-clobber rename using Linux
  renameat2(RENAME_NOREPLACE) or Darwin renameatx_np(RENAME_EXCL). No unsafe
  exists-then-rename fallback. Unsupported primitive/filesystem => SKIPPED.
- `legacy_drain.py:205,551`: lsof rechecked before rename, after rename/before
  physical deletion, and during recovery. Missing lsof, diagnostic stderr,
  command failure, timeout, ambiguous success, or open FDs => no deletion.
- `legacy_drain_safety.py:171,221`: sealed inventory includes relative paths,
  device/inode, kind, uid/gid and regular-file size/mtime/ctime/SHA256. Recursive
  deletion uses pinned parent/child descriptors, not pathname shutil.rmtree.
  Device changes, symlinks, hardlinks, special files, replacements, extra files
  and changed remaining contents fail closed. Anchors are rechecked during
  traversal. Root inode alone is never sufficient partial-recovery evidence.
- `legacy_drain.py:425`: explicit state/action/transition tables. Normal path:
  STARTED -> VALIDATED -> TOMBSTONED -> DELETE_STARTED -> COMPLETED.
  Recovery of rename-before-journal verifies the immutable full inventory before
  promoting VALIDATED to TOMBSTONED. Partial deletion requires an independent
  durable delete permit and unchanged remaining file identities.
- `legacy_drain.py:570`: a new trigger is durably marked RECOVERY_CONSUMED_RUN
  BEFORE recovering another trigger's candidate. A crash cannot assign a second
  candidate to that same trigger. Unreconciled tombstone residue blocks fresh
  candidate selection, including attempts to hide it with downgraded history.
- `legacy_drain_safety.py:108,128`: bounded strict JSON reader rejects duplicate
  keys and non-object documents. Publication uses a unique exclusive temporary,
  file fsync, atomic replace (or no-replace link for receipts), directory fsync,
  and temporary cleanup. A failed publication never makes partial JSON valid.
- Recovery failure returns `action=SKIPPED`, `state=RECOVERY_BLOCKED`, reason
  `RECOVERY_FAIL_CLOSED: ...`. No malformed journal is silently repaired.
  Backup success and previously VERIFIED manifests remain independent.

## Review findings remaining in H1 scope

### M1 — MEDIUM: advisory exclusion is not a mandatory filesystem lock

`legacy_drain.py:551`, `legacy_drain_safety.py:221`.
POSIX provides neither an atomic unlink-if-inode operation nor an atomic
lsof-check-and-delete primitive. A hostile same-UID writer can still act in the
last interval between checks and syscalls, or open a file after lsof. Descriptor
traversal prevents following replacement symlinks/directories; it does not
claim protection against an actor able to rewrite the trusted roots themselves.
Mitigation/operating requirement: private receipt directories, protected roots,
one lock-owning backup writer and no uncooperative writers. A stronger threat
model requires separate-UID/root-owned quarantine or another OS-enforced access
boundary, which is outside this local-only change and would require deployment
design/approval. This limitation is not reported as a mandatory-lock guarantee.

### L1 — LOW: conservative crash states require explicit reconciliation

`legacy_drain.py:473,570`, `legacy_drain_safety.py:207`.
An empty partial tombstone has no remaining file-identity witness against inode
reuse. A crash between terminal-receipt publication and journal replacement also
leaves inconsistent durable states. Both block automatic recovery; no later
candidate is deleted through that recovery. Old pending schema-1 records and an
orphan validated intent likewise require explicit review. This sacrifices
availability rather than weakening deletion evidence. No automatic migration
or cleanup of these states is included.

H1 counts: OPEN_CRITICAL=0, OPEN_HIGH=0, OPEN_MEDIUM=1, OPEN_LOW=1.

## Validation

Environment: local macOS, Python 3.12.14; local zstd and lsof. No production SSH.
The Darwin atomic rename path was executed. The Linux renameat2 path was source
reviewed, not executed on Linux in this task. A separately authorized Linux
preflight must run targeted tests before drain enablement; absence/failure of
the primitive fails closed.

- Existing legacy drain suite: 34 PASS, unchanged tests.
- New H1 regression suite: 68 PASS.
- Total legacy drain tests: 102 PASS.
- Backup V2 suite: 44 PASS, unchanged tests/code.
- Targeted aggregate: 146 PASS.
- Full safe suite: 1123 PASS + 4 subtests; 8 warnings; 63.75 seconds.
- git diff --check: PASS.

Full suite ran from `/private/tmp/ait-h1-final.BQgEUh`, constructed from tracked
HEAD plus only the changed Python modules/tests. An external pytest-only plugin
blocked non-loopback Python socket/DNS access; token environment values were
empty. Existing tests' subprocess fixtures use local fake dependencies. Test
backups/DBs are disposable fixtures, never the production or source project DB.

### Required regression coverage

1–3,8: outside candidate/tombstone, traversal and similar-prefix paths.
4–7: symlink candidate/tombstone, strict malformed/calendar/Unicode basenames.
9–12: candidate/tombstone inode/device mismatch and physical replacement.
13–16: FD before/after rename/recovery, real held FD, lsof error/missing policy.
17–18: broken/duplicate-key JSON and invalid transitions/state-action pairs.
19–20,24–25: all crash windows for both HOT and COLD, including rename before
journal update and after TOMBSTONED publication.
21–22: fully retargeted journal, forged terminal state, schema downgrade,
recovery trigger consumption before crash, original one-per-run tests.
23,26: VERIFIED V2 independence on actual integration failure, outside sentinel
preservation, descriptor traversal rejecting a raced nested symlink.
Additional cases: immutable no-overwrite receipt, orphan/partial safeguards,
empty partial witness refusal, hardlinks, atomic journal publication failure.

## Unchanged / handoff

PRODUCTION_CHANGED=NO; SYSTEMD_CHANGED=NO; LEGACY_BACKUPS_CHANGED=NO;
TRADING_CHANGED=NO; RESEARCH_CHANGED=NO; DB_SCHEMA_CHANGED=NO.
No commits, pushes, deployments or scheduler changes. Production drain OFF and
the supplied counts are user-provided context, not re-verified in this task.

Changed files: legacy_drain.py, legacy_drain_safety.py,
tests/test_legacy_drain_h1.py, LEGACY_DRAIN_H1_REVIEW.md.
