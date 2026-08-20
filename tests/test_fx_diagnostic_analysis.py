"""Offline regression coverage for FX conditional-edge diagnostics."""

from __future__ import annotations

import json
from pathlib import Path

from fx_research import diagnostic_analysis
from fx_research.diagnostic_analysis import analyze_outcomes, main


def _outcome(*, strategy: str = "FX_RISK_CONSERVATIVE", split: str = "TRAIN", pnl: float | None = 1.0,
             symbol: str = "EUR/USD", direction: str = "LONG", session: str = "LONDON",
             regime: str = "TRENDING", atr_pct: float = 1.0, rsi: float = 60.0,
             index: int = 0, status: str = "CLOSED") -> dict[str, object]:
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


def _qualified(*, strategy: str, direction: str, pnl: float) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for offset, (split, count) in enumerate((("TRAIN", 100), ("VALIDATION", 40), ("HOLDOUT", 40))):
        rows.extend(
            _outcome(
                strategy=strategy,
                split=split,
                direction=direction,
                pnl=(-1.0 if pnl > 0 and index % 4 == 0 else pnl),
                index=offset * 200 + index,
            )
            for index in range(count)
        )
    return rows


def _group(report: dict[str, object], *, group_type: str, label: str) -> dict[str, object]:
    return next(item for item in report["diagnostic_groups"] if item["group_type"] == group_type and item["label"] == label)  # type: ignore[index, return-value]


def test_train_quantiles_are_frozen_and_never_leak_validation_or_holdout() -> None:
    base = [
        _outcome(split="TRAIN", atr_pct=value, rsi=50 + value, index=index)
        for index, value in enumerate((1.0, 2.0, 3.0, 4.0, 5.0, 6.0))
    ]
    report = analyze_outcomes(base + [_outcome(split="VALIDATION", atr_pct=7.0, rsi=57.0, index=10), _outcome(split="HOLDOUT", atr_pct=8.0, rsi=58.0, index=11)])
    leaked = analyze_outcomes(base + [_outcome(split="VALIDATION", atr_pct=9_999.0, rsi=9_999.0, index=10), _outcome(split="HOLDOUT", atr_pct=8_888.0, rsi=8_888.0, index=11)])
    boundaries = report["train_bucket_boundaries"]["FX_RISK_CONSERVATIVE"]  # type: ignore[index]
    assert boundaries == leaked["train_bucket_boundaries"]["FX_RISK_CONSERVATIVE"]  # type: ignore[index]
    assert boundaries["volatility"]["source_split"] == "TRAIN"  # type: ignore[index]
    assert boundaries["trend_strength"]["field"] == "rsi_distance_from_50"  # type: ignore[index]


def test_groups_include_required_interactions_and_ambiguous_rows_are_not_resolved() -> None:
    rows = [
        _outcome(index=0),
        _outcome(direction="SHORT", session="ASIA", regime="RANGING", index=1),
        _outcome(status="AMBIGUOUS_INTRABAR", pnl=None, index=2),
    ]
    report = analyze_outcomes(rows)
    types = {item["group_type"] for item in report["diagnostic_groups"]}  # type: ignore[index]
    assert {"strategy × symbol × direction", "strategy × session × direction", "strategy × regime × direction"} <= types
    direction = _group(report, group_type="strategy × direction", label="direction=LONG")
    metrics = direction["splits"]["TRAIN"]  # type: ignore[index]
    assert metrics["trades"] == 2 and metrics["resolved_trades"] == 1 and metrics["ambiguous"] == 1  # type: ignore[index]


def test_stability_gates_and_deterministic_shortlist_are_descriptive() -> None:
    promising = _qualified(strategy="FX_RISK_CONSERVATIVE", direction="LONG", pnl=1.0)
    negative = _qualified(strategy="FX_TREND_CONFIRM", direction="SHORT", pnl=-1.0)
    first, second = analyze_outcomes(promising + negative), analyze_outcomes(promising + negative)
    candidate = _group(first, group_type="strategy × direction", label="direction=LONG")
    rejected = _group(first, group_type="strategy × direction", label="direction=SHORT")
    assert candidate["classification"] == "PROMISING_CONDITIONAL_EDGE"  # type: ignore[index]
    assert rejected["classification"] == "NEGATIVE"  # type: ignore[index]
    assert first["research_verdict"] == "CONDITIONAL_EDGE_CANDIDATES_FOUND"
    assert first["shortlist"] == second["shortlist"]  # type: ignore[index]
    assert len(first["shortlist"]) <= 10  # type: ignore[arg-type]


def test_minimum_sample_gate_blocks_small_positive_groups_and_preserves_inputs() -> None:
    rows = [_outcome(index=index) for index in range(10)]
    before = json.dumps(rows, sort_keys=True)
    report = analyze_outcomes(rows)
    group = _group(report, group_type="strategy × direction", label="direction=LONG")
    assert group["classification"] == "INSUFFICIENT_SAMPLE"  # type: ignore[index]
    assert report["multiple_testing"]["groups_meeting_minimum_sample"] == 0  # type: ignore[index]
    assert json.dumps(rows, sort_keys=True) == before


def test_json_output_is_opt_in_and_no_runtime_or_crypto_files_are_created(tmp_path: Path, monkeypatch) -> None:
    report = {"outcomes": {}, "multiple_testing": {"groups_evaluated": 0, "groups_meeting_minimum_sample": 0, "promising_conditional_edge_groups": 0}, "shortlist": [], "research_verdict": "NO_CONDITIONAL_EDGE"}
    monkeypatch.setattr(diagnostic_analysis, "build_report", lambda **_: report)
    output = tmp_path / "diagnostic.json"
    assert main(["--eurusd", "ignored.csv", "--gbpusd", "ignored.csv"]) == 0
    assert not output.exists()
    assert main(["--eurusd", "ignored.csv", "--gbpusd", "ignored.csv", "--json-output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["research_verdict"] == "NO_CONDITIONAL_EDGE"
    assert not any((tmp_path / name).exists() for name in ("research.db", "fx_research.db", "runtime_snapshot.json", "fx_shadow_open.json"))
