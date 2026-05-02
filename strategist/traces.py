import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

import config


def new_trace(kind: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "trace_id": f"{kind}_{uuid.uuid4().hex[:12]}",
        "kind": kind,
        "started_at": time.time(),
        "inputs": inputs,
        "steps": [],
        "result": None,
        "error": None,
    }


def add_step(trace: Dict[str, Any], name: str, payload: Dict[str, Any]) -> None:
    trace["steps"].append({"name": name, "at": time.time(), **payload})


def finalize(trace: Dict[str, Any], result: Optional[Dict[str, Any]] = None,
             error: Optional[str] = None) -> str:
    trace["finished_at"] = time.time()
    trace["result"] = result
    trace["error"] = error
    path = os.path.join(config.TRACES_DIR, f"{trace['trace_id']}.json")
    with open(path, "w") as f:
        json.dump(trace, f, indent=2)
    return path


def read(trace_id: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(config.TRACES_DIR, f"{trace_id}.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def list_recent(limit: int = 50) -> List[Dict[str, Any]]:
    if not os.path.isdir(config.TRACES_DIR):
        return []
    entries = []
    for name in os.listdir(config.TRACES_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(config.TRACES_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        entries.append({"trace_id": name[:-5], "modified": st.st_mtime})
    entries.sort(key=lambda e: e["modified"], reverse=True)
    return entries[:limit]
