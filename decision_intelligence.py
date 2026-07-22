"""Decision Intelligence v3: read-only post-trade module attribution."""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

BASE_DIR = Path(__file__).resolve().parent
MODULES = ("trend", "structure", "momentum", "risk")


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _module_verdict(net: float, won: bool) -> str:
    if abs(net) < 1e-12: return "NEUTRAL"
    return "PASS" if (net > 0) == won else "FAIL"


def build_report(*, base_dir: str | Path = BASE_DIR) -> dict[str, Any]:
    root = Path(base_dir); source = _read(root / "reports/decision_engine_v2.json")
    comparisons = source.get("comparisons", [])
    trades=[]; stats={name: Counter() for name in MODULES}; roots=Counter(); directions=defaultdict(Counter)
    for row in comparisons if isinstance(comparisons, list) else []:
        result = "WIN" if float(row.get("R", 0) or 0) > 0 else "LOSS"; won=result == "WIN"
        modules={}; positive=[]
        contribution=row.get("feature_contribution", {})
        for item in contribution.get("modules", []) if isinstance(contribution, dict) else []:
            name=str(item.get("feature", "")).lower()
            if name not in MODULES: continue
            net=float(item.get("net_directional", 0) or 0); verdict=_module_verdict(net, won)
            modules[name]={"status":verdict,"net_directional":net,"weight":item.get("weight")}
            if verdict != "NEUTRAL": stats[name]["evaluated"] += 1; stats[name]["correct" if verdict == "PASS" else "errors"] += 1
            if not won and net > 0: positive.append((net,name))
        root_cause=max(positive)[1] if positive else ("UNATTRIBUTED" if not won else None)
        if root_cause: roots[root_cause] += 1
        direction=str(row.get("direction","UNKNOWN")).upper(); directions[direction]["total"] += 1; directions[direction]["wins" if won else "losses"] += 1
        trades.append({"trade_id":row.get("trade_id"),"symbol":row.get("symbol"),"direction":direction,
                       "result":result,"R":row.get("R"),"decision":"Correct" if won else "Wrong",
                       "modules":modules,"root_cause":root_cause,
                       "root_cause_method":"largest positive directional contribution on LOSS",
                       "market_regime":row.get("market_regime","UNKNOWN")})
    module_accuracy=[]
    for name in MODULES:
        evaluated=stats[name]["evaluated"]; correct=stats[name]["correct"]
        module_accuracy.append({"module":name.title(),"accuracy_pct":round(100*correct/evaluated,2) if evaluated else None,
                                "correct":correct,"errors":stats[name]["errors"],"evaluated":evaluated})
    losses=sum(1 for row in trades if row["result"] == "LOSS")
    root_causes=[{"module":name.title(),"errors":count,"share_of_losses_pct":round(100*count/losses,2) if losses else 0,
                  "repeat_probability":"HIGH" if losses and count/losses >= .3 else "MEDIUM" if losses and count/losses >= .15 else "LOW"}
                 for name,count in roots.most_common()]
    direction_rows={name:{"trades":c["total"],"wins":c["wins"],"losses":c["losses"],
                          "accuracy_pct":round(100*c["wins"]/c["total"],2) if c["total"] else None}
                    for name,c in directions.items()}
    optimizer=source.get("calibration_pass",{}).get("optimizer",{})
    recommendations=[]; top=root_causes[0]["module"].lower() if root_causes else None
    for item in optimizer.get("recommendations",[]) if isinstance(optimizer,dict) else []:
        if item.get("feature") == top:
            current=float(item.get("current",0)); suggested=float(item.get("suggested",0))
            recommendations.append({"module":top.title(),"action":"Reduce" if suggested < current else "Increase",
                                    "current_weight":current,"suggested_weight":suggested,
                                    "expected_profit_factor":item.get("expected_profit_factor",{}),
                                    "confidence":item.get("confidence","LOW"),"automatic_apply":False,
                                    "basis":"v2.1 replay sensitivity candidate; advisory only"})
    total=len(trades); wins=sum(1 for row in trades if row["result"] == "WIN")
    return {"schema_version":1,"generated_at":datetime.now(timezone.utc).isoformat(),"version":"3.0",
            "mode":"SHADOW_RESEARCH_ONLY","sample":{"complete_decisions":total,"wins":wins,"losses":total-wins},
            "decision_accuracy_pct":round(100*wins/total,2) if total else None,
            "direction_accuracy_pct":round(100*wins/total,2) if total else None,
            "direction_intelligence":direction_rows,"module_accuracy":module_accuracy,"root_causes":root_causes,
            "recommendations":recommendations,"trades":trades,
            "methodology":{"module_pass":"directional contribution agrees with realized outcome",
                           "root_cause":"largest module contribution supporting a losing direction",
                           "limitations":"Attribution is observational, not proof of causality."},
            "restrictions":{"live_unchanged":True,"decision_engine_unchanged":True,"weights_applied":False}}


def run(*, base_dir: str | Path = BASE_DIR) -> dict[str, Any]:
    report=build_report(base_dir=base_dir); _write(Path(base_dir)/"decision_learning.json",report); return report


def format_telegram(report: Mapping[str,Any], view: str="learning") -> str:
    if view == "modules":
        return "\n".join(["🧩 Module Accuracy"]+[f"{x['module']}: {x['accuracy_pct'] if x['accuracy_pct'] is not None else 'N/A'}%" for x in report.get("module_accuracy",[])])
    if view == "rootcause":
        return "\n".join(["🧠 Top Errors"]+[f"{x['module']}: {x['errors']} ({x['repeat_probability']})" for x in report.get("root_causes",[])])
    if view == "accuracy":
        d=report.get("direction_intelligence",{}); return "\n".join(["🎯 Decision Accuracy",f"Overall: {report.get('decision_accuracy_pct','N/A')}%",f"Direction: {report.get('direction_accuracy_pct','N/A')}%",f"Long: {d.get('LONG',{}).get('accuracy_pct','N/A')}%",f"Short: {d.get('SHORT',{}).get('accuracy_pct','N/A')}%"])
    recs=report.get("recommendations",[])
    if not recs: return "🧠 Decision Learning\nNo evidence-backed weight recommendation."
    rec=recs[0]; pf=rec.get("expected_profit_factor",{})
    return "\n".join(["🧠 Decision Learning",f"Top Recommendation: {rec['action']} {rec['module']} Weight",
                       f"Weight: {rec['current_weight']:.3f} → {rec['suggested_weight']:.3f}",
                       f"Expected PF: {pf.get('current','N/A')} → {pf.get('suggested','N/A')}",
                       f"Confidence: {rec['confidence']}","Advisory only; no automatic changes."])


if __name__ == "__main__": print(format_telegram(run()))
