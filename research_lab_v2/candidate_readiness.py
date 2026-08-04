"""Readiness aggregation for evaluate-only candidates; it never changes modes."""
from __future__ import annotations
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from .candidate_policy import NEXT_RESEARCH_CANDIDATES
def build_readiness(decisions: Iterable[Mapping[str,Any]], *, runtime_errors: Iterable[str]=()) -> dict[str,Any]:
    groups=defaultdict(list)
    for row in decisions:
        if str(row.get("candidate_id","")).upper() in NEXT_RESEARCH_CANDIDATES: groups[str(row["candidate_id"]).upper()].append(row)
    errors=[str(x) for x in runtime_errors if x]; items=[]
    for strategy in sorted(NEXT_RESEARCH_CANDIDATES):
        rows=groups[strategy]; passed=[r for r in rows if r.get("condition_active")]
        blockers=Counter(str(r.get("blocked_reason") or r.get("block_reason") or "NONE") for r in rows if not r.get("condition_active"))
        symbols=Counter(str(r.get("symbol") or r.get("feature_snapshot",{}).get("symbol") or "UNKNOWN") for r in rows); regimes=Counter(str(r.get("feature_snapshot",{}).get("market_regime") or "UNKNOWN") for r in rows)
        plans=[r.get("feature_snapshot",{}).get("trade_plan",{}) for r in passed]; valid=bool(plans) and all(isinstance(p,Mapping) and all(p.get(k) not in (None,0,"") for k in ("entry","stop_loss","take_profit","rr")) for p in plans)
        ready=len(rows)>=100 and len(passed)>=20 and not errors and valid
        items.append({"strategy":strategy,"evaluated_signals":len(rows),"passed_signals":len(passed),"pass_rate":round(100*len(passed)/len(rows),2) if rows else 0.0,"blockers":dict(blockers),"symbols":dict(symbols),"regimes":dict(regimes),"runtime_errors":errors,"valid_trade_plans":valid,"readiness":"READY_FOR_SHADOW" if ready else "INSUFFICIENT_DATA"})
    return {"schema_version":"candidate-readiness-v1","generated_at":datetime.now(timezone.utc).isoformat(),"candidates":items,"automatic_shadow_enable":False}
def write_readiness(path: Path, decisions: Iterable[Mapping[str,Any]], *, runtime_errors: Iterable[str]=()) -> dict[str,Any]:
    payload=build_readiness(decisions,runtime_errors=runtime_errors); path.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8"); return payload
