import csv
from pathlib import Path
from collections import Counter
from statistics import mean

BASE_DIR = Path(__file__).resolve().parent
CSV_FILE = BASE_DIR / "decision_debug.csv"

COLUMNS = [
    "timestamp",
    "symbol",
    "direction",
    "signal",
    "score",
    "confidence",
    "trend_long",
    "trend_short",
    "structure_long",
    "structure_short",
    "momentum_long",
    "momentum_short",
    "risk_long",
    "risk_short",
    "long_total",
    "short_total",
    "diff",
    "winner",
    "trend_reason",
    "structure_reason",
    "momentum_reason",
    "risk_reason",
    "summary",
]


def load_data():
    if not CSV_FILE.is_file():
        raise FileNotFoundError(f"File not found:\n{CSV_FILE}")

    rows = []

    with CSV_FILE.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)

        for row in reader:
            if not row:
                continue

            if row[0] == "timestamp":
                continue

            if len(row) < len(COLUMNS):
                continue

            rows.append(dict(zip(COLUMNS, row)))

    return rows


def main():

    rows = load_data()

    if not rows:

        print("decision_debug.csv is empty.")

        return

    print("=" * 60)
    print("AITradingAgent Decision Analyzer V2")
    print("=" * 60)

    print(f"Total decisions : {len(rows)}")
    total = len(rows)
    scores = [int(r["score"]) for r in rows]
    conf = [float(r["confidence"]) for r in rows]
    diffs = [int(r["diff"]) for r in rows]

    print(f"Average score   : {mean(scores):.2f}")
    print(f"Average diff    : {mean(diffs):.2f}")
    print(f"Average conf    : {mean(conf):.2f}%")
    long_totals = [float(r["long_total"]) for r in rows]
    short_totals = [float(r["short_total"]) for r in rows]

    print(f"Average LONG    : {mean(long_totals):.2f}")
    print(f"Average SHORT   : {mean(short_totals):.2f}")
    print(f"Min score       : {min(scores)}")
    print(f"Max score       : {max(scores)}")

    print("\nSignals")
    print("-" * 60)

    signal_counter = Counter(r["signal"] for r in rows)

    for signal, count in signal_counter.items():
        percent = count / total * 100
        print(f"{signal:<15}{count:>5} ({percent:5.1f}%)")

    print()
    print(f"SETUP rate      : {signal_counter.get('SETUP', 0) / total * 100:.1f}%")
    print(f"WATCH rate      : {signal_counter.get('WATCH', 0) / total * 100:.1f}%")
    print(f"NO TRADE rate   : {signal_counter.get('NO TRADE', 0) / total * 100:.1f}%")

    print("\nDirections")
    print("-" * 60)

    direction_counter = Counter(r["direction"] for r in rows)

    for direction, count in direction_counter.items():
        percent = count / total * 100
        print(f"{direction:<15}{count:>5} ({percent:5.1f}%)")

    print("\nPer Symbol Statistics")
    print("-" * 60)

    symbols = sorted(set(r["symbol"] for r in rows))

    for symbol in symbols:
        symbol_rows = [r for r in rows if r["symbol"] == symbol]

        avg_score = mean(float(r["score"]) for r in symbol_rows)
        avg_conf = mean(float(r["confidence"]) for r in symbol_rows)

        signal_counts = Counter(r["signal"] for r in symbol_rows)

        print(f"\n{symbol}")
        print(f"  Decisions : {len(symbol_rows)}")
        print(f"  Avg score : {avg_score:.2f}")
        print(f"  Avg conf  : {avg_conf:.2f}%")
        setup = signal_counts.get("SETUP", 0)
        watch = signal_counts.get("WATCH", 0)
        no_trade = signal_counts.get("NO TRADE", 0)

        count = len(symbol_rows)

        print(f"  SETUP     : {setup:3d} ({setup / count * 100:5.1f}%)")
        print(f"  WATCH     : {watch:3d} ({watch / count * 100:5.1f}%)")
        print(f"  NO TRADE  : {no_trade:3d} ({no_trade / count * 100:5.1f}%)")

    print("\nEngine averages")
    print("-" * 60)

    engines = [
        ("Trend", "trend_long", "trend_short"),
        ("Structure", "structure_long", "structure_short"),
        ("Momentum", "momentum_long", "momentum_short"),
        ("Risk", "risk_long", "risk_short"),
    ]

    engine_scores = {}

    for name, long_col, short_col in engines:
        long_avg = mean(float(r[long_col]) for r in rows)
        short_avg = mean(float(r[short_col]) for r in rows)

        engine_scores[name] = long_avg + short_avg

        print(
            f"{name:<10}"
            f" LONG {long_avg:6.2f}"
            f"  SHORT {short_avg:6.2f}"
        )

    print("\nEngine dominance")
    print("-" * 60)
    for name, long_col, short_col in engines:
        long_wins = sum(1 for r in rows if float(r[long_col]) > float(r[short_col]))
        short_wins = sum(1 for r in rows if float(r[short_col]) > float(r[long_col]))
        ties = sum(1 for r in rows if float(r[long_col]) == float(r[short_col]))
        print(f"{name:<10} LONG: {long_wins:2d}  SHORT: {short_wins:2d}  TIE: {ties:2d}")

    top_engine = max(engine_scores, key=engine_scores.get)

    print("\nStrongest Engine")
    print("-" * 60)
    print(f"{top_engine}: {engine_scores[top_engine]:.2f}")

    print("\nRecommendations")
    print("-" * 60)
    print(f"• Strongest engine: {top_engine}")

    if mean(diffs) < 10:
        print("• Direction difference is too small.")
        print("  Consider lowering abs_diff threshold.")

    if signal_counter["NO TRADE"] > len(rows) * 0.7:
        print("• Too many NO TRADE signals.")

    if mean(scores) < 25:
        print("• DecisionEngine score calibration should be reviewed.")

    print("\nScore distribution")
    print("-" * 60)
    score_ranges = {
        "0-9": 0,
        "10-19": 0,
        "20-29": 0,
        "30-39": 0,
        "40+": 0,
    }
    for s in scores:
        if 0 <= s <= 9:
            score_ranges["0-9"] += 1
        elif 10 <= s <= 19:
            score_ranges["10-19"] += 1
        elif 20 <= s <= 29:
            score_ranges["20-29"] += 1
        elif 30 <= s <= 39:
            score_ranges["30-39"] += 1
        else:
            score_ranges["40+"] += 1
    for range_label, count in score_ranges.items():
        print(f"{range_label:<6} : {count}")

    print("\nDiff statistics")
    print("-" * 60)
    print(f"Average : {mean(diffs):.2f}")
    print(f"Minimum : {min(diffs)}")
    print(f"Maximum : {max(diffs)}")

    print("\nAnalyzer verdict")
    print("-" * 60)
    max_score = max(scores)
    watch_percent = signal_counter.get("WATCH", 0) / total * 100

    sorted_scores = sorted(engine_scores.values(), reverse=True)
    top1 = sorted_scores[0]
    top2 = sorted_scores[1] if len(sorted_scores) > 1 else 0

    if max_score < 30:
        print("Current score scale is compressed. Review score calculation or thresholds.")

    if watch_percent > 50:
        print("WATCH dominates the output. Review WATCH/SETUP thresholds.")

    if top2 > 0 and (top1 - top2) / top2 > 0.3:
        print("One engine dominates the decision process. Consider recalibrating engine weights.")

    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\nERROR")
        print("-" * 60)
        print(e)