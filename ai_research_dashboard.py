"""Read-only aggregator for the unified Telegram research dashboard."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from feature_logger import summarize_feature_coverage
from root_cause_analyzer import RootCauseAnalyzer
from trade_metrics_normalizer import aggregate_trade_metrics

BASE_DIR = Path(__file__).resolve().parent
SEPARATOR = "━━━━━━━━━━━━━━━━━━"


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            return [
                {str(key).strip().lower(): value for key, value in row.items()}
                for row in csv.DictReader(stream)
            ]
    except (OSError, csv.Error, UnicodeError):
        return []


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _git_branch(root: Path) -> str:
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        return head.rsplit("/", 1)[-1] if head.startswith("ref:") else head[:8]
    except OSError:
        return "N/A"


def _candidate_rows(payload: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Read map/list and legacy top-level candidate laboratory schemas."""
    candidates = payload.get("candidates", payload.get("results", payload.get("candidate_results")))
    rows: list[tuple[str, dict[str, Any]]] = []
    if isinstance(candidates, Mapping):
        rows.extend(
            (str(name), dict(data))
            for name, data in candidates.items() if isinstance(data, Mapping)
        )
    elif isinstance(candidates, list):
        for index, data in enumerate(candidates):
            if not isinstance(data, Mapping):
                continue
            name = data.get("candidate_id") or data.get("id") or data.get("name") or f"Candidate {index + 1}"
            rows.append((str(name), dict(data)))
    for key in ("best_candidate", "leader", "top_candidate"):
        data = payload.get(key)
        if isinstance(data, Mapping):
            name = data.get("candidate_id") or data.get("id") or data.get("name") or key
            rows.append((str(name), dict(data)))
        elif isinstance(data, str):
            matched = next((row for row in rows if row[0] == data), None)
            if matched:
                rows.remove(matched)
                rows.insert(0, matched)
    return rows


