import subprocess
import re
import csv
from datetime import datetime
import sys
import time
from pathlib import Path

from config import ATR_TEST_VALUES, RR_TEST_VALUES

BASE_DIR = Path(__file__).resolve().parent

def run_test(atr, rr):
    result = subprocess.run(
        [
        sys.executable,
        str(BASE_DIR / "backtest.py"),
        "--atr",
        str(atr),
        "--rr",
        str(rr),
    ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"\nERROR while testing ATR={atr}")
        print(result.stderr)
        return None

    return result.stdout

print("=" * 60)
print("AI Trading Strategy Optimizer")
print("=" * 60)

print("\nRunning optimization...\n")

print("\nATR values to test:")

for atr in ATR_TEST_VALUES:
    for rr in RR_TEST_VALUES:
        print(f" - ATR={atr} | RR={rr}")

patterns = {
    "ProfitFactor": r"ProfitFactor\s*:\s*([0-9.]+)",
    "WinRate": r"WinRate\s*:\s*([0-9.]+)",
    "Trades": r"Trades\s*:\s*(\d+)",
    "Wins": r"Wins\s*:\s*(\d+)",
    "Losses": r"Losses\s*:\s*(\d+)",
}

start_time = time.time()
results = []

for atr in ATR_TEST_VALUES:
    for rr in RR_TEST_VALUES:
        print(f"\nTesting ATR={atr} | RR={rr}...")
        output = run_test(atr, rr)
        if output is None:
            continue

        stats = {}

        for key, pattern in patterns.items():
            match = re.search(pattern, output)
            stats[key] = match.group(1) if match else "N/A"

        results.append({
            "atr": atr,
            "rr": rr,
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
        f'RR={result["rr"]} | '
        f'PF={result["ProfitFactor"]} | '
        f'WR={result["WinRate"]}% | '
        f'Trades={result["Trades"]}'
    )

print("=" * 60)
csv_file = BASE_DIR / "optimizer_results.csv"

file_exists = csv_file.exists()

with csv_file.open("a", newline="") as f:
    writer = csv.writer(f)

    if not file_exists:
        writer.writerow([
            "Date",
            "ATR",
            "RR",
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
            result["rr"],
            result["ProfitFactor"],
            result["WinRate"],
            result["Trades"],
            result["Wins"],
            result["Losses"],
        ])

if not results:
    print("No optimizer results were produced.")
    raise SystemExit(1)

results.sort(
    key=lambda r: float(r["ProfitFactor"]),
    reverse=True,
)

best_result = results[0]

print("\n" + "=" * 60)
print("BEST CONFIG")
print("=" * 60)
print(f"ATR_MULT      : {best_result['atr']}")
print(f"ProfitFactor  : {best_result['ProfitFactor']}")
print(f"WinRate       : {best_result['WinRate']}%")
print(f"Trades        : {best_result['Trades']}")
print("=" * 60)

print("\nTOP 3 RESULTS")
for r in results[:3]:
    print(
        f'ATR={r["atr"]} | '
        f'PF={r["ProfitFactor"]} | '
        f'WR={r["WinRate"]}%'
    )

elapsed = time.time() - start_time
print(f"\nOptimization finished in {elapsed:.2f} sec")
print(f"\nResult saved to {csv_file.name}")
