import csv
from statistics import mean

CSV_FILE = "decision_debug.csv"

rows = []

with open(CSV_FILE, newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

trend = []
structure = []
momentum = []
risk = []

for r in rows:
    trend.append(float(r["trend_long"]) + float(r["trend_short"]))
    structure.append(float(r["structure_long"]) + float(r["structure_short"]))
    momentum.append(float(r["momentum_long"]) + float(r["momentum_short"]))
    risk.append(float(r["risk_long"]) + float(r["risk_short"]))

trend_avg = mean(trend)
structure_avg = mean(structure)
momentum_avg = mean(momentum)
risk_avg = mean(risk)

print("=" * 60)
print("Weight Optimizer")
print("=" * 60)

print(f"Analyzed decisions : {len(rows)}")
print()
print("Current weights")
print("-" * 60)
print("Trend      : 40.0%")
print("Structure  : 25.0%")
print("Momentum   : 20.0%")
print("Risk       : 15.0%")
print()

print("Average engine activity")
print("-" * 60)
print(f"Trend      : {trend_avg:6.2f}")
print(f"Structure  : {structure_avg:6.2f}")
print(f"Momentum   : {momentum_avg:6.2f}")
print(f"Risk       : {risk_avg:6.2f}")
print()

total = trend_avg + structure_avg + momentum_avg + risk_avg

print("Relative contribution")
print("-" * 60)
trend_pct = trend_avg / total * 100
structure_pct = structure_avg / total * 100
momentum_pct = momentum_avg / total * 100
risk_pct = risk_avg / total * 100

print(f"Trend      : {trend_pct:5.1f}%")
print(f"Structure  : {structure_pct:5.1f}%")
print(f"Momentum   : {momentum_pct:5.1f}%")
print(f"Risk       : {risk_pct:5.1f}%")
print()
print("Suggestions")
print("-" * 60)

# Use calculated percentages for recommendations

if trend_pct >= 65:
    print("• Trend Engine is heavily dominating the decision process.")
    print("• Recommendation: reduce TREND_WEIGHT by 5–10%.")
elif trend_pct >= 55:
    print("• Trend Engine has noticeable dominance.")
    print("• Recommendation: monitor Trend weight before changing it.")
else:
    print("• Trend contribution is balanced.")

if structure_pct < 15:
    print("• Structure Engine has low influence.")

if momentum_pct < 15:
    print("• Momentum Engine has low influence.")

if risk_pct < 10:
    print("• Risk Engine contributes very little.")