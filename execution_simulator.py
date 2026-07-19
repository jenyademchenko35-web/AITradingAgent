"""Read-only execution-cost and fill simulator for completed trades."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trade_registry import TradeRegistry

BASE_DIR = Path(__file__).resolve().parent
REPORT_PATH = BASE_DIR / "reports" / "execution_simulator.json"
SUMMARY_PATH = BASE_DIR / "reports" / "execution_simulator_summary.txt"
HISTORY_DIR = BASE_DIR / "reports" / "execution_history"

MODE_SETTINGS = {
    "NORMAL": {"slippage_pct": 0.01, "max_slippage_pct": 0.10, "fee_multiplier": 1.0, "delay_ms": 250, "fill_pct": 100, "gap_pct": 0.0},
    "STRESS": {"slippage_pct": 0.05, "max_slippage_pct": 0.10, "fee_multiplier": 1.5, "delay_ms": 500, "fill_pct": 75, "gap_pct": 0.03},
    "EXTREME": {"slippage_pct": 0.15, "max_slippage_pct": 0.15, "fee_multiplier": 2.0, "delay_ms": 1000, "fill_pct": 50, "gap_pct": 0.10},
}
MAKER_FEE = 0.02
TAKER_FEE = 0.055
DELAY_SCENARIOS_MS = (0, 250, 500, 1000)
FILL_SCENARIOS_PCT = (100, 75, 50, 25)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _metrics(values: Sequence[float]) -> dict[str, Any]:
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_loss = abs(sum(losses))
    pf = sum(wins) / gross_loss if gross_loss else (999.0 if wins else 0.0)
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values),
        "profit_factor": round(pf, 6),
        "net_r": round(sum(values), 6),
        "winrate_pct": round(len(wins) / len(values) * 100, 6) if values else 0.0,
        "average_r": round(sum(values) / len(values), 6) if values else 0.0,
        "max_drawdown_r": round(drawdown, 6),
    }


class ExecutionSimulator:
    """Apply deterministic adverse execution assumptions without mutations."""

    def __init__(
        self,
        *,
        registry: TradeRegistry | None = None,
        base_dir: str | Path = BASE_DIR,
        report_path: str | Path = REPORT_PATH,
        summary_path: str | Path = SUMMARY_PATH,
        history_dir: str | Path = HISTORY_DIR,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.registry = registry or TradeRegistry(self.base_dir / "trades.csv")
        self.report_path = Path(report_path)
        self.summary_path = Path(summary_path)
        self.history_dir = Path(history_dir)

    def simulate_trade(
        self,
        trade: Mapping[str, Any],
        *,
        mode: str = "NORMAL",
        fill_pct: float | None = None,
        delay_ms: int | None = None,
        order_type: str = "TAKER",
        gap_direction: str | None = None,
    ) -> dict[str, Any]:
        source = deepcopy(dict(trade))
        settings = dict(MODE_SETTINGS[mode.upper()])
        fill = _num(fill_pct, settings["fill_pct"]) if fill_pct is not None else settings["fill_pct"]
        delay = int(delay_ms if delay_ms is not None else settings["delay_ms"])
        direction = str(source.get("direction", "LONG")).upper()
        sign = 1.0 if direction == "LONG" else -1.0
        entry = _num(source.get("entry"))
        exit_price = _num(source.get("exit", source.get("exit_price")))
        stop = _num(source.get("sl", source.get("stop_loss")))
        risk_per_unit = abs(entry - stop) or _num(source.get("risk_per_unit"))
        original_r = _num(source.get("R", source.get("r", source.get("pnl_r"))))

        slippage_pct = min(settings["slippage_pct"], settings["max_slippage_pct"])
        # With no tick API, delay is an explicit adverse price proxy: 0.01%/250ms.
        delay_impact_pct = delay / 250.0 * 0.01 if delay > 0 else 0.0
        gap_pct = settings["gap_pct"]
        if gap_direction == "UP":
            gap_sign = 1.0
        elif gap_direction == "DOWN":
            gap_sign = -1.0
        else:
            gap_sign = sign  # adverse entry gap
        adverse_entry_pct = sign * (slippage_pct + delay_impact_pct) + gap_sign * gap_pct
        executed_entry = entry * (1 + adverse_entry_pct / 100.0)

        fee_rate_pct = (MAKER_FEE if order_type.upper() == "MAKER" else TAKER_FEE) * settings["fee_multiplier"]
        commission_per_unit = (abs(executed_entry) + abs(exit_price)) * fee_rate_pct / 100.0
        gross_executed_r = ((exit_price - executed_entry) * sign / risk_per_unit) if risk_per_unit else original_r
        fee_r = commission_per_unit / risk_per_unit if risk_per_unit else 0.0
        fill_fraction = max(0.0, min(100.0, fill)) / 100.0
        adjusted_r = (gross_executed_r - fee_r) * fill_fraction
        original_pnl = _num(source.get("pnl"), original_r * risk_per_unit)
        adjusted_pnl = ((exit_price - executed_entry) * sign - commission_per_unit) * fill_fraction

        return {
            "trade_id": source.get("trade_id"),
            "symbol": source.get("symbol"),
            "direction": direction,
            "original_entry": round(entry, 10),
            "requested_entry": round(entry, 10),
            "executed_entry": round(executed_entry, 10),
            "exit": round(exit_price, 10),
            "commission": round(commission_per_unit * fill_fraction, 10),
            "fee_pct": round(fee_rate_pct, 6),
            "slippage_pct": round(slippage_pct, 6),
            "execution_delay_ms": delay,
            "delay_impact_pct": round(delay_impact_pct, 6),
            "fill_pct": round(fill, 2),
            "gap": "GAP_UP" if gap_sign > 0 and gap_pct else ("GAP_DOWN" if gap_pct else "NONE"),
            "gap_pct": round(gap_pct, 6),
            "original_r": round(original_r, 6),
            "adjusted_r": round(adjusted_r, 6),
            "original_pnl": round(original_pnl, 6),
            "adjusted_pnl": round(adjusted_pnl, 6),
        }

    @staticmethod
    def robustness_score(original: Mapping[str, Any], simulated: Mapping[str, Any]) -> int:
        original_pf = max(_num(original.get("profit_factor")), 0.01)
        pf_loss = max(0.0, (original_pf - _num(simulated.get("profit_factor"))) / original_pf)
        original_net = _num(original.get("net_r"))
        simulated_net = _num(simulated.get("net_r"))
        net_loss = max(0.0, original_net - simulated_net) / max(abs(original_net), 1.0)
        original_dd = max(_num(original.get("max_drawdown_r")), 0.01)
        dd_growth = max(0.0, _num(simulated.get("max_drawdown_r")) - original_dd) / original_dd
        degradation = min(1.0, pf_loss * 0.4 + net_loss * 0.4 + dd_growth * 0.2)
        return round((1.0 - degradation) * 100)

    def run(self, mode: str = "NORMAL") -> dict[str, Any]:
        selected_mode = mode.upper()
        if selected_mode not in MODE_SETTINGS:
            raise ValueError(f"Unknown execution mode: {mode}")
        trades = self.registry.get_complete_trades()
        simulated = [self.simulate_trade(row, mode=selected_mode) for row in trades]
        original = _metrics([_num(row.get("R", row.get("r", row.get("pnl_r")))) for row in trades])
        adjusted = _metrics([_num(row.get("adjusted_r")) for row in simulated])
        base_score = self.robustness_score(original, adjusted)
        # A smaller fill can make a losing sample look numerically better merely
        # because less capital was executed. Treat unfilled volume as fragility.
        fill_penalty = round((100.0 - _num(MODE_SETTINGS[selected_mode]["fill_pct"])) / 100.0 * 30)
        score = max(0, base_score - fill_penalty)
        verdict = "ROBUST" if score >= 75 else ("ACCEPTABLE" if score >= 50 else "FRAGILE")
        fingerprint_payload = {
            "mode": selected_mode,
            "settings": MODE_SETTINGS[selected_mode],
            "trades": [{"trade_id": row.get("trade_id"), "R": row.get("R"), "entry": row.get("entry"), "exit": row.get("exit")} for row in trades],
        }
        fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True, default=str).encode()).hexdigest()
        portfolio = _read_json(self.base_dir / "reports" / "portfolio_manager.json")
        dashboard = _read_json(self.base_dir / "reports" / "research_dashboard.json")
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": selected_mode,
            "status": verdict,
            "robustness_score": score,
            "robustness_breakdown": {
                "metrics_degradation_score": base_score,
                "partial_fill_penalty": fill_penalty,
                "final_score": score,
            },
            "dataset_fingerprint": fingerprint,
            "settings": {**MODE_SETTINGS[selected_mode], "maker_fee_pct": MAKER_FEE, "taker_fee_pct": TAKER_FEE, "delay_scenarios_ms": list(DELAY_SCENARIOS_MS), "fill_scenarios_pct": list(FILL_SCENARIOS_PCT)},
            "sample": {"trades": len(trades), "source": "TradeRegistry.get_complete_trades"},
            "original_metrics": original,
            "simulated_metrics": adjusted,
            "execution_impact": {
                "average_slippage_pct": round(sum(row["slippage_pct"] for row in simulated) / len(simulated), 6) if simulated else 0.0,
                "average_fee_pct": round(sum(row["fee_pct"] for row in simulated) / len(simulated), 6) if simulated else 0.0,
                "net_r_change": round(adjusted["net_r"] - original["net_r"], 6),
                "pf_change": round(adjusted["profit_factor"] - original["profit_factor"], 6),
            },
            "source_health": {"trade_registry": "OK", "portfolio_manager": "OK" if portfolio else "NOT_AVAILABLE", "research_dashboard": "OK" if dashboard else "NOT_AVAILABLE"},
            "portfolio_context": {"status": portfolio.get("status"), "open_trades": portfolio.get("open_trades")},
            "research_context": {"status": dashboard.get("system_research_status", {}).get("status")},
            "trades": simulated,
            "restrictions": ["READ_ONLY", "NO_LIVE_APPLICATION", "NO_SIGNAL_OR_TRADE_MUTATION"],
        }

    def write_reports(self, mode: str = "NORMAL") -> tuple[dict[str, Any], bool]:
        report = self.run(mode)
        _atomic_write(self.report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(self.summary_path, format_execution(report) + "\n")
        self.history_dir.mkdir(parents=True, exist_ok=True)
        duplicate = False
        for path in self.history_dir.glob("*.json"):
            if _read_json(path).get("dataset_fingerprint") == report["dataset_fingerprint"]:
                duplicate = True
                break
        if not duplicate:
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
            _atomic_write(self.history_dir / f"{stamp}.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report, not duplicate


def format_execution(report: Mapping[str, Any], view: str = "") -> str:
    original = report.get("original_metrics", {})
    simulated = report.get("simulated_metrics", {})
    impact = report.get("execution_impact", {})
    lines = ["⚙️ Execution Simulator", f"Trades: {report.get('sample', {}).get('trades', 0)}", f"Mode: {report.get('mode', 'UNKNOWN')}"]
    if view != "summary":
        lines.extend([f"Original PF: {_num(original.get('profit_factor')):.3f}", f"Simulated PF: {_num(simulated.get('profit_factor')):.3f}", f"Original Net R: {_num(original.get('net_r')):.3f}", f"Simulated Net R: {_num(simulated.get('net_r')):.3f}"])
    lines.extend([f"Average Slippage: {_num(impact.get('average_slippage_pct')):.3f}%", f"Average Fee: {_num(impact.get('average_fee_pct')):.3f}%", f"Robustness: {report.get('robustness_score', 0)}", f"Verdict: {report.get('status', 'UNKNOWN')}", "Read-only simulation. LIVE remains unchanged."])
    return "\n".join(lines)


def load_execution_report(path: str | Path = REPORT_PATH) -> dict[str, Any]:
    return _read_json(Path(path))


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Execution Simulator")
    parser.add_argument("--mode", choices=tuple(MODE_SETTINGS), default="NORMAL")
    args = parser.parse_args()
    report, snapshot = ExecutionSimulator().write_reports(args.mode)
    print(format_execution(report))
    print(f"History snapshot: {'created' if snapshot else 'deduplicated'}")


if __name__ == "__main__":
    main()
