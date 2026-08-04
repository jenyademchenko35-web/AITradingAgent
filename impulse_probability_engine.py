"""Read-only Impulse Probability Engine; never returns a trading decision."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

BASE_DIR=Path(__file__).resolve().parent
OUTPUT=BASE_DIR/"impulse_probability.json"; HEATMAP=BASE_DIR/"market_heatmap.json"
HISTORY=BASE_DIR/"impulse_probability_history.jsonl"; CHANGES=BASE_DIR/"impulse_changes.json"
def n(value: Any)->float:
    try:return float(value or 0)
    except (TypeError,ValueError):return 0.0
def label(value:int)->str:
    return "EXTREME" if value>=90 else "HIGH" if value>=75 else "MEDIUM" if value>=60 else "LOW" if value>=40 else "VERY_LOW"
def evaluate(context:Mapping[str,Any])->dict[str,Any]:
    score=0.; reasons=[]
    def add(condition:bool,points:float,text:str):
        nonlocal score
        if condition: score+=points; reasons.append({"points":points,"reason":text})
    trend=n(context.get("trend_score")); momentum=n(context.get("momentum_score")); adx=n(context.get("adx")); volume=n(context.get("volume_ratio")); confidence=n(context.get("confidence")); edge=abs(n(context.get("edge",context.get("score"))))
    add(trend>=50,18,"strong trend"); add(momentum>=15,12,"momentum confirmed"); add(adx>=25,10,"ADX above 25"); add(volume>=1,15,"volume above average"); add(confidence>=70,8,"confidence above 70"); add(edge>=20,8,"directional edge"); add(str(context.get("market_regime","")).upper() in {"TREND","BULLISH","BEARISH"},12,"trend regime")
    failed=" ".join(str(x) for x in context.get("failed_filters",[]) or []).lower(); add("risk" in failed,-20,"failed risk filter"); add("momentum" in failed,-15,"failed momentum filter")
    probability=max(0,min(100,round(score)))
    return {"symbol":str(context.get("symbol","UNKNOWN")),"impulse_probability":probability,"class":label(probability),"reasons":reasons,"timestamp":str(context.get("timestamp") or datetime.now(timezone.utc).isoformat())}
def publish(contexts:list[Mapping[str,Any]], *, output:Path=OUTPUT, heatmap:Path=HEATMAP)->list[dict[str,Any]]:
    previous={}
    try: previous={x.get("symbol"):x for x in json.loads(output.read_text()) if isinstance(x,dict)}
    except (OSError,ValueError,TypeError): pass
    rows=[]
    changes=[]
    for context in contexts:
        row=evaluate(context); prior=previous.get(row["symbol"]); old=n(prior.get("impulse_probability")) if prior else None
        if old is not None and abs(row["impulse_probability"]-old)>=15:
            delta=row["impulse_probability"]-old; row["change"]={"from":old,"to":row["impulse_probability"],"delta":delta}
            changes.append({"symbol":row["symbol"],"previous_probability":old,"current_probability":row["impulse_probability"],"delta":delta,"direction":"UP" if delta>0 else "DOWN","previous_class":prior.get("class"),"current_class":row["class"],"reasons":row["reasons"],"timestamp":row["timestamp"]})
        rows.append(row)
    rows.sort(key=lambda x:(-x["impulse_probability"],x["symbol"])); output.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8"); heatmap.write_text(json.dumps([{k:r[k] for k in ("symbol","impulse_probability","class")} for r in rows],ensure_ascii=False,indent=2),encoding="utf-8")
    existing=set()
    try: existing={(json.loads(line).get("symbol"),json.loads(line).get("cycle_id")) for line in HISTORY.read_text(encoding="utf-8").splitlines() if line}
    except (OSError,ValueError): pass
    try:
        with HISTORY.open("a",encoding="utf-8") as handle:
            for row,context in zip(rows,contexts):
                key=(row["symbol"],context.get("cycle_id") or row["timestamp"])
                if key in existing: continue
                handle.write(json.dumps({"schema_version":"ipe-history-v1","timestamp":row["timestamp"],"cycle_id":key[1],"symbol":row["symbol"],"side":context.get("side") or context.get("direction"),"impulse_probability":row["impulse_probability"],"impulse_class":row["class"],"delta_from_previous":row.get("change",{}).get("delta"),"raw_signal":context.get("raw_signal"),"market_regime":context.get("market_regime"),"adx":context.get("adx"),"atr":context.get("atr"),"volume_ratio":context.get("volume_ratio"),"failed_filters":context.get("failed_filters",[]),"explanations":row["reasons"]},ensure_ascii=False)+"\n")
    except OSError: pass
    CHANGES.write_text(json.dumps({"schema_version":"ipe-changes-v1","generated_at":datetime.now(timezone.utc).isoformat(),"status":"READY","limitations":[],"data_quality":{},"items":sorted(changes,key=lambda x:-abs(x["delta"]))},ensure_ascii=False,indent=2),encoding="utf-8"); return rows
