from pathlib import Path
import argparse
from runtime_strategy_diagnostics import write_reports
p=argparse.ArgumentParser(); p.add_argument("--data-dir",type=Path,default=Path(".")); args=p.parse_args(); write_reports(args.data_dir)
