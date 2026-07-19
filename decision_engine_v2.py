"""Experimental DecisionEngine v2 calibration branch.

This module never imports into or mutates the LIVE decision pipeline. It keeps
v1 raw values intact and evaluates a parallel, report-only shadow decision.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from shadow_replay.metrics import metric_bundle
from signal_quality_analyzer import SignalQualityAnalyzer
from trade_registry import TradeRegistry
from walk_forward_validator import build_windows

BASE_DIR = Path(__file__).resolve().parent
WEIGHTS_PATH = BASE_DIR / "decision_engine_v2_weights.json"
REPORT_PATH = BASE_DIR / "reports/decision_engine_v2.json"
SUMMARY_PATH = BASE_DIR / "reports/decision_engine_v2_summary.txt"
HISTORY_DIR = BASE_DIR / "reports/decision_engine_v2_history"
ALLOWED_STATUSES = {"EXPERIMENTAL", "PROMISING", "READY_FOR_AB", "REJECT"}


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


class DecisionEngineV2:
    """Small evidence-weighted calibration layer over immutable v1 output."""

    def __init__(self, *, base_dir: str | Path = BASE_DIR,
                 registry: TradeRegistry | None = None,
                 weights_path: str | Path = WEIGHTS_PATH,
                 feature_rows: Sequence[Mapping[str, Any]] | None = None,
                 signal_quality_report: Mapping[str, Any] | None = None,
                 report_path: str | Path = REPORT_PATH,
                 summary_path: str | Path = SUMMARY_PATH,
                 history_dir: str | Path = HISTORY_DIR) -> None:
        self.base_dir = Path(base_dir)
        self.registry = registry or TradeRegistry(self.base_dir / "trades.csv")
        self.weights_path = Path(weights_path)
        self.weights = _read_json(self.weights_path)
        if not self.weights:
            raise ValueError(f"Missing or invalid v2 weights: {self.weights_path}")
        self.signal_quality = dict(signal_quality_report or _read_json(self.base_dir / "reports/signal_quality.json"))
        self._feature_rows = [dict(row) for row in feature_rows] if feature_rows is not None else None
        self.report_path, self.summary_path, self.history_dir = Path(report_path), Path(summary_path), Path(history_dir)
        self.power = {str(item.get("feature")): str(item.get("predictive_power", "NONE"))
                      for item in self.signal_quality.get("feature_ranking", [])}

    def _multiplier(self, feature: str) -> float:
        level = self.power.get(feature, "NONE")
        return float(self.weights["predictive_power_multipliers"].get(level, .25))

    def calibrate_confidence(self, raw_confidence: Any) -> dict[str, Any]:
        raw = _number(raw_confidence)
        if raw is None:
            return {"raw_confidence": None, "calibrated_confidence": None,
                    "confidence_change": 0.0, "calibration_reason": ["UNKNOWN_CONFIDENCE_PRESERVED"]}
        config = self.weights["confidence_calibration"]
        winning = _number(self.signal_quality.get("winning_profile", {}).get("average_confidence"))
        losing = _number(self.signal_quality.get("losing_profile", {}).get("average_confidence"))
        gap = abs(winning - losing) if winning is not None and losing is not None else None
        adjustment = 0.0; reasons = []
        if gap is not None and gap < float(config["winning_losing_gap_reference"]):
            adjustment += float(config["weak_separation_penalty"])
            reasons.append(f"WEAK_WIN_LOSS_SEPARATION_{gap:.2f}")
        if raw >= 90:
            adjustment += float(config["high_confidence_penalty"]) * self._multiplier("confidence")
            reasons.append("HIGH_CONFIDENCE_LOW_PREDICTIVE_POWER")
        limit = float(self.weights["limits"]["maximum_absolute_confidence_change"])
        adjustment = max(-limit, min(limit, adjustment))
        calibrated = round(max(0.0, min(100.0, raw + adjustment)), 4)
        return {"raw_confidence": raw, "calibrated_confidence": calibrated,
                "confidence_change": round(calibrated-raw, 4),
                "calibration_reason": reasons or ["NO_CONFIDENCE_ADJUSTMENT"]}

    def calibrate_score(self, signal: Mapping[str, Any]) -> dict[str, Any]:
        raw = _number(signal.get("score"))
        if raw is None:
            return {"raw_score": None, "calibrated_score": None, "score_change": 0.0,
                    "score_adjustments": [{"reason":"UNKNOWN_SCORE_PRESERVED","amount":0.0}]}
        configured = self.weights["score_adjustments"]; adjustments=[]
        def add(key: str, feature: str) -> None:
            amount = float(configured[key]) * self._multiplier(feature)
            adjustments.append({"reason":key.upper(),"feature":feature,"predictive_power":self.power.get(feature,"NONE"),"amount":round(amount,4)})
        hour = _number(signal.get("hour"))
        if hour == 7: add("hour_07", "hour")
        if str(signal.get("quality","")).upper() == "B": add("quality_B", "quality")
        if 25 <= raw < 27: add("score_25_26", "score")
        confidence = _number(signal.get("confidence"))
        if confidence is not None and confidence >= 90: add("confidence_90_plus", "confidence")
        weekday = str(signal.get("weekday","")).title()
        if weekday in {"Monday","Tuesday"}: add(f"weekday_{weekday}", "weekday")
        if str(signal.get("symbol","")).upper().startswith("SOL"): add("symbol_SOL", "symbol")
        change = sum(item["amount"] for item in adjustments)
        limit = float(self.weights["limits"]["maximum_absolute_score_change"])
        change = max(-limit, min(limit, change)); calibrated=round(max(0.0,raw+change),4)
        return {"raw_score":raw,"calibrated_score":calibrated,"score_change":round(calibrated-raw,4),
                "score_adjustments":adjustments or [{"reason":"NO_SCORE_ADJUSTMENT","amount":0.0}]}

    def evaluate(self, signal: Mapping[str, Any]) -> dict[str, Any]:
        confidence=self.calibrate_confidence(signal.get("confidence")); score=self.calibrate_score(signal)
        accepted_v1=bool(signal.get("accepted_by_v1", True))
        min_conf=float(self.weights["acceptance"]["minimum_calibrated_confidence"])
        min_score=float(self.weights["acceptance"]["minimum_calibrated_score"])
        # Unknown research features never create a new shadow rejection.
        accepted_v2=accepted_v1 and (confidence["calibrated_confidence"] is None or confidence["calibrated_confidence"]>=min_conf) and (score["calibrated_score"] is None or score["calibrated_score"]>=min_score)
        reasons=confidence["calibration_reason"]+[item["reason"] for item in score["score_adjustments"]]
        explanation={"decision":"Signal accepted" if accepted_v2 else "Signal downgraded below v2 acceptance threshold",
                     "confidence":{"raw":confidence["raw_confidence"],"calibrated":confidence["calibrated_confidence"],"reasons":confidence["calibration_reason"]},
                     "score":{"raw":score["raw_score"],"calibrated":score["calibrated_score"],"adjustments":score["score_adjustments"]},
                     "safety":"SHADOW_ONLY; v1 signal and trading parameters unchanged"}
        return {**confidence,**score,"accepted_by_v1":accepted_v1,"accepted_by_v2":accepted_v2,
                "agreement":accepted_v1==accepted_v2,"reason_difference":reasons,"explanation":explanation}

    def _rows(self) -> list[dict[str, Any]]:
        if self._feature_rows is not None: return [dict(row) for row in self._feature_rows]
        return SignalQualityAnalyzer(registry=self.registry,base_dir=self.base_dir).collect_features()

    @staticmethod
    def _replay(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return metric_bundle(({**row,"replay_r":row.get("R")} for row in rows),"replay_r")

    def _walk_forward(self, comparisons: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        ordered=sorted(comparisons,key=lambda row:str(row.get("opened_at","")))
        windows=build_windows(len(ordered),target_windows=3); results=[]
        for window in windows:
            test=ordered[window.test_start:window.test_end]
            v1=self._replay(test); v2=self._replay([row for row in test if row["accepted_by_v2"]])
            results.append({"window":window.index,"train_range":[window.train_start,window.train_end],"test_range":[window.test_start,window.test_end],"v1":v1,"v2":v2,
                            "pf_improved":v2["profit_factor"]>v1["profit_factor"],"net_r_improved":v2["net_r"]>v1["net_r"]})
        improved=sum(item["pf_improved"] and item["net_r_improved"] for item in results)
        status="PROMISING" if results and improved>=math.ceil(len(results)*2/3) else ("REJECT" if results and improved==0 else "EXPERIMENTAL")
        return {"status":status,"windows":results,"improved_windows":improved,"total_windows":len(results),
                "method":"Existing chronological build_windows; v2 weights frozen for this research run"}

    def build_report(self) -> dict[str, Any]:
        rows=self._rows(); comparisons=[]
        for row in rows:
            decision=self.evaluate({**row,"accepted_by_v1":True}); comparisons.append({**row,**decision})
        v1=self._replay(comparisons); accepted=[row for row in comparisons if row["accepted_by_v2"]]; v2=self._replay(accepted)
        count=len(comparisons); agreement=sum(row["agreement"] for row in comparisons)
        walk=self._walk_forward(comparisons)
        loss_report=_read_json(self.base_dir/"reports/loss_attribution.json")
        existing_walk=_read_json(self.base_dir/"reports/walk_forward.json")
        dashboard=_read_json(self.base_dir/"reports/research_dashboard.json")
        execution=_read_json(self.base_dir/"reports/execution_simulator.json")
        adaptive=_read_json(self.base_dir/"adaptive_research_report.json")
        dashboard_recommendation=dashboard.get("main_recommendation",dashboard.get("recommendation"))
        ready_safety_gate=(execution.get("status")!="FRAGILE" and dashboard_recommendation!="FIX_DATA_QUALITY")
        if count<30: status="EXPERIMENTAL"
        elif v2["profit_factor"]>1 and v2["net_r"]>0 and walk["status"]=="PROMISING" and ready_safety_gate: status="READY_FOR_AB"
        elif v2["profit_factor"]>v1["profit_factor"] and v2["net_r"]>v1["net_r"] and walk["status"]=="PROMISING": status="PROMISING"
        elif v2["profit_factor"]<=v1["profit_factor"] and v2["net_r"]<=v1["net_r"]: status="REJECT"
        else: status="EXPERIMENTAL"
        reason_counts=Counter(reason for row in comparisons for reason in row["reason_difference"])
        fingerprint_payload={"weights":self.weights,"signal_quality_fingerprint":self.signal_quality.get("dataset_fingerprint"),
                             "results":[{"trade_id":x.get("trade_id"),"v2":x["accepted_by_v2"],"score":x["calibrated_score"],"confidence":x["calibrated_confidence"]} for x in comparisons]}
        fingerprint=hashlib.sha256(json.dumps(fingerprint_payload,sort_keys=True,default=str).encode()).hexdigest()
        return {"generated_at":datetime.now(timezone.utc).isoformat(),"mode":"SHADOW_RESEARCH_ONLY","status":status,"dataset_fingerprint":fingerprint,
                "signals":{"compared":count,"accepted_by_v1":sum(x["accepted_by_v1"] for x in comparisons),"accepted_by_v2":len(accepted),"rejected_by_v2":count-len(accepted),
                           "agreement_pct":round(agreement/count*100,4) if count else 0,"disagreement_pct":round((count-agreement)/count*100,4) if count else 0,
                           "average_confidence_change":round(sum(x["confidence_change"] for x in comparisons)/count,4) if count else 0,
                           "average_score_change":round(sum(x["score_change"] for x in comparisons)/count,4) if count else 0},
                "top_recalibration_reasons":[{"reason":key,"signals":value} for key,value in reason_counts.most_common(10)],
                "shadow_replay":{"v1":v1,"v2":v2,"method":"Existing shadow_replay.metrics.metric_bundle over COMPLETE historical R"},
                "walk_forward":walk,"comparisons":comparisons,
                "research_evidence":{
                    "signal_quality":{"score":self.signal_quality.get("signal_quality_score"),"status":self.signal_quality.get("signal_quality_status"),"top_features":[item.get("feature") for item in self.signal_quality.get("feature_ranking",[])[:5]]},
                    "loss_attribution":{"status":loss_report.get("status"),"top_scenarios":[item.get("segment") for item in loss_report.get("top_loss_scenarios",[])[:5]]},
                    "existing_walk_forward":{"status":existing_walk.get("status"),"generated_at":existing_walk.get("generated_at")},
                    "research_dashboard":{"status":dashboard.get("system_research_status",{}).get("status",dashboard.get("status")),"recommendation":dashboard_recommendation},
                    "execution_simulator":{"status":execution.get("status"),"robustness_score":execution.get("robustness_score")},
                    "adaptive_research":{"status":adaptive.get("status"),"global_confidence":adaptive.get("global_confidence")},
                    "trade_registry":{"complete_trades":count},
                    "ready_for_ab_safety_gate":ready_safety_gate,
                },
                "sources":{"signal_quality":bool(self.signal_quality),"loss_attribution":bool(_read_json(self.base_dir/"reports/loss_attribution.json")),
                           "walk_forward":bool(_read_json(self.base_dir/"reports/walk_forward.json")),"research_dashboard":bool(_read_json(self.base_dir/"reports/research_dashboard.json")),
                           "execution_simulator":bool(_read_json(self.base_dir/"reports/execution_simulator.json")),"trade_registry":True,
                           "adaptive_research":bool(_read_json(self.base_dir/"adaptive_research_report.json"))},
                "weights":self.weights,"recommendation":"Continue Shadow Testing" if status in {"EXPERIMENTAL","PROMISING"} else ("Prepare controlled A/B review" if status=="READY_FOR_AB" else "Reject current v2 calibration"),
                "restrictions":["NO_LIVE_IMPORT","NO_AUTOMATIC_PROMOTION","V1_UNCHANGED","NO_SIGNAL_OR_TRADE_PARAMETER_MUTATION"]}

    def write_reports(self) -> tuple[dict[str,Any],bool]:
        report=self.build_report(); _atomic_write(self.report_path,json.dumps(report,ensure_ascii=False,indent=2)+"\n"); _atomic_write(self.summary_path,format_decision_v2(report)+"\n")
        self.history_dir.mkdir(parents=True,exist_ok=True); duplicate=any(_read_json(path).get("dataset_fingerprint")==report["dataset_fingerprint"] for path in self.history_dir.glob("*.json"))
        if not duplicate:
            stamp=datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f"); _atomic_write(self.history_dir/f"{stamp}.json",json.dumps(report,ensure_ascii=False,indent=2)+"\n")
        return report,not duplicate


def load_decision_v2(path: str | Path=REPORT_PATH) -> dict[str,Any]: return _read_json(Path(path))


def format_decision_v2(report: Mapping[str,Any],view: str="") -> str:
    signals=report.get("signals",{}); replay=report.get("shadow_replay",{}); v1=replay.get("v1",{}); v2=replay.get("v2",{}); walk=report.get("walk_forward",{})
    lines=["🧪 DecisionEngine v2",f"Status: {report.get('status','EXPERIMENTAL')}",f"Signals Compared: {signals.get('compared',0)}",f"Agreement: {_number(signals.get('agreement_pct'),0):.1f}%",f"Average Confidence Δ: {_number(signals.get('average_confidence_change'),0):+.2f}",f"Average Score Δ: {_number(signals.get('average_score_change'),0):+.2f}",f"Replay PF: {_number(v1.get('profit_factor'),0):.3f} → {_number(v2.get('profit_factor'),0):.3f}",f"Replay Net R: {_number(v1.get('net_r'),0):+.2f} → {_number(v2.get('net_r'),0):+.2f}",f"Walk Forward: {walk.get('status','EXPERIMENTAL')}"]
    if view in {"compare","report"}: lines.extend([f"Accepted v1/v2: {signals.get('accepted_by_v1',0)} / {signals.get('accepted_by_v2',0)}",f"Disagreement: {_number(signals.get('disagreement_pct'),0):.1f}%",f"V1/V2 Drawdown: {_number(v1.get('max_drawdown_r'),0):.2f}R / {_number(v2.get('max_drawdown_r'),0):.2f}R"])
    lines.extend([f"Recommendation: {report.get('recommendation','Continue Shadow Testing')}","v1 and LIVE remain unchanged."]); return "\n".join(lines)


def main() -> None:
    report,snapshot=DecisionEngineV2().write_reports(); print(format_decision_v2(report,"report")); print(f"History snapshot: {'created' if snapshot else 'deduplicated'}")


if __name__=="__main__": main()
