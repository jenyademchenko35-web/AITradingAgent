"""Read-only, schema-v2 diagnostics for published runtime artifacts.

The module never edits source CSVs.  It deliberately keeps monetary results
separate from R-multiples and marks incomplete evidence instead of guessing.
"""
from __future__ import annotations

import csv, json, math, statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "runtime-diagnostics-v2"
EPISODE_ACTIVE = {"WATCH", "SETUP", "HIGH PRIORITY"}
BLOCKERS = {"momentum":"Momentum", "trend":"Trend", "structure":"Structure", "risk":"Risk", "cooldown":"Cooldown", "portfolio":"Portfolio", "correlation":"Correlation", "rr":"RR", "volume":"Volume", "regime":"Regime"}
R_ALIASES = ("pnl_r", "result_r", "net_r", "realized_r")

def rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as handle: return list(csv.DictReader(handle))
    except (OSError, UnicodeError): return []

def timestamp(value: Any) -> datetime | None:
    try:
        parsed=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (ValueError, TypeError): return None

def number(value: Any) -> float | None:
    try:
        result=float(str(value).replace("%","")); return result if math.isfinite(result) else None
    except (ValueError, TypeError): return None

def signal_status(row: dict[str,str]) -> str:
    return str(row.get("final_signal") or row.get("signal") or row.get("raw_signal") or "NO TRADE").upper().replace("_"," ")

def trade_result(row: dict[str,str]) -> tuple[float | None, float | None, str]:
    """Return (pnl_r, pnl_money, basis); never infer R from a money pnl."""
    r_value=next((number(row.get(key)) for key in R_ALIASES if number(row.get(key)) is not None),None)
    money=number(row.get("pnl"))
    if r_value is not None: return r_value, money, "r"
    if money is not None: return None, money, "money"
    return None, None, "unknown"

def validate_trade(row: dict[str,str]) -> tuple[dict[str,Any] | None, str | None]:
    opened, closed=timestamp(row.get("opened_at")), timestamp(row.get("closed_at"))
    if opened is None: return None, "MISSING_OPEN_TIME"
    if closed is None: return None, "MISSING_CLOSE_TIME"
    if closed < opened: return None, "INVALID_TIME_ORDER"
    r_value,money,basis=trade_result(row)
    return {"row":row,"opened":opened,"closed":closed,"pnl_r":r_value,"pnl_money":money,"metric_basis":basis},None

def quality_summary(trades: list[dict[str,str]]) -> tuple[list[dict[str,Any]], list[dict[str,Any]], dict[str,int]]:
    valid,invalid=[],[]; summary=Counter(total_rows=len(trades))
    for row in trades:
        item,error=validate_trade(row)
        if error:
            invalid.append({"trade_id":row.get("trade_id") or row.get("shadow_trade_id"),"symbol":row.get("symbol"),"data_quality_status":error}); summary[error.lower()]+=1; continue
        assert item is not None
        summary["valid_rows"]+=1
        for key,label in (("pnl_r","missing_result"),("entry","missing_entry"),("stop_loss","missing_stop")):
            if key in item and item[key] is None or key not in item and number(row.get(key)) is None: summary[label]+=1
        valid.append(item)
    return valid,invalid,dict(summary)

def percentile(values: list[float], p: float) -> float | None:
    if not values: return None
    values=sorted(values); return values[min(len(values)-1, max(0, math.ceil(len(values)*p)-1))]

