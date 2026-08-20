"""Regression coverage for read-only FX pre-registered hypothesis diagnosis."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from fx_research import research_diagnosis_v2
from fx_research.research_diagnosis_v2 import analyze_outcomes, main


def _outcome(
    *,
    strategy: str = "FX_RISK_CONSERVATIVE",
    split: str = "TRAIN",
    pnl: float | None = 1.0,
    symbol: str = "EUR/USD",
    direction: str = "LONG",
    session: str = "LONDON",
    regime: str = "TRENDING",
    atr_pct: float = 1.0,
    rsi: float = 60.0,
    index: int = 0,
    status: str = "CLOSED",
) -> dict[str, object]:
    return {
        "strategy_id": strategy,
        "symbol": symbol,
        "side": direction,
        "split": split,
        "entry_time": f"2024-01-{(index // 24) + 1:02d}T{index % 24:02d}:00:00+00:00",
        "status": status,
        "pnl_r": pnl,
        "feature_snapshot": {
            "symbol": symbol,
            "session": session,
            "market_regime": regime,
            "atr_pct": atr_pct,
            "rsi": rsi,
        },
    }


def _split_rows(*, strategy: str, symbol: str, direction: str, values: list[float], base: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for offset, (split, count) in enumerate((("TRAIN", 75), ("VALIDATION", 30), ("HOLDOUT", 30))):
        rows.extend(
            _outcome(
                strategy=strategy,
                symbol=symbol,
                direction=direction,
                split=split,
                pnl=values[index % len(values)],
                atr_pct=1.0 + (index % 3),
                rsi=55.0 + (index % 3),
                index=base + offset * 100 + index,
            )
            for index in range(count)
        )
    return rows


def _candidate_fixture() -> list[dict[str, object]]:
    # Both symbols: 150/60/60 globally, each with positive/negative outcomes.
    return (
        _split_rows(strategy="FX_RISK_CONSERVATIVE", symbol="EUR/USD", direction="LONG", values=[1.0, 1.0, 1.0, -1.0], base=0)
        + _split_rows(strategy="FX_RISK_CONSERVATIVE", symbol="GBP/USD", direction="LONG", values=[1.0, 1.0, 1.0, -1.0], base=1000)
    )


def _group(report: dict[str, object], dimensions: list[str], condition: dict[str, str]) -> dict[str, object]:
    return next(item for item in report["groups"] if item["dimensions"] == dimensions and item["condition"] == condition)  # type: ignore[index, return-value]


def test_train_boundaries_are_frozen_and_validation_holdout_cannot_leak() -> None:
    rows = _candidate_fixture()
    changed = copy.deepcopy(rows)
    for row in changed:
        if row["split"] != "TRAIN":
            row["feature_snapshot"]["atr_pct"] = 9999.0  # type: ignore[index]
            row["feature_snapshot"]["rsi"] = 9999.0  # type: ignore[index]
    first, second = analyze_outcomes(rows), analyze_outcomes(changed)
    assert first["bucket_boundaries"] == second["bucket_boundaries"]  # type: ignore[index]
    assert first["bucket_boundaries"]["FX_RISK_CONSERVATIVE"]["volatility"]["source_split"] == "TRAIN"  # type: ignore[index]
    assert first["bucket_boundaries"]["FX_RISK_CONSERVATIVE"]["momentum_proxy"]["field"] == "RSI_DISTANCE_PROXY"  # type: ignore[index]


def test_failure_attribution_sums_trade_and_loss_contributions_by_dimension() -> None:
    rows = _candidate_fixture() + _split_rows(
        strategy="FX_RISK_CONSERVATIVE",
        symbol="EUR/USD",
        direction="SHORT",
        values=[-1.0, 1.0],
        base=3000,
    )
    report = analyze_outcomes(rows)
    direction = [item for item in report["failure_attribution"]["FX_RISK_CONSERVATIVE"] if item["dimension"] == "direction"]  # type: ignore[index]
    assert len(direction) == 2
    for split in ("TRAIN", "VALIDATION", "HOLDOUT"):
        assert sum(item["splits"][split]["trade_share_pct"] for item in direction) == 100.0  # type: ignore[index]
        assert sum(item["splits"][split]["loss_contribution_pct"] for item in direction) == 100.0  # type: ignore[index]


def test_only_pre_registered_interactions_are_evaluated_and_ambiguous_is_excluded() -> None:
    rows = _candidate_fixture() + [_outcome(status="AMBIGUOUS_INTRABAR", pnl=None, index=3000)]
    report = analyze_outcomes(rows)
    actual = {tuple(item["dimensions"]) for item in report["groups"]}  # type: ignore[index]
    assert actual <= {
        ("direction",),
        ("session",),
        ("regime",),
        ("volatility_bucket",),
        ("symbol",),
        ("direction", "session"),
        ("direction", "regime"),
    }
    group = _group(report, ["direction"], {"direction": "LONG"})
    assert group["splits"]["TRAIN"]["ambiguous"] == 1  # type: ignore[index]
    assert group["splits"]["TRAIN"]["resolved_trades"] == 150  # type: ignore[index]


def test_gates_robustness_and_cross_symbol_rule_are_deterministic() -> None:
    rows = _candidate_fixture()
    first, second = analyze_outcomes(rows), analyze_outcomes(rows)
    candidate = _group(first, ["direction"], {"direction": "LONG"})
    symbol_only = _group(first, ["symbol"], {"symbol": "EUR/USD"})
    assert candidate["gate_results"] == {"sample": True, "safety_floor": True, "improvement": False, "cross_symbol_stable": True}  # type: ignore[index]
    assert candidate["robustness"] == _group(second, ["direction"], {"direction": "LONG"})["robustness"]  # type: ignore[index]
    assert symbol_only["gate_results"]["cross_symbol_stable"] is False  # type: ignore[index]
    assert not first["hypotheses"] and first["verdict"] == "NO_ROBUST_HYPOTHESIS"  # type: ignore[index]


def test_hypotheses_are_limited_to_three_and_require_improvement() -> None:
    rows = _candidate_fixture()
    # Make baseline worse with a pre-defined non-candidate SHORT population.
    rows += _split_rows(strategy="FX_RISK_CONSERVATIVE", symbol="EUR/USD", direction="SHORT", values=[-1.0, -1.0, 1.0], base=4000)
    rows += _split_rows(strategy="FX_RISK_CONSERVATIVE", symbol="GBP/USD", direction="SHORT", values=[-1.0, -1.0, 1.0], base=5000)
    report = analyze_outcomes(rows)
    assert len(report["hypotheses"]) <= 3  # type: ignore[arg-type]
    assert report["hypotheses"] and report["verdict"] == "HYPOTHESES_READY_FOR_FROZEN_TEST"  # type: ignore[index]
    assert all(item["gate_results"]["improvement"] for item in report["hypotheses"])  # type: ignore[index]


def test_json_output_is_opt_in_and_input_rows_are_not_mutated(tmp_path: Path, monkeypatch) -> None:
    rows = _candidate_fixture()
    original = json.dumps(rows, sort_keys=True)
    analyze_outcomes(rows)
    assert json.dumps(rows, sort_keys=True) == original
    report = {
        "baseline_metrics": {},
        "hypotheses": [],
        "verdict": "NO_ROBUST_HYPOTHESIS",
    }
    monkeypatch.setattr(research_diagnosis_v2, "build_report", lambda **_: report)
    output = tmp_path / "diagnosis.json"
    assert main(["--eurusd", "ignored.csv", "--gbpusd", "ignored.csv"]) == 0
    assert not output.exists()
    assert main(["--eurusd", "ignored.csv", "--gbpusd", "ignored.csv", "--json-output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["verdict"] == "NO_ROBUST_HYPOTHESIS"