def _candidate_leader(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = _candidate_rows(payload)
    if not rows:
        return {}
    non_live = [row for row in rows if row[0].upper() != "LIVE_BASELINE"]
    if non_live:
        rows = non_live
    eligible = []
    for index, (name, data) in enumerate(rows):
        pf = data.get("profit_factor", data.get("pf"))
        complete = int(data.get(
            "complete_trades", data.get("closed_trades", data.get("closed", 0))
        ) or 0)
        decisions = int(data.get("total_decisions", data.get("decisions", 0)) or 0)
        eligible.append((
            pf is not None,
            _number(pf, -1.0),
            complete,
            decisions,
            -index,
            dict(data),
            str(name),
        ))
    _, _, _, _, _, data, name = max(eligible)
    data["profit_factor"] = data.get("profit_factor", data.get("pf"))
    data["complete_trades"] = complete = int(data.get(
        "complete_trades", data.get("closed_trades", data.get("closed", 0))
    ) or 0)
    data["winrate"] = data.get("winrate", data.get("win_rate", 0))
    data["name"] = name.replace("_", " ").title()
    return data


def _normalize_news_status(item: Mapping[str, Any]) -> str:
    status = str(item.get("status") or item.get("health_status") or "UNAVAILABLE").upper()
    error = str(item.get("error", "")).lower()
    http_status = int(item.get("http_status", 0) or 0)
    if status == "PARSER_ERROR" and http_status == 202 and (
        "no element found" in error or "empty_response" in error
    ):
        return "NO_CONTENT"
    return status


def load_walk_forward(base_dir: Path) -> dict[str, Any]:
    """Read the new walk-forward schema defensively; never break Dashboard."""
    payload = _json(base_dir / "walk_forward_report.json")
    if not payload or not isinstance(payload.get("status"), str):
        return {
            "status": "NOT_RUN", "candidate": "N/A", "oos_pf": 0.0,
            "oos_net_r": 0.0, "better_windows": "0 / 0",
            "profitable_windows": "0 / 0", "confidence": "LOW",
        }
    candidate = payload.get("candidate")
    comparison = payload.get("comparison")
    bootstrap = payload.get("bootstrap")
    candidate = candidate if isinstance(candidate, Mapping) else {}
    comparison = comparison if isinstance(comparison, Mapping) else {}
    bootstrap = bootstrap if isinstance(bootstrap, Mapping) else {}
    windows = int(candidate.get("windows", 0) or 0)
    return {
        "status": payload.get("status", "NOT_RUN"),
        "candidate": candidate.get("name", "N/A"),
        "oos_pf": candidate.get("profit_factor", 0),
        "oos_net_r": _number(candidate.get("net_r")),
        "better_windows": f"{int(comparison.get('candidate_better_windows', 0) or 0)} / {windows}",
        "profitable_windows": f"{int(candidate.get('profitable_windows', 0) or 0)} / {windows}",
        "confidence": bootstrap.get("confidence", "LOW"),
    }


def _age(path: Path, now: datetime | None = None) -> str:
    try:
        seconds = max(
            0.0,
            ((now or datetime.now(timezone.utc)).timestamp() - path.stat().st_mtime),
        )
    except OSError:
        return "Unknown"
    if seconds < 60:
        return f"{int(seconds)} sec ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def _newest_existing(root: Path, paths: tuple[str, ...]) -> Path:
    existing = [root / relative for relative in paths if (root / relative).exists()]
    return max(existing, key=lambda path: path.stat().st_mtime) if existing else root / paths[0]


def _feature_source(root: Path) -> tuple[Path, list[dict[str, str]]]:
    paths = tuple(root / relative for relative in (
        "decision_features.csv", "data/decision_features.csv",
        "reports/decision_features.csv",
    ))
    populated = [(path, _csv(path)) for path in paths if path.exists()]
    with_rows = [(path, rows) for path, rows in populated if rows]
    if with_rows:
        return max(with_rows, key=lambda item: item[0].stat().st_mtime)
    if populated:
        return max(populated, key=lambda item: item[0].stat().st_mtime)
    return paths[0], []


def calculate_health_score(report: Mapping[str, Any]) -> dict[str, Any]:
    score = 100
    trading = report.get("trading", {})
    research = report.get("research", {})
    candidate = report.get("candidate", {})
    features = report.get("features", {})
    root = report.get("root_cause", {})
    news = report.get("news", [])
    if _number(trading.get("profit_factor")) < 1:
        score -= 10
    if _number(trading.get("winrate")) < 25:
        score -= 10
    research_status = str(research.get("status", "")).upper()
    if research_status in {"WARNING", "DEGRADED"}:
        score -= 5
    elif research_status in {"ERROR", "FAILED"}:
        score -= 15
    score -= 2 * sum(
        str(item.get("status", "")).upper() not in {"ONLINE", "DEGRADED", "EMPTY"}
        for item in news if isinstance(item, Mapping)
    )
    if _number(features.get("market_regime_field")) < 100:
        score -= 5
    if _number(features.get("unknown_market_regime")) > 30:
        score -= 5
    if candidate:
        if candidate.get("profit_factor") is None or _number(candidate.get("profit_factor")) < 1:
            score -= 5
        if int(candidate.get("complete_trades", candidate.get("closed_trades", 0)) or 0) < 50:
            score -= 5
    causes = root.get("causes", []) if isinstance(root, Mapping) else []
    if causes and str(causes[0].get("severity", "")).upper() == "HIGH":
        score -= 5
    score = max(0, score)
    label = "EXCELLENT" if score >= 90 else "GOOD" if score >= 75 else "WARNING" if score >= 60 else "ATTENTION"
    return {"score": score, "label": label}


def build_recommendation(report: Mapping[str, Any]) -> str:
    candidate = report.get("candidate", {})
    if candidate and int(candidate.get("complete_trades", candidate.get("closed_trades", 0)) or 0) < 50:
        return (
            "Continue collecting statistics.\n"
            f"{candidate.get('name', 'Candidate')} has insufficient trades for promotion."
        )
    research_status = str(report.get("research", {}).get("status", "")).upper()
    if research_status in {"WARNING", "DEGRADED", "ERROR", "FAILED"}:
        return "Run Walk Forward Validation."
    causes = report.get("root_cause", {}).get("causes", [])
    if causes and causes[0].get("name") == "Risk" and causes[0].get("severity") == "HIGH":
        return "Review Risk Engine."
    return "Continue collecting statistics."


class AIResearchDashboard:
    def __init__(self, base_dir: str | Path = BASE_DIR) -> None:
        self.base_dir = Path(base_dir)

    def build_report(self) -> dict[str, Any]:
        stats_path = self.base_dir / "agent_v3_stats.json"
        research_path = self.base_dir / "reports" / "research_dashboard.json"
        candidate_path = _newest_existing(self.base_dir, (
            "reports/candidate_laboratory.json", "candidate_laboratory.json",
        ))
        features_path, feature_rows = _feature_source(self.base_dir)
        news_path = _newest_existing(self.base_dir, (
            "market_news_feed.json", "market_news_sources.json",
        ))
        stats = _json(stats_path)
        trades = aggregate_trade_metrics(_csv(self.base_dir / "trades.csv"))
        research_payload = _json(research_path)
        research_status = research_payload.get("system_research_status", {})
        candidate = _candidate_leader(_json(candidate_path))
        root = RootCauseAnalyzer(self.base_dir).build_report()
        features = summarize_feature_coverage(feature_rows)
        news_payload = _json(news_path)
        source_rows = news_payload.get("source_statuses", news_payload.get("sources", []))
        news = [
            {
                "name": item.get("source_name") or item.get("name") or "Unknown",
                "status": _normalize_news_status(item),
            }
            for item in source_rows
            if isinstance(item, Mapping) and str(item.get("kind", "")) != "LOCAL_CACHE"
        ]
        report = {
            "agent": {
                "server": "ONLINE" if stats else "UNKNOWN",
                "version": "v1.0",
                "git": _git_branch(self.base_dir),
                "cycle": stats.get("runs", 0),
            },
            "trading": {
                "closed": trades.get("closed_trades", 0),
                "winrate": trades.get("winrate", 0),
                "profit_factor": trades.get("profit_factor", 0),
                "net_r": trades.get("net_r", 0),
                "drawdown": trades.get("max_drawdown_r", 0),
            },
            "research": {
                "status": research_status.get("status", "NOT_AVAILABLE"),
                "recommendation": research_payload.get("main_recommendation", "KEEP_LIVE_UNCHANGED"),
                "confidence": (
                    "HIGH" if _number(research_status.get("global_confidence_percent")) >= 70
                    else "MEDIUM" if _number(research_status.get("global_confidence_percent")) >= 45
                    else "LOW"
                ),
            },
            "candidate": candidate,
            "walk_forward": load_walk_forward(self.base_dir),
            "root_cause": root,
            "features": features,
            "news": news,
            "freshness": {
                "Research": _age(research_path),
                "Candidates": _age(candidate_path),
                "Decision Features": _age(features_path),
                "News": _age(news_path),
                "Agent Stats": _age(stats_path),
            },
        }
        report["health"] = calculate_health_score(report)
        report["recommendation"] = build_recommendation(report)
        return report

    def format_telegram(self, report: Mapping[str, Any]) -> str:
        agent, trading = report["agent"], report["trading"]
        research, candidate = report["research"], report.get("candidate", {})
        features, health = report["features"], report["health"]
        lines = [
            "📊 AI Research Dashboard", "", SEPARATOR, "", "🤖 Agent", "",
            f"Server:\n{agent['server']}", "", f"Version:\n{agent['version']}", "",
            f"Git:\n{agent['git']}", "", f"Cycle:\n{agent['cycle']}", "",
            SEPARATOR, "", "📈 Trading", "",
            f"Closed:\n{trading['closed']}", "", f"Winrate:\n{_number(trading['winrate']):.1f}%", "",
            f"Profit Factor:\n{_number(trading['profit_factor']):.2f}", "",
            f"Net R:\n{_number(trading['net_r']):.2f}", "",
            f"Drawdown:\n{_number(trading['drawdown']):.2f}R", "",
            SEPARATOR, "", "🧪 Research", "",
            f"Status:\n{research['status']}", "", f"Recommendation:\n{research['recommendation']}", "",
            f"Confidence:\n{research['confidence']}", "", SEPARATOR, "", "🏆 Candidate Leader", "",
        ]
        if candidate:
            lines.extend([
                str(candidate["name"]), "",
                f"PF:\n{_number(candidate.get('profit_factor')):.2f}" if candidate.get("profit_factor") is not None else "PF:\nN/A", "",
                f"Winrate:\n{_number(candidate.get('winrate')):.1f}%", "",
                f"Closed:\n{candidate.get('complete_trades', candidate.get('closed_trades', 0))}", "",
                f"Status:\n{candidate.get('status', 'COLLECTING')}", "",
            ])
        else:
            lines.extend(["No candidate data", ""])
        walk_forward = report.get("walk_forward", {})
        lines.extend([
            SEPARATOR, "", "🔬 Walk-Forward", "",
            f"Status:\n{walk_forward.get('status', 'NOT_RUN')}", "",
            f"Candidate:\n{walk_forward.get('candidate', 'N/A')}", "",
            f"OOS PF:\n{walk_forward.get('oos_pf', 0)}", "",
            f"OOS Net R:\n{_number(walk_forward.get('oos_net_r')):.2f}R", "",
            f"Better Windows:\n{walk_forward.get('better_windows', '0 / 0')}", "",
            f"Profitable Windows:\n{walk_forward.get('profitable_windows', '0 / 0')}", "",
            f"Confidence:\n{walk_forward.get('confidence', 'LOW')}", "",
        ])
        lines.extend([SEPARATOR, "", "🧠 Root Cause", ""])
        causes = report.get("root_cause", {}).get("causes", [])
        lines.extend(
            f"{item['name']}\n{item['percentage']:.1f}%\n"
            for item in causes[:3]
        )
        if not causes:
            lines.extend(["Insufficient data", ""])
        lines.extend([
            SEPARATOR, "", "📊 Feature Coverage", "",
            f"ATR\n{features['atr']:.1f}%", "", f"ADX\n{features['adx']:.1f}%", "",
            f"Volume\n{features['volume']:.1f}%", "",
            f"Market Regime\n{features['market_regime_field']:.1f}%", "",
            f"Defined Regime\n{features['defined_market_regime']:.1f}%", "",
            f"Unknown\n{features['unknown_market_regime']:.1f}%", "",
            SEPARATOR, "", "📰 News", "",
        ])
        news = report.get("news", [])
        for item in news:
            lines.extend([str(item["name"]), str(item["status"]), ""])
        if not news:
            lines.extend(["No news data", ""])
        lines.extend([SEPARATOR, "", "🕒 Data Freshness", ""])
        for name, age in report.get("freshness", {}).items():
            lines.extend([str(name), str(age), ""])
        lines.extend([
            SEPARATOR, "", "✅ Overall Health", "",
            f"{health['label']} ({health['score']}/100)", "",
            "Recommendation:", "", str(report["recommendation"]),
        ])
        return "\n".join(lines)[:4096]