def episodes(signals: list[dict[str,str]], trades: list[dict[str,str]], gap_minutes: int=30) -> dict[str,Any]:
    by_symbol=defaultdict(list)
    for row in signals: by_symbol[str(row.get("symbol") or "UNKNOWN")].append(row)
    opened={(str(t.get("symbol")),str(t.get("direction") or "").upper()) for t in trades if timestamp(t.get("opened_at"))}
    output=[]
    for symbol,items in by_symbol.items():
        items.sort(key=lambda r: timestamp(r.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)); current=None
        for row in items:
            stamp=timestamp(row.get("timestamp")); value=signal_status(row); side=str(row.get("direction") or "").upper(); side=side if side in {"LONG","SHORT"} else "UNKNOWN"
            inactive=value not in EPISODE_ACTIVE
            gap=current and stamp and (stamp-current["last"]).total_seconds()>gap_minutes*60
            changed=current and side != current["side"]
            if current and (inactive or gap or changed):
                end=stamp or current["last"]; duration=(end-current["start"]).total_seconds()/60
                output.append({"episode_id":f"{symbol}:{current['side']}:{current['start'].isoformat()}","symbol":symbol,"side":current["side"],"start_time":current["start"].isoformat(),"end_time":end.isoformat(),"duration_minutes":round(duration,2),"bars_alive":current["bars"],"highest_priority":current["priority"],"final_status":"INACTIVITY_GAP" if gap else value,"opened_trade":(symbol,current["side"]) in opened,"blocked_reason":row.get("reason") or None})
                current=None
            if not inactive and current is None:
                start=stamp or datetime.now(timezone.utc); current={"start":start,"last":start,"side":side,"bars":0,"priority":value}
            if current: current["last"]=stamp or current["last"]; current["bars"]+=1; current["priority"]="HIGH PRIORITY" if value=="HIGH PRIORITY" else current["priority"]
        if current:
            duration=(current["last"]-current["start"]).total_seconds()/60
            output.append({"episode_id":f"{symbol}:{current['side']}:{current['start'].isoformat()}","symbol":symbol,"side":current["side"],"start_time":current["start"].isoformat(),"end_time":current["last"].isoformat(),"duration_minutes":round(duration,2),"bars_alive":current["bars"],"highest_priority":current["priority"],"final_status":"ONGOING","opened_trade":(symbol,current["side"]) in opened,"blocked_reason":None})
    durations=[e["duration_minutes"] for e in output]
    return {"items":output,"summary":{"total_episodes":len(output),"by_symbol":dict(Counter(e["symbol"] for e in output)),"by_side":dict(Counter(e["side"] for e in output)),"by_final_status":dict(Counter(e["final_status"] for e in output)),"average_duration_minutes":statistics.mean(durations) if durations else None,"median_duration_minutes":statistics.median(durations) if durations else None,"p90_duration_minutes":percentile(durations,.9),"opened_trade_count":sum(e["opened_trade"] for e in output),"conversion_to_trade_percent":round(100*sum(e["opened_trade"] for e in output)/len(output),2) if output else None}}

def blocker_statistics(signals: list[dict[str,str]], telemetry: list[dict[str,str]]) -> dict[str,Any]:
    examined=signals+telemetry; explicit=defaultdict(list); missing=not_blocked=unknown=0
    for row in examined:
        raw=next((row.get(key) for key in ("blocking_reasons","failed_filters","veto_reasons","reason") if row.get(key) not in (None,"")),None)
        if raw is None: missing+=1; continue
        text=str(raw).lower()
        match=next((label for key,label in BLOCKERS.items() if key in text),None)
        if match: explicit[match].append({"timestamp":row.get("timestamp"),"symbol":row.get("symbol"),"value":str(raw)[:300]})
        elif text in {"pass","none","[]","no trade"}: not_blocked+=1
        else: unknown+=1
    coverage=sum(map(len,explicit.values()))+unknown
    return {"rows_examined":len(examined),"rows_with_explicit_blocker":coverage,"rows_without_blocker_field":missing,"blocker_coverage_percent":round(100*coverage/len(examined),2) if examined else 0,"explicit_blockers":{key:{"count":len(v),"percent":round(100*len(v)/coverage,2) if coverage else 0,"examples":v[-3:]} for key,v in explicit.items()},"missing_blocker_data":missing,"not_blocked":not_blocked,"unknown_unmapped_value":unknown}

