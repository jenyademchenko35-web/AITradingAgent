# Mini App production data audit

## Verified data path

```text
Server Mac runtime artifacts
  → runtime_publisher.py (allowlisted read-only bundle)
  → POST /api/runtime/ingest on Railway
  → atomic runtime_ingest/current.json
  → ReadOnlyRepository projections
  → authenticated GET /api/*
  → Mini App API client and screens
```

The publisher is an observer process. It reads already-generated JSON and does
not participate in decisions, execution, risk, portfolio state, or Research Lab
strategy evaluation.

## Endpoint contract and source policy

| Screen / endpoint | Primary production source | Freshness | Fallback | Availability rule |
| --- | --- | --- | --- | --- |
| Dashboard / `/api/dashboard` | `runtime_snapshot` plus its explicit `portfolio` projection | Snapshot timestamp | Local `trades.csv` only when ingest is absent | Missing canonical portfolio metrics are `null`, never zero |
| System / `/api/system` | canonical runtime snapshot plus allowlisted `system_summary` | Per component | Local JSON only when ingest is absent | Unknown fields stay `UNKNOWN` / `null` |
| Signals / `/api/watchlist` | canonical snapshot signal rows | Each signal timestamp | Canonical/legacy decision-source policy | Rows carry `source`, `freshness`, and `source_freshness` |
| Activity / `/api/activity` | Saved decision rows | Each event timestamp | Existing decision-source policy | Events are normalized to UTC and sorted newest first; stale events are marked |
| Research / `/api/research/live` | `research_lab_v2_status.json` and `research_data_integrity.json` carried by ingest | Runtime and integrity timestamps independently | Read-only local `research.db` only when ingest is absent | Research integrity is separate from server health |
| Shadow / `/api/shadow` | Explicit Research Lab shadow ledger | Ledger timestamp | Read-only local report only when ingest is absent | `NOT_PUBLISHED` differs from an empty published ledger |

## Why values were previously unknown or misleading on Railway

- `Server`, `Telegram`, `News`, `Cycle`, `Next Scan`, and `Uptime` were written
  by Mac runtime JSON files but were not included in the ingest bundle. Railway
  correctly had no local copy to read. The publisher now sends only a
  whitelisted `system_summary` projection.
- Research runtime status was transported, but Research Data Integrity was not.
  Railway therefore could not report the distinction between a healthy server
  and degraded research evidence. `research_integrity` is now transported and
  projected separately.
- Railway must not treat its own missing `trades.csv` as authoritative. When an
  ingest exists, trade metrics may come only from the canonical snapshot's
  explicit `portfolio` data. If it was not published, the API returns `null` / a
  not-published state rather than synthetic zeroes.
- Older activity and signal rows may be real but no longer current. They retain
  their timestamp and receive a stale marker instead of being shown as live.

## Research Integrity DTO

`/api/research/live` exposes a normalized `integrity` object with:

- `integrity_state`, `ledger_closed`, `canonical_outcomes`, `sync_gap`
- `historical_unresolved_joins`, `current_pipeline_unresolved_joins`
- `feature_join_coverage`
- `new_outcomes_since_attribution_fix`, `new_outcomes_fully_joined`,
  `new_outcomes_join_coverage_pct`
- `ranking_allowed`, `walk_forward_allowed`, `promotion_allowed`
- independent `metrics_fresh` and `walk_forward_fresh` metadata

For the current known production evidence, `DATA_DEGRADED` means historical
attribution debt is visible. It does not change an independently healthy
`Server: ONLINE` status. Feature analysis remains insufficient until enough
fully joined post-fix outcomes exist. Ranking may be allowed while walk-forward
and promotion remain blocked; those gates are deliberately independent.

## Freshness semantics

Each freshness object contains `generated_at`, `source_updated_at`,
`age_seconds`, and `status` (`FRESH`, `STALE`, or `UNKNOWN`). The UI does not
infer freshness from a different subsystem. Agent snapshots, market rows, trade
metrics, research runtime, research integrity, news, candidate ranking, and
walk-forward are represented separately.

## Strategy evidence

The current published runtime status can safely state a strategy's configured
mode and whether it is evaluated. Evidence quality, PF, win rate, Net R,
version, confidence, and walk-forward state must be published explicitly by
Research Lab before the Mini App can rank them. A small or absent sample is
shown as insufficient evidence; the client never labels it a best candidate.

## Safe next UI work

The data contract is now suitable for a later visual redesign: cards can use
the normalized DTOs and should display `—`, `UNKNOWN`, or `NOT_PUBLISHED` for
missing fields. No frontend calculation, local fallbacks, or mixing of live and
research trade metrics is permitted.
