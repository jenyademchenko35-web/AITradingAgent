import csv
from collections import Counter
from statistics import mean

CSV_FILE = "signals_v3.csv"

# Set to a date like "2026-06-26" to analyze only newer signals.
# Leave as None to analyze the entire file.
START_DATE = None

with open(CSV_FILE, newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
    if START_DATE:
        rows = [
            r for r in rows
            if r.get("timestamp", "")[:10] >= START_DATE
        ]

print("=" * 60)
print("Backtester V1")
print("=" * 60)

print(f"Signals analyzed : {len(rows)}")
if START_DATE:
    print(f"Start date       : {START_DATE}")

# Remove accidental repeated header rows
rows = [r for r in rows if r["score"] != "score"]
if not rows:
    print("No signals found.")
    raise SystemExit

symbols = Counter(r["symbol"] for r in rows)
directions = Counter(r["direction"] for r in rows)
signals = Counter(r["signal"] for r in rows)

scores = [float(r["score"]) for r in rows]
confidences = [float(r["confidence"]) for r in rows]

print()
print("Symbols")
print("-" * 60)

for symbol, count in symbols.items():
    print(f"{symbol:<12} {count:>5}")

print()
print("Directions")
print("-" * 60)

for direction, count in directions.items():
    print(f"{direction:<12} {count:>5}")

print()
print("Signals")
print("-" * 60)

for signal, count in signals.items():
    print(f"{signal:<12} {count:>5}")

print()
print("Signal percentages")
print("-" * 60)

for signal, count in sorted(signals.items()):
    percent = count / len(rows) * 100
    print(f"{signal:<12} {percent:5.1f}%")

print()
print("Quality Statistics")
print("-" * 60)

qualities = Counter(r.get("quality", "Unknown") for r in rows)

for quality in ["A", "B", "C", "D", "E"]:
    count = qualities.get(quality, 0)
    percent = count / len(rows) * 100 if rows else 0
    print(f"{quality:<5} {count:>5} ({percent:5.1f}%)")

print()
print("Statistics")
print("-" * 60)

print(f"Average score      : {mean(scores):.2f}")
print(f"Average confidence : {mean(confidences):.2f}%")
print(f"Max score          : {max(scores):.0f}")
print(f"Min score          : {min(scores):.0f}")

print()
print("Average Score by Quality")
print("-" * 60)

for quality in ["A", "B", "C", "D", "E"]:
    quality_rows = [r for r in rows if r.get("quality") == quality]

    if not quality_rows:
        continue

    avg_score = mean(float(r["score"]) for r in quality_rows)
    avg_conf = mean(float(r["confidence"]) for r in quality_rows)

    print(
        f"{quality}: "
        f"Score={avg_score:.2f} | "
        f"Confidence={avg_conf:.2f}% | "
        f"Signals={len(quality_rows)}"
    )

print()
print("Per Symbol Statistics")
print("-" * 60)

for symbol in sorted(symbols.keys()):
    symbol_rows = [r for r in rows if r["symbol"] == symbol]

    avg_score = mean(float(r["score"]) for r in symbol_rows)
    avg_conf = mean(float(r["confidence"]) for r in symbol_rows)

    signal_counter = Counter(r["signal"] for r in symbol_rows)

    print(f"\n{symbol}")
    print(f"  Signals    : {len(symbol_rows)}")
    print(f"  Avg score  : {avg_score:.2f}")
    print(f"  Avg conf   : {avg_conf:.2f}%")
    print(f"  SETUP      : {signal_counter.get('SETUP', 0)}")
    print(f"  WATCH      : {signal_counter.get('WATCH', 0)}")
    print(f"  NO TRADE   : {signal_counter.get('NO TRADE', 0)}")

print()
print("Top 10 Signals")
print("-" * 60)

top = sorted(rows, key=lambda r: float(r["score"]), reverse=True)[:10]

for r in top:
    print(
        f"{r['symbol']:<10} "
        f"{r['direction']:<6} "
        f"{r['signal']:<12} "
        f"Score={r['score']:<3} "
        f"Conf={r['confidence']}%"
    )

print()
print("Bottom 10 Signals")
print("-" * 60)

bottom = sorted(rows, key=lambda r: float(r["score"]))[:10]

for r in bottom:
    print(
        f"{r['symbol']:<10} "
        f"{r['direction']:<6} "
        f"{r['signal']:<12} "
        f"Score={r['score']:<3} "
        f"Conf={r['confidence']}%"
    )
