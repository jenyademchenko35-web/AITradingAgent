"""Explicit research decisions; never used by LIVE execution."""
from __future__ import annotations
REJECTED_CANDIDATES={"MOMENTUM_RELAXED":{"status":"REJECTED","reason":"WALK_FORWARD_UNDERPERFORMANCE","candidate_better_windows":"0/3","profit_factor":0.375643,"net_r":-22.89089,"probability_net_r_above_zero":0.001,"comparison_summary":"WORSE_THAN_BASELINE: PF, Net R, Winrate, Max Drawdown; worse in all 3 walk-forward windows."}}
NEXT_RESEARCH_CANDIDATES=frozenset({"TREND_PULLBACK","CONSERVATIVE"})
def rejected_decision(strategy_id: str) -> dict | None:
    item=REJECTED_CANDIDATES.get(str(strategy_id).upper()); return dict(item) if item else None