def metric_set(values: list[float]) -> dict[str,Any]:
    wins=[x for x in values if x>0]; losses=[x for x in values if x<0]; loss=abs(sum(losses))
    return {"wins":len(wins),"losses":len(losses),"winrate":round(100*len(wins)/len(values),2) if values else None,"profit_factor":round(sum(wins)/loss,3) if loss else None,"net":round(sum(values),3) if values else None,"expectancy":round(statistics.mean(values),3) if values else None}

def trade_quality(valid: list[dict[str,Any]], invalid: list[dict[str,Any]], signals: list[dict[str,str]]) -> dict[str,Any]:
    items=[]; missing_snapshots=0
    for item in valid:
        row=item["row"]; entry,stop=number(row.get("entry")),number(row.get("stop_loss")); side=str(row.get("direction") or "").upper(); duration=(item["closed"]-item["opened"]).total_seconds()
        result={"trade_id":row.get("trade_id") or row.get("shadow_trade_id"),"symbol":row.get("symbol"),"side":side,"opened_at":item["opened"].isoformat(),"closed_at":item["closed"].isoformat(),"duration_seconds":duration,"duration_minutes":round(duration/60,2),"duration_hours":round(duration/3600,3),"bars_alive":None,"pnl_r":item["pnl_r"],"pnl_money":item["pnl_money"],"metric_basis":item["metric_basis"],"mfe_r":None,"mae_r":None,"mfe_status":None}
        timeframe=number(str(row.get("timeframe") or "").replace("h","")); result["bars_alive"]=math.floor(duration/(timeframe*3600)) if timeframe and timeframe>0 else None
        if entry is None: result["mfe_status"]="MISSING_ENTRY"
        elif stop is None: result["mfe_status"]="MISSING_STOP"
        elif side not in {"LONG","SHORT"}: result["mfe_status"]="UNKNOWN_SIDE"
        else:
            prices=[number(s.get("price")) for s in signals if s.get("symbol")==row.get("symbol") and (t:=timestamp(s.get("timestamp"))) and item["opened"]<=t<=item["closed"]]; prices=[p for p in prices if p is not None]
            if not prices: result["mfe_status"]="NO_PRICE_SNAPSHOTS"; missing_snapshots+=1
            else:
                risk=abs(entry-stop); rvalues=[((p-entry) if side=="LONG" else (entry-p))/risk for p in prices]; result["mfe_r"],result["mae_r"],result["mfe_status"]=max(rvalues),min(rvalues),"CALCULATED"
        items.append(result)
    return {"items":items,"invalid_items":invalid,"data_quality_summary":{"total_rows":len(valid)+len(invalid),"valid_rows":len(valid),"invalid_rows":len(invalid),"missing_price_snapshots":missing_snapshots}}

