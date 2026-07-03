"""Trade Pattern Discovery Framework.

This read-only module mines repeated combinations of trade features that are
overrepresented in WIN or LOSS outcomes. It does not change DecisionEngine,
strategy logic, weights, config.py, live agent behavior, or any trading action.
"""

from __future__ import annotations

import csv
import itertools
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


BASE_DIR = Path(__file__).resolve().parent
TRADES_FILE = BASE_DIR / "trades.csv"
DEBUG_FILE = BASE_DIR / "decision_debug.csv"
DIAGNOSTICS_FILE = BASE_DIR / "decision_diagnostics.csv"
EXPLANATIONS_FILE = BASE_DIR / "decision_explanations.csv"
SIGNALS_FILE = BASE_DIR / "signals_v3.csv"
COMPARATOR_REPORT = BASE_DIR / "trade_comparator_report.json"
POST_LOSS_REPORT = BASE_DIR / "post_loss_decomposition_report.json"

REPORT_JSON = BASE_DIR / "trade_pattern_discovery_report.json"
SUMMARY_TEXT = BASE_DIR / "trade_pattern_discovery_summary.txt"
PATTERNS_CSV = BASE_DIR / "trade_pattern_discovery_patterns.csv"

ENGINES: Sequence[str] = ("Trend", "Structure", "Momentum", "Risk")
PATTERN_SIZES: Sequence[int] = (2, 3, 4)
MIN_SUPPORT = 2
TOP_LIMIT = 10


def utc_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a value to float."""
    try:
        text = str(value).replace("%", "").strip()
        if not text:
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def parse_time(value: Any) -> Optional[datetime]:
    """Parse ISO timestamp into UTC datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read CSV rows, skipping empty rows and repeated headers."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    clean_rows = []
    for row in rows:
        if not row or not any(row.values()):
            continue
        if row.get("timestamp") == "timestamp":
            continue
        clean_rows.append(row)
    return clean_rows


