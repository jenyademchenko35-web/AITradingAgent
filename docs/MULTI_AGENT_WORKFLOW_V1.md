# AITradingAgent Multi-Agent Workflow v1

## Purpose

This workflow separates implementation from verification:

```text
Builder writes → Reviewer verifies → Production Auditor verifies deployment
```

The release gate remains the user. This package does not create an autonomous
loop and does not enable automatic commit, push, deploy, backfill, strategy
optimization, or production mutation.

## Roles

| Role | Config | Default access | Responsibility |
| --- | --- | --- | --- |
| Builder | `aitrading-builder` | workspace write | Implement one scoped task and tests. |
| Reviewer | `aitrading-reviewer` | read-only | Inspect a completed Builder diff for safety and completeness. |
| Production Auditor | `aitrading-production-auditor` | read-only | Verify a deployed release and runtime health. |
| Server Operator | `server-operator` | operational role | Make explicitly authorized server changes. |

The Production Auditor does not duplicate Server Operator. Auditor reports
health after release; Server Operator performs only separately authorized
runtime operations.

## Standard workflow

1. User defines a bounded task and release constraints.
2. Builder identifies affected files, implements, tests, and stops editing.
3. Reviewer inspects the Builder diff read-only.
4. If Reviewer returns `NEEDS_FIX`, Builder makes only the required correction.
5. Reviewer returns `APPROVED` when scope, tests, runtime safety, and research
   safety pass.
6. User explicitly authorizes commit and push.
7. Deployment is a separate, explicitly authorized action.
8. Production Auditor performs a read-only post-deploy audit.

Builder and Reviewer must never edit the same working tree concurrently. v1 uses
the primary task worktree for Builder and a read-only diff review for Reviewer.
An isolated reviewer worktree is an optional later enhancement, not a v1
requirement.

## Hard safety rules

`Data integrity before strategy optimization.`

Until a user explicitly authorizes it, no role may:

- change strategy logic, feature thresholds, filters, risk, ranking thresholds,
  promotion, or LIVE_BASELINE;
- modify DecisionEngine, execution, RiskManager, PortfolioManager, or Research
  Lab logic;
- enable live execution;
- run manual Walk-Forward, promotion, or historical backfill;
- modify production DB/runtime data;
- reveal or copy secrets from `.env`.

Historical unresolved outcomes (baseline: 22) are migration debt. They must not
be retroactively attributed. New partial/broken outcomes after the attribution
fix are a regression and must be reported, not repaired automatically.

## Task templates

### Feature task

```text
Builder:
Implement X without touching Y. Add focused tests; do not commit/push.

Reviewer:
Review Builder changes for runtime, data-integrity, and research safety.

Production Auditor:
After authorized deployment, verify the release, five services, Telegram=1,
runtime freshness, and relevant health endpoints.
```

### Research/instrumentation task

```text
Builder:
Implement instrumentation or data-integrity only. Do not alter strategies,
thresholds, promotion, Walk-Forward, backfill, or LIVE behavior.

Reviewer:
Verify attribution IDs, timestamps, source provenance, idempotency, and that
historical migration debt remains separate from current evidence.

Production Auditor:
Run a read-only trace after deployment; report new fully joined versus
partial/broken outcomes.
```

### UI task

```text
Builder:
Change only frontend or Telegram UI. Preserve API contracts and avoid runtime
side effects.

Reviewer:
Verify API compatibility, authorization policy, command/callback compatibility,
and absence of runtime or trading side effects.

Production Auditor:
After deployment, verify UI-serving health and Telegram single-instance state.
```

## Reports

Each role emits its required structured report from its configuration file. A
report must distinguish confirmed evidence from unknown production facts.
