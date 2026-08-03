export type ResearchRecord = Record<string, unknown>;

export interface ResearchStatusView { name: string; state: string; enabled: string; recommendation: string; }
export interface ResearchCandidateView { strategyName: string; profitFactor: string; winrate: string; closedTrades: string; netR: string; status: string; }

function record(value: unknown): ResearchRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as ResearchRecord : {};
}

function field(source: ResearchRecord, ...keys: string[]): unknown {
  for (const key of keys) if (Object.hasOwn(source, key) && source[key] !== null && source[key] !== undefined && source[key] !== "") return source[key];
  return null;
}

export function researchText(value: unknown): string {
  return ["string", "number", "boolean"].includes(typeof value) ? String(value) : "—";
}

export function formatResearchStatus(report: ResearchRecord): ResearchStatusView {
  const runtime = record(field(report, "runtime_status", "status"));
  const enabled = field(runtime, "enabled") ?? field(report, "enabled");
  return {
    name: researchText(field(runtime, "name", "status") ?? field(report, "status")),
    state: researchText(field(runtime, "state", "database_status") ?? field(report, "state")),
    enabled: enabled === null ? "—" : enabled === true ? "ON" : enabled === false ? "OFF" : researchText(enabled),
    recommendation: researchText(field(runtime, "recommendation") ?? field(report, "recommendation")),
  };
}

export function formatResearchCandidate(report: ResearchRecord): ResearchCandidateView {
  const candidate = record(field(report, "best_candidate", "candidate"));
  return {
    strategyName: researchText(field(candidate, "name", "strategy_name", "strategy_id", "id")),
    profitFactor: researchText(field(candidate, "profit_factor", "pf")),
    winrate: researchText(field(candidate, "winrate")),
    closedTrades: researchText(field(candidate, "closed_trades")),
    netR: researchText(field(candidate, "net_r")),
    status: researchText(field(candidate, "status")),
  };
}
