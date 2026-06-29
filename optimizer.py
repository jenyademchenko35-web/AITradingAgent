import subprocess
import re
import csv
from datetime import datetime
import os

print("=" * 60)
print("AI Trading Strategy Optimizer")
print("=" * 60)

print("\nRunning backtest...\n")

result = subprocess.run(
    ["python3", "backtest.py"],
    capture_output=True,
    text=True
)

output = result.stdout

patterns = {
    "ProfitFactor": r"ProfitFactor\s*:\s*([0-9.]+)",
    "WinRate": r"WinRate\s*:\s*([0-9.]+)",
    "Trades": r"Trades\s*:\s*(\d+)",
    "Wins": r"Wins\s*:\s*(\d+)",
    "Losses": r"Losses\s*:\s*(\d+)",
}

stats = {}

for key, pattern in patterns.items():
    match = re.search(pattern, output)
    if match:
        stats[key] = match.group(1)
    else:
        stats[key] = "N/A"

print("=" * 60)
print("BACKTEST SUMMARY")
print("=" * 60)

for k, v in stats.items():
    print(f"{k:15} {v}")

print("=" * 60)
csv_file = "optimizer_results.csv"

file_exists = os.path.exists(csv_file)

with open(csv_file, "a", newline="") as f:
    writer = csv.writer(f)

    if not file_exists:
        writer.writerow([
            "Date",
            "ProfitFactor",
            "WinRate",
            "Trades",
            "Wins",
            "Losses"
        ])

    writer.writerow([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        stats["ProfitFactor"],
        stats["WinRate"],
        stats["Trades"],
        stats["Wins"],
        stats["Losses"],
    ])

print("\nResult saved to optimizer_results.csv")