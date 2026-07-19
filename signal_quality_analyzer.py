"""Read-only analysis of which historical signal features separate outcomes."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from trade_registry import TradeRegistry

BASE_DIR = Path(__file__).resolve().parent
REPORT_PATH = BASE_DIR / "reports/signal_quality.json"
SUMMARY_PATH = BASE_DIR / "reports/signal_quality_summary.txt"
HISTORY_DIR = BASE_DIR / "reports/signal_quality_history"
MIN_SAMPLE = 5
ANALYZER_VERSION = "1.0.1"
RECOMMENDATIONS = {"KEEP_IN_SHADOW", "INVESTIGATE", "INSUFFICIENT_DATA"}
FEATURES = (
    "symbol", "direction", "confidence", "score", "quality",
    "trend_alignment", "volatility", "atr", "risk_reward", "hour",
    "weekday", "market_regime", "primary_blocker", "decision_reason",
    "entry_type",
)
NUMERIC_FEATURES = {"confidence", "score", "atr", "risk_reward", "hour"}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [_num(row.get("R")) for row in rows]
    wins, losses = [x for x in values if x > 0], [x for x in values if x < 0]
    loss = abs(sum(losses))
    return {
        "trades": len(values),
        "winrate_pct": round(len(wins) / len(values) * 100, 4) if values else 0.0,
        "profit_factor": round(sum(wins) / loss, 6) if loss else (999.0 if wins else 0.0),
        "net_r": round(sum(values), 6),
        "average_r": round(statistics.mean(values), 6) if values else 0.0,
        "median_r": round(statistics.median(values), 6) if values else 0.0,
        "stddev_r": round(statistics.pstdev(values), 6) if len(values) > 1 else 0.0,
        "wins": len(wins), "losses": len(losses),
    }


def _bucket(feature: str, value: Any) -> str:
    if value in (None, "", "UNKNOWN"):
        return "UNKNOWN"
    number = _num(value, float("nan"))
    if feature == "confidence":
        return "<70" if number < 70 else ("70-79" if number < 80 else ("80-89" if number < 90 else "90+"))
    if feature == "score":
        return "<23" if number < 23 else ("23-24" if number < 25 else ("25-26" if number < 27 else "27+"))
    if feature == "atr":
        return "LOW" if number < .5 else ("MEDIUM" if number < 2 else "HIGH")
    if feature == "risk_reward":
        return "<1" if number < 1 else ("1-1.9" if number < 2 else "2+")
    if feature == "hour":
        return f"{int(number):02d}:00" if math.isfinite(number) else "UNKNOWN"
    return str(value).upper()


class SignalQualityAnalyzer:
    def __init__(self, *, registry: TradeRegistry | None = None,
                 base_dir: str | Path = BASE_DIR,
                 context_rows: Iterable[Mapping[str, Any]] | None = None,
                 report_path: str | Path = REPORT_PATH,
                 summary_path: str | Path = SUMMARY_PATH,
                 history_dir: str | Path = HISTORY_DIR,
                 minimum_sample: int = MIN_SAMPLE) -> None:
        self.base_dir = Path(base_dir)
        self.registry = registry or TradeRegistry(self.base_dir / "trades.csv")
        self.context_rows = ([dict(row) for row in context_rows] if context_rows is not None
                             else _csv(self.base_dir / "trade_market_context.csv"))
        self.report_path, self.summary_path, self.history_dir = Path(report_path), Path(summary_path), Path(history_dir)
        self.minimum_sample = minimum_sample

    @staticmethod
    def _key(row: Mapping[str, Any]) -> tuple[str, str]:
        parsed = _time(row.get("opened_at", row.get("timestamp")))
        stamp = parsed.isoformat() if parsed else str(row.get("opened_at", row.get("timestamp", "")))
        return str(row.get("symbol", "")).upper(), stamp

    def collect_features(self) -> list[dict[str, Any]]:
        contexts = {self._key(row): row for row in self.context_rows}
        result = []
        for source in sorted(self.registry.get_complete_trades(), key=lambda row: str(row.get("opened_at", ""))):
            row = deepcopy(dict(source)); context = contexts.get(self._key(row), {})
            opened = _time(row.get("opened_at"))
            entry, stop, target = _num(row.get("entry")), _num(row.get("sl", row.get("stop_loss"))), _num(row.get("tp", row.get("take_profit")))
            trend = str(context.get("trend", row.get("trend", "UNKNOWN"))).upper()
            direction = str(row.get("direction", "UNKNOWN")).upper()
            raw = {
                "trade_id": row.get("trade_id"), "R": _num(row.get("R")),
                "symbol": str(row.get("symbol") or "UNKNOWN").upper(), "direction": direction,
                "confidence": context.get("confidence", row.get("confidence", "UNKNOWN")),
                "score": context.get("score", row.get("score", "UNKNOWN")),
                "quality": context.get("quality", row.get("quality", "UNKNOWN")) or "UNKNOWN",
                "trend_alignment": "UNKNOWN" if trend == "UNKNOWN" else ("ALIGNED" if trend == direction else "COUNTER_TREND"),
                "volatility": context.get("volatility", row.get("volatility", "UNKNOWN")) or "UNKNOWN",
                "atr": context.get("atr", row.get("atr", "UNKNOWN")) or "UNKNOWN",
                "risk_reward": round(abs(target-entry)/abs(entry-stop), 4) if abs(entry-stop) > 0 else "UNKNOWN",
                "hour": opened.hour if opened else "UNKNOWN", "weekday": opened.strftime("%A") if opened else "UNKNOWN",
                "market_regime": context.get("market_regime", row.get("market_regime", "UNKNOWN")) or "UNKNOWN",
                "primary_blocker": context.get("primary_blocker", row.get("primary_blocker", "NONE")) or "NONE",
                "decision_reason": context.get("context_notes", context.get("decision_reason", "UNKNOWN")) or "UNKNOWN",
                "entry_type": context.get("entry_type", row.get("entry_type", "UNKNOWN")) or "UNKNOWN",
                "opened_at": row.get("opened_at"),
            }
            result.append(raw)
        return result

    def analyze_feature(self, rows: Sequence[Mapping[str, Any]], feature: str) -> dict[str, Any]:
        known = [row for row in rows if row.get(feature) not in (None, "", "UNKNOWN")]
        groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[_bucket(feature, row.get(feature))].append(row)
        values = []
        for name, members in groups.items():
            values.append({"value": name, **_metrics(members), "sample_share_pct": round(len(members)/len(rows)*100, 4) if rows else 0})
        values.sort(key=lambda item: item["net_r"], reverse=True)
        coverage = len(known) / len(rows) if rows else 0.0
        known_groups = [item for item in values if item["value"] != "UNKNOWN"]
        winrate_spread = ((max(x["winrate_pct"] for x in known_groups)-min(x["winrate_pct"] for x in known_groups))/100 if len(known_groups)>1 else 0)
        overall_std = _metrics(known)["stddev_r"] if known else 0.0
        between = statistics.pstdev([item["average_r"] for item in known_groups]) if len(known_groups)>1 else 0.0
        separation = min(1.0, between / max(overall_std, .01))
        stability = 1.0 / (1.0 + statistics.mean(item["stddev_r"] for item in known_groups)) if known_groups else 0.0
        sample_factor = min(1.0, len(known) / 30.0) ** .5
        supported = sum(item["trades"] for item in known_groups if item["trades"] >= self.minimum_sample)
        group_support = supported / len(known) if known else 0.0
        score = round(100 * coverage * sample_factor * group_support * (.45*winrate_spread + .35*separation + .20*stability))
        if len(known) < self.minimum_sample or coverage < .4: power = "NONE"
        elif score >= 65 and coverage >= .7: power = "HIGH"
        elif score >= 40 and coverage >= .6: power = "MEDIUM"
        elif score >= 20: power = "LOW"
        else: power = "NONE"
        recommendation = "INSUFFICIENT_DATA" if power == "NONE" else ("INVESTIGATE" if power in {"HIGH", "MEDIUM"} else "KEEP_IN_SHADOW")
        return {"feature": feature, "trades": len(rows), "known_trades": len(known), "coverage_pct": round(coverage*100, 2),
                "unknown_pct": round((1-coverage)*100, 2), "feature_score": score, "predictive_power": power,
                "recommendation": recommendation, "values": values,
                "score_breakdown": {"sample_factor": round(sample_factor,4), "group_support": round(group_support,4), "winrate_spread": round(winrate_spread,4), "separation": round(separation,4), "stability": round(stability,4), "coverage": round(coverage,4)}}

    @staticmethod
    def _profile(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        def avg(field: str) -> float | None:
            values = [_num(row[field], float("nan")) for row in rows if row.get(field) not in (None,"","UNKNOWN")]
            values = [x for x in values if math.isfinite(x)]
            return round(statistics.mean(values), 4) if values else None
        def common(field: str, count: int = 3) -> list[dict[str, Any]]:
            values = [_bucket(field, row.get(field)) for row in rows if row.get(field) not in (None,"","UNKNOWN")]
            return [{"value": value, "trades": amount} for value, amount in Counter(values).most_common(count)]
        return {"trades": len(rows), "average_confidence": avg("confidence"), "average_score": avg("score"),
                "average_atr": avg("atr"), "average_risk_reward": avg("risk_reward"),
                "frequent_symbols": common("symbol"), "direction": common("direction",1), "hours": common("hour"),
                "volatility": common("volatility"), "trend_alignment": common("trend_alignment"), "quality": common("quality")}

    @staticmethod
    def _profile_comparison(winning: Mapping[str, Any], losing: Mapping[str, Any], ranking: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        scores = {item["feature"]: item["feature_score"] for item in ranking}
        result = []
        for feature, profile_key in (("confidence","average_confidence"),("score","average_score"),("atr","average_atr"),("risk_reward","average_risk_reward")):
            win, loss = winning.get(profile_key), losing.get(profile_key)
            result.append({"feature": feature, "winning_value": win, "losing_value": loss,
                           "difference": round(win-loss,4) if win is not None and loss is not None else None,
                           "importance": "HIGH" if scores.get(feature,0)>=65 else ("MEDIUM" if scores.get(feature,0)>=40 else ("LOW" if scores.get(feature,0)>=20 else "NONE"))})
        return result

    def _candidate_filters(self, rows: Sequence[Mapping[str, Any]], features: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        baseline = _metrics(rows); candidates=[]
        for feature in features:
            if feature["feature"] in {"decision_reason", "entry_type"}: continue
            for value in feature["values"]:
                if value["value"] in {"UNKNOWN","NONE"} or value["net_r"] >= 0: continue
                removed=[row for row in rows if _bucket(feature["feature"],row.get(feature["feature"]))==value["value"]]
                remaining=[row for row in rows if row not in removed]; after=_metrics(remaining)
                sufficient=len(removed)>=self.minimum_sample and len(remaining)>=self.minimum_sample
                recommendation="INSUFFICIENT_DATA"
                if sufficient: recommendation="INVESTIGATE" if after["net_r"]>baseline["net_r"] and after["profit_factor"]>=baseline["profit_factor"] else "KEEP_IN_SHADOW"
                confidence=round(min(100, len(removed)/20*100) * feature["coverage_pct"]/100)
                candidates.append({"filter":f"{feature['feature'].upper()}={value['value']}","affected_trades":len(removed),
                    "removed_losses":sum(_num(x["R"])<0 for x in removed),"removed_winners":sum(_num(x["R"])>0 for x in removed),
                    "net_r_change":round(after["net_r"]-baseline["net_r"],6),"pf_change":round(after["profit_factor"]-baseline["profit_factor"],6),
                    "confidence":confidence,"minimum_sample_passed":sufficient,"recommendation":recommendation})
        return sorted(candidates,key=lambda x:(x["recommendation"]!="INVESTIGATE",-x["net_r_change"]))

    def _drift(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if len(rows)<10: return {"status":"UNKNOWN","reason":"Fewer than 10 COMPLETE trades"}
        midpoint=len(rows)//2; first,last=rows[:midpoint],rows[-midpoint:]
        fm,lm=_metrics(first),_metrics(last)
        def average(part,field):
            values=[_num(x[field],float("nan")) for x in part if x.get(field) not in (None,"","UNKNOWN")]; values=[x for x in values if math.isfinite(x)]
            return statistics.mean(values) if values else None
        changes={field:{"first":average(first,field),"last":average(last,field)} for field in ("confidence","score")}
        coverage = {field: {
            "first_pct": round(sum(x.get(field) not in (None,"","UNKNOWN") for x in first)/len(first)*100,2),
            "last_pct": round(sum(x.get(field) not in (None,"","UNKNOWN") for x in last)/len(last)*100,2),
        } for field in ("confidence","score","quality")}
        quality_first=Counter(str(x.get("quality","UNKNOWN")) for x in first).most_common(1)[0][0]
        quality_last=Counter(str(x.get("quality","UNKNOWN")) for x in last).most_common(1)[0][0]
        performance=(lm["profit_factor"]-fm["profit_factor"])+(lm["winrate_pct"]-fm["winrate_pct"])/100
        insufficient = any(values[side] < 50 for values in coverage.values() for side in ("first_pct","last_pct"))
        status="UNKNOWN" if insufficient else ("IMPROVING" if performance>.1 else ("DEGRADING" if performance<-.1 else "STABLE"))
        return {"status":status,"first_trades":len(first),"last_trades":len(last),"first_metrics":fm,"last_metrics":lm,
                "confidence":changes["confidence"],"score":changes["score"],"quality":{"first":quality_first,"last":quality_last},
                "coverage":coverage,"reason":"Insufficient feature coverage" if insufficient else "Performance comparison"}

    def build_report(self) -> dict[str, Any]:
        rows=self.collect_features(); analyses=[self.analyze_feature(rows,f) for f in FEATURES]
        ranking=sorted(analyses,key=lambda x:(-x["feature_score"],-x["coverage_pct"],x["feature"]))
        for index,item in enumerate(ranking,1): item["rank"]=index
        winners=[x for x in rows if _num(x["R"])>0]; losers=[x for x in rows if _num(x["R"])<0]
        winning,losing=self._profile(winners),self._profile(losers); baseline=_metrics(rows)
        average_coverage=statistics.mean(x["coverage_pct"] for x in analyses) if analyses else 0
        breakdown={"winrate":min(30,baseline["winrate_pct"]*.3),"profit_factor":min(25,baseline["profit_factor"]/2*25),
                   "net_r":20 if baseline["net_r"]>0 else 0,"data_coverage":average_coverage*.15,
                   "feature_evidence":min(10,(ranking[0]["feature_score"] if ranking else 0)*.1)}
        quality_score=round(sum(breakdown.values())); status="EXCELLENT" if quality_score>=85 else ("GOOD" if quality_score>=70 else ("AVERAGE" if quality_score>=50 else "POOR"))
        fingerprint=hashlib.sha256(json.dumps({"analyzer_version":ANALYZER_VERSION,"rows":rows},sort_keys=True,default=str).encode()).hexdigest()
        sources={name:("OK" if _json(self.base_dir/path) else "NOT_AVAILABLE") for name,path in {
            "loss_attribution":"reports/loss_attribution.json","research_dashboard":"reports/research_dashboard.json",
            "walk_forward":"reports/walk_forward.json","replay":"shadow_replay_report.json","adaptive_research":"adaptive_research_report.json"}.items()}
        return {"generated_at":datetime.now(timezone.utc).isoformat(),"analyzer_version":ANALYZER_VERSION,"mode":"SHADOW_READ_ONLY","dataset_fingerprint":fingerprint,
            "sample":{"complete_trades":len(rows),"wins":len(winners),"losses":len(losers)},"baseline":baseline,
            "feature_ranking":ranking,"winning_profile":winning,"losing_profile":losing,
            "profile_comparison":self._profile_comparison(winning,losing,ranking),"candidate_filters":self._candidate_filters(rows,analyses),
            "drift":self._drift(rows),"signal_quality_score":quality_score,"signal_quality_status":status,"score_breakdown":breakdown,
            "recommendation":"INSUFFICIENT_DATA" if len(rows)<self.minimum_sample else ("INVESTIGATE" if status in {"POOR","AVERAGE"} else "KEEP_IN_SHADOW"),
            "source_health":sources,"allowed_recommendations":sorted(RECOMMENDATIONS),
            "restrictions":["READ_ONLY","NO_AUTOMATIC_APPLICATION","LIVE_AND_TRADING_LOGIC_UNCHANGED"]}

    def write_reports(self) -> tuple[dict[str,Any],bool]:
        report=self.build_report(); _write(self.report_path,json.dumps(report,ensure_ascii=False,indent=2)+"\n"); _write(self.summary_path,format_signal_quality(report)+"\n")
        self.history_dir.mkdir(parents=True,exist_ok=True)
        duplicate=any(_json(path).get("dataset_fingerprint")==report["dataset_fingerprint"] for path in self.history_dir.glob("*.json"))
        if not duplicate:
            stamp=datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f"); _write(self.history_dir/f"{stamp}.json",json.dumps(report,ensure_ascii=False,indent=2)+"\n")
        return report,not duplicate


def load_signal_quality(path: str | Path = REPORT_PATH) -> dict[str,Any]: return _json(Path(path))


def format_signal_quality(report: Mapping[str,Any], view: str="") -> str:
    ranking=report.get("feature_ranking",[]); drift=report.get("drift",{})
    if view=="features":
        return "\n".join(["📈 Signal Quality — Features"]+[f"{x['rank']}. {x['feature']}: {x['feature_score']}/100, coverage {x['coverage_pct']:.1f}%, {x['predictive_power']}" for x in ranking]+["Shadow analysis only."])
    if view=="drift":
        return "\n".join(["📈 Signal Quality — Drift",f"Status: {drift.get('status','UNKNOWN')}",f"First PF: {_num(drift.get('first_metrics',{}).get('profit_factor')):.3f}",f"Last PF: {_num(drift.get('last_metrics',{}).get('profit_factor')):.3f}","LIVE remains unchanged."])
    lines=["📈 Signal Quality",f"Complete Trades: {report.get('sample',{}).get('complete_trades',0)}",f"Signal Score: {report.get('signal_quality_score',0)}/100",f"Status: {report.get('signal_quality_status','POOR')}"]
    profile=report.get("winning_profile",{}); losing=report.get("losing_profile",{})
    lines.extend(["Winning Profile:",f"Confidence {profile.get('average_confidence')}",f"Score {profile.get('average_score')}","Losing Profile:",f"Confidence {losing.get('average_confidence')}",f"Score {losing.get('average_score')}","Top Predictive Features:"])
    lines.extend(f"{x['rank']}. {x['feature']} ({x['predictive_power']})" for x in ranking[:5 if view=="top" else 3])
    lines.extend([f"Drift: {drift.get('status','UNKNOWN')}",f"Recommendation: {report.get('recommendation','INSUFFICIENT_DATA')}","LIVE remains unchanged."])
    return "\n".join(lines)


def main() -> None:
    report,snapshot=SignalQualityAnalyzer().write_reports(); print(format_signal_quality(report,"top")); print(f"History snapshot: {'created' if snapshot else 'deduplicated'}")


if __name__=="__main__": main()