def diagnostics(base: Path) -> dict[str,Any]:
    source={name:base/name for name in ("signals_v3.csv","signals.csv","trades.csv","research_lab_shadow_history.csv","decision_engine_telemetry.csv")}; signal_rows=rows(source["signals_v3.csv"]) or rows(source["signals.csv"]); trade_rows=rows(source["trades.csv"])+rows(source["research_lab_shadow_history.csv"]); telemetry=rows(source["decision_engine_telemetry.csv"])
    valid,invalid,quality=quality_summary(trade_rows); eps=episodes(signal_rows,trade_rows); blocks=blocker_statistics(signal_rows,telemetry); quality_report=trade_quality(valid,invalid,signal_rows)
    symbols={}
    for symbol in sorted({str(r.get("symbol")) for r in signal_rows+trade_rows if r.get("symbol")}):
        trades=[x for x in valid if x["row"].get("symbol")==symbol]; rvals=[x["pnl_r"] for x in trades if x["pnl_r"] is not None]; money=[x["pnl_money"] for x in trades if x["pnl_money"] is not None]
        symbols[symbol]={"opened_trades":sum(r.get("symbol")==symbol for r in trade_rows),"closed_trades":len(trades),"trades_with_r_result":len(rvals),"trades_with_money_result":len(money),"trades_without_result":sum(x["metric_basis"]=="unknown" for x in trades),"r_metrics":metric_set(rvals),"money_metrics":metric_set(money),"metric_basis":"r" if rvals else "money" if money else "unknown"}
    feature={"status":"INSUFFICIENT_TELEMETRY","sample_size":0,"matched_sample_size":0,"minimum_sample_required":20,"confidence":"LOW","limitations":["Telemetry must be joined to closed trades by a stable trade key before feature importance is calculated."]} if not telemetry else {"status":"INSUFFICIENT_MATCHED_RESULTS","sample_size":len(telemetry),"matched_sample_size":0,"minimum_sample_required":20,"confidence":"LOW","limitations":["No safe telemetry-to-trade joins are available in the current published schema."]}
    direction={}
    for symbol in symbols:
        relevant=[e for e in eps["items"] if e["symbol"]==symbol]; long=sum(e["side"]=="LONG" for e in relevant); short=sum(e["side"]=="SHORT" for e in relevant); count=len(relevant); warning="DIRECTIONAL_BIAS_OBSERVED" if count>=20 and max(long,short)/count>=.9 else None
        direction[symbol]={"first_timestamp":min((e["start_time"] for e in relevant),default=None),"last_timestamp":max((e["end_time"] for e in relevant),default=None),"distinct_signal_episodes_long":long,"distinct_signal_episodes_short":short,"opened_trades_long":None,"opened_trades_short":None,"result_coverage_long":None,"result_coverage_short":None,"warning":warning}
    recommendations=[]
    if not any(v["trades_with_r_result"] for v in symbols.values()): recommendations.append({"code":"INSUFFICIENT_PNL_R","severity":"WARNING","evidence":{"trades_with_r_result":0},"action":"Collect published pnl_r/result_r for closed trades.","requires_more_data":True})
    if blocks["blocker_coverage_percent"]<30: recommendations.append({"code":"MISSING_BLOCKER_TELEMETRY","severity":"INFO","evidence":{"coverage_percent":blocks["blocker_coverage_percent"]},"action":"Collect explicit blocker telemetry before assessing blocker influence.","requires_more_data":True})
    if feature["status"]!="READY": recommendations.append({"code":"MISSING_FEATURE_TELEMETRY","severity":"INFO","evidence":{"status":feature["status"]},"action":"Collect and safely join DecisionEngine telemetry after the agent update.","requires_more_data":True})
    meta={"schema_version":SCHEMA_VERSION,"generated_at":datetime.now(timezone.utc).isoformat(),"source_files":{name:str(path) for name,path in source.items() if path.exists()},"source_row_counts":{"signals":len(signal_rows),"trades":len(trade_rows),"telemetry":len(telemetry)},"data_quality":quality,"limitations":["Money and R metrics are never mixed.","MFE/MAE require timestamped price snapshots."],"status":"PARTIAL" if invalid or not telemetry else "READY"}
    return {"metadata":meta,"episodes":eps,"blockers":blocks,"trade_quality":quality_report,"symbols":symbols,"direction_bias":direction,"feature_importance":feature,"recommendations":recommendations}

def write_reports(base: Path) -> dict[str,Any]:
    payload=diagnostics(base); mapping={"signal_episode_report.json":{"metadata":payload["metadata"],**payload["episodes"]},"blocker_statistics.json":{"metadata":payload["metadata"],**payload["blockers"]},"trade_quality_report.json":{"metadata":payload["metadata"],**payload["trade_quality"]},"symbol_statistics.json":{"metadata":payload["metadata"],"items":payload["symbols"]},"direction_bias.json":{"metadata":payload["metadata"],"items":payload["direction_bias"]},"feature_importance.json":{"metadata":payload["metadata"],**payload["feature_importance"]},"strategy_recommendations.json":{"metadata":payload["metadata"],"items":payload["recommendations"]}}
    for name,value in mapping.items(): (base/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
    return payload
