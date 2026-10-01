"""Reproduce the XAUUSD study from the previously captured, immutable-price dataset."""
import gzip
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fusion.store import Store
from fusion.research import research_dataset,save_research_report


def main():
    store=Store(ROOT/"data/fusion.sqlite3")
    cfg,versions=store.active()
    with gzip.open(ROOT/"artifacts/gold-history.json.gz","rt",encoding="utf-8") as f: snapshot=json.load(f)
    cfg["strategy"]={**cfg["strategy"],"symbol":"XAUUSD","timeframe":"M5","context_timeframes":["H1"],"bars":60000}
    result=research_dataset(snapshot,cfg,lambda m:print(m,flush=True))
    result.update(config_versions=versions,research_config=cfg,run_note="Correctness/adaptive-risk rerun of the previously inspected dataset; exploratory, not a fresh unseen holdout.")
    save_research_report(ROOT/"artifacts",result)
    store.put("research_result",result)
    store.audit("research.cached_completed",{"run_id":result["run_id"],"approved":result["candidate"]["approved"]})
    print(json.dumps({"run_id":result["run_id"],"candidate":result["candidate"]},ensure_ascii=False))


if __name__=="__main__":main()
