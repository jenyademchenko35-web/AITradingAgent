import { expect, test } from "vitest";
import { formatResearchCandidate, formatResearchStatus } from "./research";

test("formats runtime and candidate objects without leaking object text", () => {
  const report = { runtime_status: { name: "Research Lab", state: "RUNNING", enabled: true, recommendation: "OBSERVE" }, best_candidate: { strategy_id: "TREND_CONFIRM", profit_factor: 1.42, winrate: 58.3, closed_trades: 121, net_r: 14.2, status: "RESEARCH" } };
  expect(formatResearchStatus(report)).toEqual({ name: "Research Lab", state: "RUNNING", enabled: "ON", recommendation: "OBSERVE" });
  expect(formatResearchCandidate(report)).toEqual({ strategyName: "TREND_CONFIRM", profitFactor: "1.42", winrate: "58.3", closedTrades: "121", netR: "14.2", status: "RESEARCH" });
});

test("uses a dash for missing or nested values", () => {
  expect(formatResearchStatus({ runtime_status: {} })).toEqual({ name: "—", state: "—", enabled: "—", recommendation: "—" });
  expect(formatResearchCandidate({ best_candidate: { strategy_id: { nested: true }, profit_factor: null } })).toEqual({ strategyName: "—", profitFactor: "—", winrate: "—", closedTrades: "—", netR: "—", status: "—" });
});
