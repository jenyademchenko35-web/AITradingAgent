"""Read-only IPE accuracy; absent price snapshots remain unevaluated."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
def build(history:Path, output:Path):
    try: rows=[json.loads(x) for x in history.read_text(encoding="utf-8").splitlines() if x]
    except (OSError,ValueError): rows=[]
    payload={"schema_version":"ipe-accuracy-v1","generated_at":datetime.now(timezone.utc).isoformat(),"source_files":[str(history)],"status":"INSUFFICIENT_DATA","limitations":["No timestamped future price snapshots were supplied."],"data_quality":{"history_rows":len(rows)},"evaluated_predictions":None,"pending_predictions":len(rows),"confirmed_impulses":None,"false_positives":None,"false_negatives":None,"precision":None,"hit_rate":None,"by_probability_bucket":{},"by_symbol":{},"by_regime":{},"by_horizon":{}}
    output.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8"); return payload
