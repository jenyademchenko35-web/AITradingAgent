import csv
from collections import Counter

CSV_FILE = "decision_debug.csv"

with open(CSV_FILE, newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

# Убираем случайные повторные заголовки
rows = [r for r in rows if r.get("score") != "score"]

print("=" * 60)
print("Decision Calibrator")
print("=" * 60)

print(f"Analyzed decisions : {len(rows)}")
print()

signals = Counter(r["signal"] for r in rows)
qualities = Counter(r.get("quality", "Unknown") for r in rows)
directions = Counter(r["direction"] for r in rows)

print("Signal distribution")
print("-" * 60)
for signal, count in signals.items():
    print(f"{signal:<15}{count}")

print()

print("Quality distribution")
print("-" * 60)
for quality in ["A", "B", "C", "D", "E"]:
    print(f"{quality:<5}{qualities.get(quality, 0)}")

print()

print("Direction distribution")
print("-" * 60)
for direction, count in directions.items():
    print(f"{direction:<10}{count}")
