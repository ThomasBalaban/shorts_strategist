"""
Recommendation artifact store.

All thinker output lands under ``output/recommendations/`` in canonical paths
the rest of the system can rely on. Each artifact carries an ``input_hashes``
block; tasks check that block before doing work so unchanged inputs skip the
LLM round-trip.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import config

# Categories a UI can iterate over. Keep keys stable — the hub references them.
CATEGORIES: Dict[str, str] = {
    "postmortems":         "postmortems",
    "tailwind_critiques":  "tailwind_critiques",
    "titles":              "titles",
    "channel":             "channel",
    "capabilities":        "capabilities",
}


def _category_dir(category: str) -> str:
    if category not in CATEGORIES:
        raise ValueError(f"unknown recommendation category: {category}")
    path = os.path.join(config.RECOMMENDATIONS_DIR, CATEGORIES[category])
    os.makedirs(path, exist_ok=True)
    return path


def artifact_path(category: str, key: str) -> str:
    """Resolve the on-disk path for ``(category, key)``.

    ``key`` is interpreted as the filename stem; ``.json`` is appended unless
    the caller already supplied it.
    """
    name = key if key.endswith(".json") else f"{key}.json"
    return os.path.join(_category_dir(category), name)


# ─── read ─────────────────────────────────────────────────────────────────────

def read(category: str, key: str) -> Optional[Dict[str, Any]]:
    p = artifact_path(category, key)
    if not os.path.isfile(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def existing_input_hash(category: str, key: str) -> Optional[str]:
    art = read(category, key)
    if not art:
        return None
    return art.get("input_hash")


def list_category(category: str) -> List[Dict[str, Any]]:
    d = _category_dir(category)
    items: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        body: Optional[Dict[str, Any]] = None
        try:
            with open(path) as f:
                body = json.load(f)
        except (OSError, json.JSONDecodeError):
            body = None
        items.append({
            "category": category,
            "key": name[:-5],  # strip .json
            "path": path,
            "modified": stat.st_mtime,
            "size_bytes": stat.st_size,
            "input_hash": (body or {}).get("input_hash"),
            "task_type": (body or {}).get("task_type"),
            "trace_id":  (body or {}).get("trace_id"),
        })
    return items


# ─── write ────────────────────────────────────────────────────────────────────

@dataclass
class WriteResult:
    path: str
    written: bool


def write(
    category: str,
    key: str,
    *,
    task_type: str,
    input_hash: str,
    payload: Dict[str, Any],
    trace_id: Optional[str] = None,
) -> WriteResult:
    """Atomically write a recommendation artifact.

    The artifact's outer envelope is:
        {task_type, input_hash, generated_at, trace_id, payload}
    Atomic write is tmp-file + rename so a partial write never leaves a
    half-decoded JSON on disk.
    """
    path = artifact_path(category, key)
    envelope = {
        "task_type":    task_type,
        "category":     category,
        "key":          key,
        "input_hash":   input_hash,
        "generated_at": time.time(),
        "trace_id":     trace_id,
        "payload":      payload,
    }
    raw = json.dumps(envelope, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(prefix=".tmp.", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(raw)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return WriteResult(path=path, written=True)
