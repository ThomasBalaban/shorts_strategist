"""
Input snapshot layer.

The thinker reads every input source through this module so each tick has a
single, hashed view of the world. Hashes feed the skip-if-unchanged logic in
``recommendations.py`` — if a task's input hash matches the artifact already
on disk, the task is skipped.

Sources:
    1. shorts_analyzer/output/<handle>.json            (per-short Gemini analysis)
    2. shorts_analyzer/output/<handle>.tailwind.json   (per-video tailwind hypotheses)
    3. shorts_analyzer/output/<handle>.synthesis.json  (corpus narrative + stats)
    4. shorts_analyzer/output/<handle>.context.json    (channel baselines)
    5. shorts-auto-editor/shorts_data/shorts_metadata_*.json (pre-publish cuts)

Each source either resolves to a SourceFile (mtime + sha256 + parsed body) or
is reported as missing. A missing source is not fatal — tasks that depend on
it just stay out of the queue.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import config


@dataclass
class SourceFile:
    path: str
    mtime: float
    sha256: str
    body: Any

    @property
    def basename(self) -> str:
        return os.path.basename(self.path)


@dataclass
class Snapshot:
    """A single tick's view of all inputs. Build once, query many."""
    channel_handle: str
    per_short: Optional[SourceFile] = None
    tailwind: Optional[SourceFile] = None
    synthesis: Optional[SourceFile] = None
    context: Optional[SourceFile] = None
    pre_publish: List[SourceFile] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    # ── Derived views ─────────────────────────────────────────────────────
    def shorts_by_video_id(self) -> Dict[str, Dict[str, Any]]:
        if not self.per_short:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for s in self.per_short.body.get("shorts", []):
            vid = s.get("video_id")
            if vid:
                out[vid] = s
        return out

    def tailwind_by_video_id(self) -> Dict[str, Dict[str, Any]]:
        if not self.tailwind:
            return {}
        videos = self.tailwind.body.get("videos", {})
        # The analyzer keys this by video_id directly, not as a list.
        return dict(videos) if isinstance(videos, dict) else {}

    def context_by_video_id(self) -> Dict[str, Dict[str, Any]]:
        if not self.context:
            return {}
        videos = self.context.body.get("videos", {})
        return dict(videos) if isinstance(videos, dict) else {}

    def pre_publish_by_basename(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for sf in self.pre_publish:
            body = sf.body
            # Each file is a single-element list per shorts-auto-editor convention.
            record = body[0] if isinstance(body, list) and body else body
            if isinstance(record, dict):
                out[sf.basename] = record
        return out

    def summary(self) -> Dict[str, Any]:
        return {
            "channel_handle": self.channel_handle,
            "per_short_count": len(self.shorts_by_video_id()),
            "tailwind_count": len(self.tailwind_by_video_id()),
            "synthesis_present": self.synthesis is not None,
            "context_present": self.context is not None,
            "pre_publish_count": len(self.pre_publish),
            "missing": list(self.missing),
            "input_mtimes": {
                "per_short":   self.per_short.mtime   if self.per_short   else None,
                "tailwind":    self.tailwind.mtime    if self.tailwind    else None,
                "synthesis":   self.synthesis.mtime   if self.synthesis   else None,
                "context":     self.context.mtime    if self.context    else None,
                "pre_publish_max": (
                    max((sf.mtime for sf in self.pre_publish), default=None)
                ),
            },
        }


# ─── readers ──────────────────────────────────────────────────────────────────

def _read_json_file(path: str) -> Optional[SourceFile]:
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        raw = f.read()
    sha = hashlib.sha256(raw).hexdigest()
    body = json.loads(raw.decode("utf-8"))
    return SourceFile(path=path, mtime=os.path.getmtime(path), sha256=sha, body=body)


def _analyzer_path(handle: str, suffix: str) -> str:
    # shorts_analyzer writes <handle>.json, <handle>.tailwind.json, etc.
    name = f"{handle}.json" if suffix == "" else f"{handle}.{suffix}.json"
    return os.path.join(config.ANALYZER_OUTPUT_DIR, name)


def take(channel_handle: Optional[str] = None) -> Snapshot:
    """Read all input sources once and return a Snapshot."""
    handle = channel_handle or config.CHANNEL_HANDLE
    snap = Snapshot(channel_handle=handle)

    pairs = [
        ("per_short", _analyzer_path(handle, "")),
        ("tailwind",  _analyzer_path(handle, "tailwind")),
        ("synthesis", _analyzer_path(handle, "synthesis")),
        ("context",   _analyzer_path(handle, "context")),
    ]
    for attr, path in pairs:
        sf = _read_json_file(path)
        if sf is None:
            snap.missing.append(path)
        else:
            setattr(snap, attr, sf)

    pp_glob = os.path.join(config.SUBTITLER_SHORTS_DATA_DIR, "shorts_metadata_*.json")
    for path in sorted(glob.glob(pp_glob)):
        sf = _read_json_file(path)
        if sf is not None:
            snap.pre_publish.append(sf)
    if not snap.pre_publish and not os.path.isdir(config.SUBTITLER_SHORTS_DATA_DIR):
        snap.missing.append(config.SUBTITLER_SHORTS_DATA_DIR)

    return snap


# ─── hashing helpers used by tasks ────────────────────────────────────────────

def hash_inputs(parts: Dict[str, Optional[str]]) -> str:
    """Stable hash of {source_name: sha256-or-None} pairs.

    Tasks build this dict from the source SHAs they depend on. Two snapshots
    with identical relevant SHAs produce the same hash, so an artifact that
    stored this hash can be detected as ``current``.
    """
    h = hashlib.sha256()
    for name in sorted(parts):
        h.update(name.encode("utf-8"))
        h.update(b"\x00")
        h.update((parts[name] or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()
