import subprocess
import re
import csv
from datetime import datetime
import os
#import shutil

from config import ATR_TEST_VALUES

def run_test(atr):
    result = subprocess.run(
        [
            "python3",
            "backtest.py",
            "--atr",
            str(atr),
        ],
        capture_output=True,
        text=True,
    )

    return result.stdout

print("=" * 60)
print("AI Trading Strategy Optimizer")
print("=" * 60)

print("\nRunning backtest...\n")

#CONFIG_FILE = "config.py"
#BACKUP_FILE = "config_backup.py"

#shutil.copy(CONFIG_FILE, BACKUP_FILE)

print("\nATR values to test:")

for atr in ATR_TEST_VALUES:
    print(f" - ATR_MULT = {atr}")

patterns = {
    "ProfitFactor": r"ProfitFactor\s*:\s*([0-9.]+)",
    "WinRate": r"WinRate\s*:\s*([0-9.]+)",
    "Trades": r"Trades\s*:\s*(\d+)",
    "Wins": r"Wins\s*:\s*(\d+)",
    "Losses": r"Losses\s*:\s*(\d+)",
}

results = []

for atr in ATR_TEST_VALUES:
    output = run_test(atr)

    stats = {}

    for key, pattern in patterns.items():
        match = re.search(pattern, output)
        stats[key] = match.group(1) if match else "N/A"

    results.append({
        "atr": atr,
        "ProfitFactor": stats["ProfitFactor"],
        "WinRate": stats["WinRate"],
        "Trades": stats["Trades"],
        "Wins": stats["Wins"],
        "Losses": stats["Losses"],
    })

print("=" * 60)
print("BACKTEST SUMMARY")
print("=" * 60)

for result in results:
    print(
        f'ATR={result["atr"]} | '
        f'PF={result["ProfitFactor"]} | '
        f'WR={result["WinRate"]}% | '
        f'Trades={result["Trades"]}'
    )

print("=" * 60)
csv_file = "optimizer_results.csv"

file_exists = os.path.exists(csv_file)

with open(csv_file, "a", newline="") as f:
    writer = csv.writer(f)

    if not file_exists:
        writer.writerow([
            "Date",
            "ATR",
            "ProfitFactor",
            "WinRate",
            "Trades",
            "Wins",
            "Losses"
        ])

    for result in results:
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            result["atr"],
            result["ProfitFactor"],
            result["WinRate"],
            result["Trades"],
            result["Wins"],
            result["Losses"],
        ])

best_result = max(results, key=lambda r: float(r["ProfitFactor"]))

print("\n" + "=" * 60)
print("BEST CONFIG")
print("=" * 60)
print(f"ATR_MULT      : {best_result['atr']}")
print(f"ProfitFactor  : {best_result['ProfitFactor']}")
print(f"WinRate       : {best_result['WinRate']}%")
print(f"Trades        : {best_result['Trades']}")
print("=" * 60)

print("\nResult saved to optimizer_results.csv")

#shutil.move(BACKUP_FILE, CONFIG_FILE)

#print("Config restored.")