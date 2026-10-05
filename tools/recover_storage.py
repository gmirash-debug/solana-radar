"""Bounded cold-outbox recovery under the scanner writer lock; no RPC calls."""
import argparse
import gzip
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import scanner
from cold_outbox import archive_snapshot, decode_snapshot
import requests


def recover(maximum=10, seconds=120):
    scanner.load_env()
    config = {"remote_legacy_snapshot_enabled":False}
    url, secret = scanner.remote_data_url_from_env(), scanner.remote_ingest_secret()
    if not url or not secret:
        raise ValueError("storage recovery authorization unavailable")
    deadline = time.monotonic()+seconds
    result = {"archived":0,"replayed":0,"deferred":0,"rpc_calls":0}
    state = scanner.load_json(scanner.STATE_PATH,{})
    if (state.get("_runtime") or {}).get("updated_at"):
        checkpoint = scanner.sync_runtime_checkpoint(state,{"runtime_checkpoint_budget_seconds":min(180,seconds)},"deep")
        result["checkpoint_saved"] = checkpoint.get("accepted") is True
        result["checkpoint_parts"] = {key:checkpoint.get(key,0) for key in ("parts_total","parts_reused","parts_uploaded")}
    paths = sorted([*scanner.REMOTE_OUTBOX_DIR.glob("*.json.gz"),
                    *(scanner.REMOTE_OUTBOX_DIR/"quarantine").glob("*.json.gz")])[:maximum]
    for path in paths:
        if time.monotonic()>=deadline:
            break
        try:
            if path.stat().st_size>16*1024*1024:
                raise ValueError("original exceeds archive capacity")
            with gzip.open(path,"rb") as handle:
                raw = handle.read(128*1024*1024+1)
            if len(raw)>128*1024*1024:
                raise ValueError("original exceeds decoded capacity")
            archive_snapshot(json.loads(raw),url,secret,timeout=max(1,min(25,int(deadline-time.monotonic()))))
            path.unlink()
            result["archived"]+=1
        except (RuntimeError,ValueError,requests.RequestException):
            result["deferred"]+=1
            break
    if time.monotonic()>=deadline:
        return result
    cursor = scanner.remote_api_call("GET","/api/runtime/storage-recovery",config)
    after = ((cursor.get("document") or {}).get("value") or {}).get("after","")
    page = scanner.remote_api_call("GET","/api/storage/outbox",config,params={"after":after})
    entries = page.get("entries") or []
    for entry in entries[:maximum]:
        if time.monotonic()>=deadline:
            break
        after = entry["id"]
        if entry.get("completed"):
            continue
        try:
            response = requests.get(url+"/api/storage/outbox",params={"id":entry["id"]},
                                    headers={"x-radar-ingest-secret":secret},timeout=25,
                                    allow_redirects=False,stream=True)
            with response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data)>16*1024*1024 or time.monotonic()>=deadline:
                        raise ValueError("cold replay capacity exhausted")
            body = decode_snapshot(data,entry["archive_ref"])
            body["_sync_progress"] = {**entry.get("progress",{}),"source_archive":1}
            complete = False
            try:
                complete = scanner.send_remote_snapshot(body,config,deadline=deadline)
            finally:
                scanner.remote_api_call("PATCH","/api/storage/outbox",config,
                    {"progress":body["_sync_progress"],"completed":complete},params={"id":entry["id"]})
            result["replayed"]+=int(complete)
            result["deferred"]+=int(not complete)
        except (RuntimeError,ValueError,requests.RequestException):
            result["deferred"]+=1
    scanner.remote_api_call("POST","/api/runtime/storage-recovery",config,
                            {"value":{"after":after if len(entries)==25 else ""},
                             "updated_at":scanner.utc_now().isoformat()})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-items",type=int,default=10,choices=range(1,26))
    parser.add_argument("--max-seconds",type=int,default=120,choices=range(1,181))
    args = parser.parse_args()
    try:
        result=recover(args.max_items,args.max_seconds)
        result["status"] = "checkpoint_pending" if result.get("checkpoint_saved") is False else "partial" if result["deferred"] else "ok"
        print(json.dumps(result))
        return 1 if result["status"]=="checkpoint_pending" else 0
    except (ValueError,RuntimeError,requests.RequestException):
        print("Storage recovery deferred; unacknowledged originals remain preserved")
        return 1


if __name__=="__main__":
    raise SystemExit(main())