def read_json(path: Path) -> Dict[str, Any]:
    """Read JSON object from disk."""
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON report."""
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)


def mean(values: Iterable[float]) -> float:
    """Return arithmetic mean."""
    clean = [value for value in values if value is not None]
    return sum(clean) / len(clean) if clean else 0.0


def pct(part: int, total: int) -> float:
    """Return percentage."""
    if total <= 0:
        return 0.0
    return round(part / total * 100, 2)


def group_by_symbol(rows: Iterable[Mapping[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """Group rows by symbol and sort by timestamp."""
    grouped: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        symbol = row.get("symbol", "")
        if symbol:
            grouped[symbol].append(dict(row))
    for symbol_rows in grouped.values():
        symbol_rows.sort(key=lambda item: item.get("timestamp", ""))
    return grouped


def nearest_absolute(
    rows: Iterable[Mapping[str, str]],
    target_time: Optional[datetime],
    max_seconds: int,
) -> Optional[Dict[str, str]]:
    """Find closest row around target_time within max_seconds."""
    if target_time is None:
        return None
    best_row = None
    best_delta = None
    for row in rows:
        row_time = parse_time(row.get("timestamp"))
        if row_time is None:
            continue
        delta = abs((row_time - target_time).total_seconds())
        if delta <= max_seconds and (best_delta is None or delta < best_delta):
            best_delta = delta
            best_row = dict(row)
    return best_row


def nearest_before(
    rows: Iterable[Mapping[str, str]],
    target_time: Optional[datetime],
    direction: str = "",
    max_seconds: int = 3600,
) -> Optional[Dict[str, str]]:
    """Find nearest row at or before target_time."""
    if target_time is None:
        return None
    best_row = None
    best_time = None
    for row in rows:
        row_time = parse_time(row.get("timestamp"))
        if row_time is None or row_time > target_time:
            continue
        if direction and row.get("direction") not in {"", direction, "NEUTRAL"}:
            continue
        if (target_time - row_time).total_seconds() > max_seconds:
            continue
        if best_time is None or row_time > best_time:
            best_time = row_time
            best_row = dict(row)
    return best_row


def bin_numeric(value: float, boundaries: Sequence[Tuple[str, float, float]]) -> str:
    """Return label for numeric boundaries."""
    for label, low, high in boundaries:
        if low <= value < high:
            return label
    return "N/A"


def confidence_bucket(confidence: float) -> str:
    """Return detailed confidence bucket."""
    return bin_numeric(
        confidence,
        (
            ("<60", 0, 60),
            ("60-70", 60, 70),
            ("70-80", 70, 80),
            ("80-90", 80, 90),
            ("90-95", 90, 95),
            ("95+", 95, 101),
        ),
    )


def score_bucket(score: float) -> str:
    """Return score bucket."""
    return bin_numeric(
        score,
        (
            ("<20", 0, 20),
            ("20-25", 20, 25),
            ("25-27", 25, 27),
            ("27-30", 27, 30),
            ("30+", 30, 999),
        ),
    )


def value_bucket(value: float, prefix: str) -> str:
    """Return generic score-like bucket."""
    return f"{prefix}={score_bucket(value)}"


def age_bucket(minutes: float) -> str:
    """Return signal age bucket."""
    if minutes <= 5:
        return "age=0-5m"
    if minutes <= 30:
        return "age=5-30m"
    if minutes <= 120:
        return "age=30-120m"
    return "age=120m+"


def rr_bucket(rr: float) -> str:
    """Return RR bucket."""
    if rr < 1.5:
        return "<1.5"
    if rr < 2.2:
        return "1.5-2.2"
    return "2.2+"


def sl_atr_bucket(sl_atr: float) -> str:
    """Return stop-distance-in-ATR bucket."""
    if sl_atr <= 0:
        return "sl_atr=unknown"
    if sl_atr < 0.9:
        return "sl_atr=tight"
    if sl_atr <= 1.8:
        return "sl_atr=normal"
    return "sl_atr=wide"


def local_trend_from_debug(debug_row: Optional[Mapping[str, str]]) -> str:
    """Extract 1h EMA trend from decision_debug trend_reason."""
    if debug_row is None:
        return "UNKNOWN"
    trend_reason = str(debug_row.get("trend_reason", ""))
    if "1h: EMA=BULLISH" in trend_reason:
        return "BULLISH"
    if "1h: EMA=BEARISH" in trend_reason:
        return "BEARISH"
    return "UNKNOWN"


def counter_local(direction: str, local_trend: str) -> bool:
    """Return True when trade direction is against local 1h EMA trend."""
    return (direction == "SHORT" and local_trend == "BULLISH") or (
        direction == "LONG" and local_trend == "BEARISH"
    )


def post_loss_index(report: Mapping[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Index post-loss rows by symbol/opened_at."""
    output: Dict[Tuple[str, str], Dict[str, Any]] = {}
    losses = report.get("losses", [])
    if not isinstance(losses, list):
        return output
    for row in losses:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol", ""))
        opened_at = str(row.get("opened_at", ""))
        if symbol and opened_at:
            output[(symbol, opened_at)] = dict(row)
    return output


def entry_quality(
    result: str,
    direction: str,
    local_trend: str,
    age_minutes: float,
    loss_row: Mapping[str, Any],
) -> str:
    """Classify entry quality for discovery-only analysis."""
    if safe_bool(loss_row.get("bad_entry")):
        return "D"
    if safe_bool(loss_row.get("late_entry")) or age_minutes > 30:
        return "D"
    if counter_local(direction, local_trend):
        return "C"
    if result == "WIN":
        return "B"
    return "B"


def sl_quality(stop_size: float, atr: float, loss_row: Mapping[str, Any]) -> Tuple[str, float]:
    """Classify SL quality for discovery-only analysis."""
    if safe_bool(loss_row.get("bad_sl_zone")):
        sl_atr = safe_float(loss_row.get("sl_atr"))
        return "D", sl_atr
    sl_atr = safe_float(loss_row.get("sl_atr"))
    if sl_atr <= 0 and atr > 0:
        sl_atr = stop_size / atr
    if sl_atr <= 0:
        return "UNKNOWN", 0.0
    if sl_atr < 0.9:
        return "D", sl_atr
    if sl_atr <= 1.8:
        return "B", sl_atr
    return "C", sl_atr


