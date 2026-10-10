"""
Outcome sync — fill the strategy.db outcomes table from the analyzer.

The cut→video_id join (MISSION Phase 3) is only useful once outcomes
exist for the video_ids. This task copies every analyzer-scored short's
breakout_score + views into strategy.outcomes. No LLM calls; it re-runs
whenever the per-short corpus changes.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .. import inputs as inputs_mod
from .. import recommendations
from .. import strategy
from .base import Task

TASK_TYPE = "outcome_sync"
CATEGORY = "channel"


def enumerate_tasks(snap: inputs_mod.Snapshot) -> List[Task]:
    if not snap.per_short:
        return []

    input_hash = inputs_mod.hash_inputs({
        "per_short": snap.per_short.sha256,
        "historical": (snap.historical.sha256
                       if getattr(snap, "historical", None) else ""),
    })
    key = f"{snap.channel_handle}.outcome_sync"

    def _run() -> Dict[str, Any]:
        strategy.init_schema()
        sources = [snap.per_short.body]
        hist = getattr(snap, "historical", None)
        if hist is not None:
            sources.append(hist.body)

        synced = 0
        seen: set = set()
        for body in sources:
            for s in (body or {}).get("shorts", []):
                vid = s.get("video_id")
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                strategy.record_outcome(
                    video_id=vid,
                    channel_handle=snap.channel_handle,
                    breakout_score=s.get("breakout_score"),
                    retention_curve=None,
                    raw={
                        "title": s.get("title"),
                        "views": s.get("views"),
                        "published_at": s.get("published_at"),
                        "duration_seconds": s.get("duration_seconds"),
                    },
                )
                synced += 1

        payload = {
            "channel_handle": snap.channel_handle,
            "outcomes_synced": synced,
        }
        recommendations.write(
            CATEGORY, key,
            task_type=TASK_TYPE, input_hash=input_hash, payload=payload,
        )
        return payload

    return [Task(
        task_type=TASK_TYPE,
        key=key,
        category=CATEGORY,
        input_hash=input_hash,
        run=_run,
        description=f"sync analyzer outcomes into strategy.db "
                    f"for @{snap.channel_handle}",
    )]
