"""Read-only analysis of published signal, trade and telemetry artifacts."""
from __future__ import annotations

import csv, json, math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EPISODE_ACTIVE = {"SETUP", "WATCH", "HIGH PRIORITY", "HIGH_PRIORITY"}
BLOCKER_MAP = {"momentum":"Momentum", "trend":"Trend", "structure":"Structure", "risk":"Risk", "cooldown":"Cooldown", "portfolio":"Portfolio", "correlation":"Correlation", "rr":"RR", "volume":"Volume", "regime":"Regime"}

def rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as handle: return list(csv.DictReader(handle))
    except (OSError, UnicodeError): return []

def ts(value: Any) -> datetime | None:
    try: return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError): return None

def number(value: Any) -> float | None:
    try:
        result = float(str(value).replace("%", "")); return result if math.isfinite(result) else None
    except (ValueError, TypeError): return None

def status(row: dict[str, str]) -> str:
    return str(row.get("final_signal") or row.get("signal") or row.get("raw_signal") or "NO TRADE").upper().replace("_", " ")

def episodes(signals: list[dict[str, str]], trades: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in signals: grouped[str(row.get("symbol") or "UNKNOWN")].append(row)
    opened = {(str(t.get("symbol")), str(t.get("direction")).upper()) for t in trades if t.get("opened_at")}
    output=[]
    for symbol, items in grouped.items():
        items.sort(key=lambda r: ts(r.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)); current=None
        for row in items:
            value, side = status(row), str(row.get("direction") or "").upper()
            side = side if side in {"LONG", "SHORT"} else "UNKNOWN"
            active = value in EPISODE_ACTIVE
            if current and (not active or side != current["side"]):
                end=ts(row.get("timestamp")) or current["start"]
                current["end_time"], current["duration_minutes"] = end.isoformat(), round((end-current["start"]).total_seconds()/60, 2)
                current["bars_alive"], current["final_status"] = current.pop("bars"), value
                current["opened_trade"] = (symbol, current["side"]) in opened
                current["blocked_reason"] = row.get("reason") or None
                current.pop("start"); output.append(current); current=None
            if active and current is None:
                start=ts(row.get("timestamp")) or datetime.now(timezone.utc)
                current={"episode_id":f"{symbol}:{side}:{start.isoformat()}", "symbol":symbol,"side":side,"start":start,"start_time":start.isoformat(),"bars":0,"highest_priority":value}
            if current:
                current["bars"] += 1
                if value == "HIGH PRIORITY": current["highest_priority"]="HIGH PRIORITY"
        if current:
            end=current["start"]; current.update(end_time=end.isoformat(),duration_minutes=0,bars_alive=current.pop("bars"),final_status="ONGOING",opened_trade=(symbol,current["side"]) in opened,blocked_reason=None); current.pop("start"); output.append(current)
    return output

def blocker_stats(signals: list[dict[str,str]], telemetry: list[dict[str,str]]) -> dict[str, Any]:
    found=defaultdict(list)
    for row in signals + telemetry:
        text=" ".join(str(row.get(k,"")) for k in ("reason","comment","blocking_reasons","failed_filters","veto_reasons")).lower()
        category=next((label for key,label in BLOCKER_MAP.items() if key in text),"Unknown")
        found[category].append({"timestamp":row.get("timestamp"),"symbol":row.get("symbol"),"reason":row.get("reason") or row.get("blocking_reasons")})
    total=sum(map(len,found.values()))
    return {key:{"count":len(value),"percent":round(len(value)*100/total,2) if total else 0,"examples":value[-3:]} for key,value in sorted(found.items())}

def metrics(values: list[float]) -> dict[str, Any]:
    wins=[x for x in values if x>0]; losses=[x for x in values if x<0]; loss=abs(sum(losses)); equity=peak=drawdown=0
    for value in values: equity+=value; peak=max(peak,equity); drawdown=max(drawdown,peak-equity)
    return {"wins":len(wins),"losses":len(losses),"winrate":round(100*len(wins)/len(values),2) if values else None,"profit_factor":round(sum(wins)/loss,3) if loss else None,"net_r":round(sum(values),3),"expectancy":round(sum(values)/len(values),3) if values else None,"max_drawdown_r":round(drawdown,3)}

def trade_quality(trades: list[dict[str,str]], signals: list[dict[str,str]]) -> list[dict[str,Any]]:
    result=[]
    for trade in trades:
        opened,closed=ts(trade.get("opened_at")),ts(trade.get("closed_at")); entry,stop=number(trade.get("entry")),number(trade.get("stop_loss"))
        risk=abs(entry-stop) if entry is not None and stop is not None else None
        prices=[number(s.get("price")) for s in signals if s.get("symbol")==trade.get("symbol") and opened and closed and (stamp:=ts(s.get("timestamp"))) and opened<=stamp<=closed]
        prices=[p for p in prices if p is not None]
        side=str(trade.get("direction","")).upper(); rvalues=[((p-entry) if side=="LONG" else (entry-p))/risk for p in prices] if risk else []
        result.append({"symbol":trade.get("symbol"),"side":side,"opened_at":trade.get("opened_at"),"closed_at":trade.get("closed_at"),"pnl_r":number(trade.get("pnl_r")) or number(trade.get("pnl")),"mfe_r":max(rvalues) if rvalues else None,"mae_r":min(rvalues) if rvalues else None,"bars_alive":len(prices),"time_to_mfe":None,"time_to_mae":None,"partial_tp_reached":None,"sl_after_profit":bool(rvalues and max(rvalues)>0 and (number(trade.get("pnl_r")) or 0)<0)})
    return result

def diagnostics(base: Path) -> dict[str,Any]:
    signals=rows(base/"signals_v3.csv") or rows(base/"signals.csv"); trades=rows(base/"trades.csv"); telemetry=rows(base/"decision_engine_telemetry.csv")
    eps=episodes(signals,trades); quality=trade_quality(trades,signals); by_symbol={}
    for symbol in sorted({r.get("symbol") for r in signals+trades if r.get("symbol")}):
        relevant=[t for t in trades if t.get("symbol")==symbol]; values=[v for t in relevant if (v:=number(t.get("pnl_r"))) is not None]
        by_symbol[symbol]={"signals":sum(r.get("symbol")==symbol for r in signals),"episodes":sum(e["symbol"]==symbol for e in eps),"opened_trades":len(relevant),**metrics(values),"blocked_percent":None}
    feature={}
    for key in ("trend_score","structure_score","momentum_score","risk_score","volume_score","adx","atr","volume_ratio"):
        wins=[number(r.get(key)) for r in telemetry if "WIN" in str(r.get("final_decision",""))]; losses=[number(r.get(key)) for r in telemetry if "LOSS" in str(r.get("final_decision",""))]
        wins=[x for x in wins if x is not None]; losses=[x for x in losses if x is not None]
        feature[key]={"average_win":sum(wins)/len(wins) if wins else None,"average_loss":sum(losses)/len(losses) if losses else None,"difference":(sum(wins)/len(wins)-sum(losses)/len(losses)) if wins and losses else None,"importance_score":abs(sum(wins)/len(wins)-sum(losses)/len(losses)) if wins and losses else None}
    bias={}
    for symbol in by_symbol:
        signal_dirs=[str(r.get("direction","")).upper() for r in signals if r.get("symbol")==symbol and status(r) in EPISODE_ACTIVE]
        trade_dirs=[str(r.get("direction","")).upper() for r in trades if r.get("symbol")==symbol]
        total=len(signal_dirs)
        bias[symbol]={"long_percent":round(100*signal_dirs.count("LONG")/total,2) if total else None,"short_percent":round(100*signal_dirs.count("SHORT")/total,2) if total else None,"setup_long":sum(d=="LONG" for d in signal_dirs),"setup_short":sum(d=="SHORT" for d in signal_dirs),"trade_long":trade_dirs.count("LONG"),"trade_short":trade_dirs.count("SHORT"),"winrate_long":None,"winrate_short":None}
        for side in ("LONG","SHORT"):
            values=[number(t.get("pnl_r")) for t in trades if t.get("symbol")==symbol and str(t.get("direction","")).upper()==side]
            values=[x for x in values if x is not None]
            bias[symbol][f"winrate_{side.lower()}"]=round(100*sum(x>0 for x in values)/len(values),2) if values else None
    recommendations=[f"{s}: PF {v['profit_factor']}, winrate {v['winrate']} — review or exclude from Shadow." for s,v in by_symbol.items() if v["profit_factor"] is not None and v["profit_factor"]<1]
    return {"episodes":{"items":eps,"count":len(eps)},"blockers":blocker_stats(signals,telemetry),"trade_quality":{"items":quality,"count":len(quality)},"symbols":by_symbol,"direction_bias":bias,"feature_importance":feature,"recommendations":recommendations or ["Insufficient published data for evidence-based recommendations."]}

def write_reports(base: Path) -> dict[str,Any]:
    payload=diagnostics(base); mapping={"signal_episode_report.json":payload["episodes"],"blocker_statistics.json":payload["blockers"],"trade_quality_report.json":payload["trade_quality"],"symbol_statistics.json":payload["symbols"],"direction_bias.json":payload["direction_bias"],"feature_importance.json":payload["feature_importance"],"strategy_recommendations.json":payload["recommendations"]}
    for name,value in mapping.items(): (base/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
    return payload