def safe_bool(value: Any) -> bool:
    """Convert bool-like values to bool."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def parse_loss_reasons(value: Any) -> List[str]:
    """Parse loss reasons from report strings/lists."""
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if not value:
        return []
    return [item.strip() for item in str(value).split(";") if item.strip()]


class TradePatternDiscovery:
    """Discover repeated WIN/LOSS feature combinations."""

    def __init__(self) -> None:
        self.trades_file_rows = read_csv_rows(TRADES_FILE)
        self.debug_by_symbol = group_by_symbol(read_csv_rows(DEBUG_FILE))
        self.diagnostics_by_symbol = group_by_symbol(read_csv_rows(DIAGNOSTICS_FILE))
        self.explanations_by_symbol = group_by_symbol(read_csv_rows(EXPLANATIONS_FILE))
        self.signals_by_symbol = group_by_symbol(read_csv_rows(SIGNALS_FILE))
        self.comparator_report = read_json(COMPARATOR_REPORT)
        self.post_loss_report = read_json(POST_LOSS_REPORT)
        self.post_loss_by_key = post_loss_index(self.post_loss_report)

    def build_report(self) -> Dict[str, Any]:
        """Build pattern discovery report and artifacts."""
        profiles = self.build_profiles()
        patterns = self.mine_patterns(profiles)
        loss_patterns = self.top_patterns(patterns, "LOSS")
        win_patterns = self.top_patterns(patterns, "WIN")
        report = {
            "generated_at": utc_now(),
            "status": "OK" if profiles else "NO_CLOSED_TRADES",
            "sample": {
                "trades": len(profiles),
                "wins": sum(1 for row in profiles if row["result"] == "WIN"),
                "losses": sum(1 for row in profiles if row["result"] == "LOSS"),
                "base_loss_rate": self.base_rate(profiles, "LOSS"),
                "base_win_rate": self.base_rate(profiles, "WIN"),
                "min_support": MIN_SUPPORT,
                "pattern_sizes": list(PATTERN_SIZES),
            },
            "top_loss_patterns": loss_patterns,
            "top_win_patterns": win_patterns,
            "dangerous_combinations": self.recommend_block_candidates(loss_patterns),
            "encouraged_combinations": self.recommend_encourage_candidates(win_patterns),
            "profiles": profiles,
        }
        write_json(REPORT_JSON, report)
        self.write_patterns_csv(patterns)
        self.write_summary(report)
        return report

    def build_profiles(self) -> List[Dict[str, Any]]:
        """Build feature profile for every closed trade."""
        comparator_trades = self.comparator_report.get("trades", [])
        if isinstance(comparator_trades, list) and comparator_trades:
            source_trades = [row for row in comparator_trades if isinstance(row, Mapping)]
        else:
            source_trades = [
                row for row in self.trades_file_rows
                if str(row.get("result") or row.get("status", "")).upper() in {"WIN", "LOSS"}
            ]

        profiles = []
        for row in source_trades:
            profile = self.profile_trade(row)
            if profile:
                profiles.append(profile)
        return profiles

    def profile_trade(self, trade: Mapping[str, Any]) -> Dict[str, Any]:
        """Build one trade profile."""
        symbol = str(trade.get("symbol", ""))
        direction = str(trade.get("direction", "")).upper()
        result = str(trade.get("result") or trade.get("status", "")).upper()
        opened_at = str(trade.get("opened_at", ""))
        opened_time = parse_time(opened_at)
        decision_time = parse_time(trade.get("decision_time")) or opened_time
        debug_row = nearest_absolute(
            self.debug_by_symbol.get(symbol, []),
            decision_time,
            max_seconds=15,
        ) or nearest_before(
            self.debug_by_symbol.get(symbol, []),
            opened_time,
            direction=direction,
        )
        diagnostic_row = nearest_absolute(
            self.diagnostics_by_symbol.get(symbol, []),
            decision_time,
            max_seconds=15,
        )
        local_trend = local_trend_from_debug(debug_row)
        loss_row = self.post_loss_by_key.get((symbol, opened_at), {})
        confidence = safe_float(trade.get("confidence"))
        score = safe_float(trade.get("score"))
        weighted_score = safe_float(trade.get("weighted_score"))
        potential_score = safe_float(trade.get("potential_score"))
        age_minutes = safe_float(trade.get("signal_age_minutes"))
        rr = safe_float(trade.get("rr"))
        atr = safe_float(trade.get("atr"))
        stop_size = safe_float(trade.get("stop_size"))
        entry_q = entry_quality(result, direction, local_trend, age_minutes, loss_row)
        sl_q, sl_atr = sl_quality(stop_size, atr, loss_row)
        loss_reasons = parse_loss_reasons(trade.get("loss_reasons")) or parse_loss_reasons(
            loss_row.get("reasons")
        )
        momentum_disagreement = (
            str(trade.get("momentum_status", "")).upper() == "FAIL"
            or "Momentum disagreement" in loss_reasons
        )
        profile = {
            "symbol": symbol,
            "direction": direction,
            "result": result,
            "pnl": safe_float(trade.get("pnl")),
            "decision": str(trade.get("decision", "")),
            "quality": str(trade.get("quality", "")),
            "trend": str(trade.get("trend_status", "UNKNOWN")),
            "structure": str(trade.get("structure_status", "UNKNOWN")),
            "momentum": str(trade.get("momentum_status", "UNKNOWN")),
            "risk": str(trade.get("risk_status", "UNKNOWN")),
            "confidence": confidence,
            "score": score,
            "weighted_score": weighted_score,
            "potential_score": potential_score,
            "entry_quality": entry_q,
            "sl_quality": sl_q,
            "momentum_disagreement": momentum_disagreement,
            "local_trend": local_trend,
            "atr": atr,
            "rr": rr,
            "age_minutes": age_minutes,
            "sl_atr": round(sl_atr, 4),
            "primary_blocker": str(
                trade.get("primary_blocker") or (diagnostic_row or {}).get("primary_blocker", "")
            ),
            "loss_reasons": loss_reasons,
            "features": [],
        }
        profile["features"] = self.features(profile)
        return profile

    @staticmethod
    def features(profile: Mapping[str, Any]) -> List[str]:
        """Return normalized feature labels for pattern mining."""
        features = [
            f"symbol={profile.get('symbol')}",
            f"direction={profile.get('direction')}",
            f"signal={profile.get('decision') or 'UNKNOWN'}",
            f"quality={profile.get('quality') or 'UNKNOWN'}",
            f"Trend={profile.get('trend')}",
            f"Structure={profile.get('structure')}",
            f"Momentum={profile.get('momentum')}",
            f"Risk={profile.get('risk')}",
            f"confidence={confidence_bucket(safe_float(profile.get('confidence')))}",
            f"score={score_bucket(safe_float(profile.get('score')))}",
            value_bucket(safe_float(profile.get("weighted_score")), "weighted_score"),
            value_bucket(safe_float(profile.get("potential_score")), "potential_score"),
            f"entry_quality={profile.get('entry_quality')}",
            f"sl_quality={profile.get('sl_quality')}",
            f"local_trend={profile.get('local_trend')}",
            f"rr={rr_bucket(safe_float(profile.get('rr')))}",
            age_bucket(safe_float(profile.get("age_minutes"))),
            sl_atr_bucket(safe_float(profile.get("sl_atr"))),
        ]
        if profile.get("momentum_disagreement"):
            features.append("momentum_disagreement")
        else:
            features.append("momentum_aligned")
        if counter_local(str(profile.get("direction", "")), str(profile.get("local_trend", ""))):
            features.append("counter_local_trend")
        else:
            features.append("with_or_neutral_local_trend")
        blocker = str(profile.get("primary_blocker", ""))
        if blocker:
            features.append(f"primary_blocker={blocker}")
        return sorted(set(feature for feature in features if feature and "None" not in feature))

    @staticmethod
    def base_rate(profiles: Sequence[Mapping[str, Any]], target: str) -> float:
        """Return base target rate."""
        if not profiles:
            return 0.0
        return round(sum(1 for row in profiles if row.get("result") == target) / len(profiles), 4)

    def mine_patterns(self, profiles: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Mine feature combinations and calculate support/confidence/lift."""
        total = len(profiles)
        if not total:
            return []
        base_loss = self.base_rate(profiles, "LOSS")
        base_win = self.base_rate(profiles, "WIN")
        pattern_rows: Dict[Tuple[str, ...], List[Mapping[str, Any]]] = defaultdict(list)
        for profile in profiles:
            features = list(profile.get("features", []))
            for size in PATTERN_SIZES:
                if len(features) < size:
                    continue
                for combo in itertools.combinations(features, size):
                    pattern_rows[combo].append(profile)

        patterns = []
        for combo, rows in pattern_rows.items():
            support = len(rows)
            if support < MIN_SUPPORT:
                continue
            wins = sum(1 for row in rows if row.get("result") == "WIN")
            losses = sum(1 for row in rows if row.get("result") == "LOSS")
            win_conf = wins / support
            loss_conf = losses / support
            pnls = [safe_float(row.get("pnl")) for row in rows]
            patterns.append(
                {
                    "pattern": " + ".join(combo),
                    "features": list(combo),
                    "size": len(combo),
                    "support": support,
                    "support_pct": pct(support, total),
                    "wins": wins,
                    "losses": losses,
                    "win_confidence": round(win_conf, 4),
                    "loss_confidence": round(loss_conf, 4),
                    "win_lift": round(win_conf / base_win, 4) if base_win else 0.0,
                    "loss_lift": round(loss_conf / base_loss, 4) if base_loss else 0.0,
                    "avg_pnl": round(mean(pnls), 8),
                    "net_pnl": round(sum(pnls), 8),
                }
            )
        return patterns

    @staticmethod
    def top_patterns(patterns: Sequence[Mapping[str, Any]], target: str) -> List[Dict[str, Any]]:
        """Return top WIN or LOSS patterns."""
        confidence_key = "loss_confidence" if target == "LOSS" else "win_confidence"
        lift_key = "loss_lift" if target == "LOSS" else "win_lift"
        count_key = "losses" if target == "LOSS" else "wins"
        filtered = [
            dict(pattern) for pattern in patterns
            if safe_float(pattern.get(count_key)) > 0
        ]
        filtered.sort(
            key=lambda item: (
                safe_float(item.get(lift_key)),
                safe_float(item.get(confidence_key)),
                safe_float(item.get("support")),
                abs(safe_float(item.get("net_pnl"))),
            ),
            reverse=True,
        )
        return filtered[:TOP_LIMIT]

    @staticmethod
    def recommend_block_candidates(loss_patterns: Sequence[Mapping[str, Any]]) -> List[str]:
        """Generate cautious future-block candidates from top LOSS patterns."""
        recommendations = []
        for pattern in loss_patterns:
            support = int(safe_float(pattern.get("support")))
            loss_conf = safe_float(pattern.get("loss_confidence"))
            lift = safe_float(pattern.get("loss_lift"))
            if support >= MIN_SUPPORT and loss_conf >= 0.9 and lift > 1.0:
                recommendations.append(
                    f"Consider future block-test: {pattern.get('pattern')} "
                    f"(support={support}, LOSS confidence={loss_conf:.2f}, lift={lift:.2f})."
                )
        if not recommendations:
            recommendations.append("No high-confidence block candidate found yet.")
        recommendations.append("Do not implement automatically; confirm with replay/backtest first.")
        return recommendations[:TOP_LIMIT]

    @staticmethod
    def recommend_encourage_candidates(win_patterns: Sequence[Mapping[str, Any]]) -> List[str]:
        """Generate cautious future-encouragement candidates from top WIN patterns."""
        recommendations = []
        for pattern in win_patterns:
            support = int(safe_float(pattern.get("support")))
            win_conf = safe_float(pattern.get("win_confidence"))
            lift = safe_float(pattern.get("win_lift"))
            if support >= MIN_SUPPORT and win_conf >= 0.5 and lift > 1.0:
                recommendations.append(
                    f"Encourage in future scoring tests: {pattern.get('pattern')} "
                    f"(support={support}, WIN confidence={win_conf:.2f}, lift={lift:.2f})."
                )
        if not recommendations:
            recommendations.append("No robust WIN pattern found yet.")
        recommendations.append("Do not boost automatically; validate with more closed trades.")
        return recommendations[:TOP_LIMIT]

    @staticmethod
    def write_patterns_csv(patterns: Sequence[Mapping[str, Any]]) -> None:
        """Write mined pattern table."""
        fieldnames = [
            "pattern",
            "size",
            "support",
            "support_pct",
            "wins",
            "losses",
            "win_confidence",
            "loss_confidence",
            "win_lift",
            "loss_lift",
            "avg_pnl",
            "net_pnl",
        ]
        ranked = sorted(
            patterns,
            key=lambda item: (
                max(safe_float(item.get("loss_lift")), safe_float(item.get("win_lift"))),
                safe_float(item.get("support")),
            ),
            reverse=True,
        )
        with PATTERNS_CSV.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for pattern in ranked:
                writer.writerow({field: pattern.get(field, "") for field in fieldnames})

    @staticmethod
    def write_summary(report: Mapping[str, Any]) -> None:
        """Write text summary."""
        sample = report.get("sample", {})
        lines = [
            "Trade Pattern Discovery",
            "=======================",
            f"Generated: {report.get('generated_at')}",
            f"Status: {report.get('status')}",
            "",
            "Sample:",
            f"- Trades: {sample.get('trades')} | WIN: {sample.get('wins')} | LOSS: {sample.get('losses')}",
            f"- Base LOSS rate: {round(safe_float(sample.get('base_loss_rate')) * 100, 2)}%",
            f"- Min support: {sample.get('min_support')}",
            "",
            "TOP LOSS Patterns:",
        ]
        for index, pattern in enumerate(report.get("top_loss_patterns", [])[:TOP_LIMIT], 1):
            lines.append(
                f"{index}. {pattern.get('pattern')} | support={pattern.get('support')} "
                f"LOSS={round(safe_float(pattern.get('loss_confidence')) * 100, 2)}% "
                f"lift={pattern.get('loss_lift')} avg_pnl={pattern.get('avg_pnl')}"
            )
        lines.extend(["", "TOP WIN Patterns:"])
        for index, pattern in enumerate(report.get("top_win_patterns", [])[:TOP_LIMIT], 1):
            lines.append(
                f"{index}. {pattern.get('pattern')} | support={pattern.get('support')} "
                f"WIN={round(safe_float(pattern.get('win_confidence')) * 100, 2)}% "
                f"lift={pattern.get('win_lift')} avg_pnl={pattern.get('avg_pnl')}"
            )
        lines.extend(["", "Future Block Candidates:"])
        for item in report.get("dangerous_combinations", []):
            lines.append(f"- {item}")
        lines.extend(["", "Future Encourage Candidates:"])
        for item in report.get("encouraged_combinations", []):
            lines.append(f"- {item}")
        lines.extend(
            [
                "",
                "Important:",
                "- This is pattern discovery on a small sample.",
                "- No strategy, config, weights, or DecisionEngine code was changed.",
            ]
        )
        SUMMARY_TEXT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def print_report(report: Mapping[str, Any]) -> None:
        """Print compact terminal report."""
        sample = report.get("sample", {})
        print("Trade Pattern Discovery")
        print(f"Trades: {sample.get('trades')} | WIN/LOSS: {sample.get('wins')}/{sample.get('losses')}")
        top_loss = report.get("top_loss_patterns", [{}])[0] if report.get("top_loss_patterns") else {}
        top_win = report.get("top_win_patterns", [{}])[0] if report.get("top_win_patterns") else {}
        print(f"Top LOSS: {top_loss.get('pattern', 'N/A')}")
        print(f"Top WIN: {top_win.get('pattern', 'N/A')}")
        print(f"JSON: {REPORT_JSON.name}")
        print(f"Summary: {SUMMARY_TEXT.name}")
        print(f"CSV: {PATTERNS_CSV.name}")


def main() -> None:
    """CLI entry point."""
    discovery = TradePatternDiscovery()
    report = discovery.build_report()
    discovery.print_report(report)


if __name__ == "__main__":
    main()
