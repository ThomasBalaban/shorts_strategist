"""
Shorts Strategist API Server
Deep-think reasoning for shorts-auto-editor: titles, cut plans, effects,
strategy memory, experiments, capability roadmap.
Port: 9022  (sibling of shorts-auto-editor:9020 and shorts_analyzer:9021)
"""
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402
from strategist import logs as strategist_logs  # noqa: E402
strategist_logs.install()  # tee stdout/stderr early so we capture uvicorn boot logs
from strategist import strategy, traces, think_title as think_title_mod  # noqa: E402
from strategist import recommendations as recs_mod  # noqa: E402
from strategist import thinker as thinker_mod  # noqa: E402
from strategist.analyzer_client import AnalyzerClient  # noqa: E402


# ─── Models ───────────────────────────────────────────────────────────────────

class TitleRequest(BaseModel):
    channel_handle: str
    transcript: List[Dict[str, Any]]
    trim_summary: Optional[Dict[str, Any]] = None
    cut_id: Optional[str] = None


class CutPlanRequest(BaseModel):
    channel_handle: str
    transcript: List[Dict[str, Any]]
    audio_events: Optional[List[Dict[str, Any]]] = None
    visual_events: Optional[List[Dict[str, Any]]] = None
    candidate_ranges: List[Dict[str, Any]]
    cut_id: Optional[str] = None


class EffectsRequest(BaseModel):
    channel_handle: str
    transcript: List[Dict[str, Any]]
    audio_events: Optional[List[Dict[str, Any]]] = None
    visual_events: Optional[List[Dict[str, Any]]] = None
    capability_manifest: Optional[Dict[str, Any]] = None
    cut_id: Optional[str] = None


class StrategyIngestRequest(BaseModel):
    cut_id: str
    channel_handle: str
    video_id: Optional[str] = None
    edit_decisions: Dict[str, Any]
    title: Optional[str] = None
    title_reasoning: Optional[str] = None


class StampVideoIdRequest(BaseModel):
    cut_id: str
    video_id: str


class ExperimentDesignRequest(BaseModel):
    channel_handle: str
    upcoming_recordings: List[Dict[str, Any]]
    hypothesis_hint: Optional[str] = None


class RoadmapGapsRequest(BaseModel):
    channel_handle: str
    capability_manifest: Dict[str, Any]


class ThinkerForceRequest(BaseModel):
    task_type: str
    key: Optional[str] = None


# ─── App lifecycle ────────────────────────────────────────────────────────────

# UI polls these every few seconds — keep them out of the access log so the
# console stays readable. Anything else (errors, mutations, real /think calls)
# still logs normally.
_QUIET_PREFIXES = (
    "/thinker/status",
    "/thinker/queue",
    "/recommendations/",
    "/health",
    "/logs",
    "/traces",
)


class _AccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access record args: (client_addr, method, path, http_version, status)
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path = str(args[2])
        status = args[4]
        # Always show non-2xx so failures aren't hidden.
        if isinstance(status, int) and status >= 400:
            return True
        return not any(path.startswith(p) for p in _QUIET_PREFIXES)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.getLogger("uvicorn.access").addFilter(_AccessLogFilter())
    strategy.init_schema()
    print(f"✅ Shorts Strategist API ready on :{config.PORT}", flush=True)
    print(f"   Data dir:    {config.DATA_DIR}", flush=True)
    print(f"   Traces dir:  {config.TRACES_DIR}", flush=True)
    print(f"   Analyzer at: {config.ANALYZER_URL}", flush=True)
    yield
    print("Shorts Strategist API stopping.", flush=True)


app = FastAPI(title="Shorts Strategist API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    analyzer = AnalyzerClient()
    return {
        "status": "ok",
        "port": config.PORT,
        "analyzer_reachable": analyzer.health(),
        "data_dir": config.DATA_DIR,
        "traces_dir": config.TRACES_DIR,
    }


# ─── /think/* ─────────────────────────────────────────────────────────────────

def _not_implemented(name: str) -> HTTPException:
    return HTTPException(
        status_code=501,
        detail=f"{name} not implemented yet — scaffold only. See gameplan in repo.",
    )


@app.post("/think/title")
def think_title(req: TitleRequest):
    try:
        return think_title_mod.think_title(
            channel_handle=req.channel_handle,
            transcript=req.transcript,
            trim_summary=req.trim_summary,
            cut_id=req.cut_id,
        )
    except RuntimeError as e:
        # Missing API key — config error, not a transient failure
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"think_title failed: {e}")


@app.post("/think/cut-plan")
def think_cut_plan(req: CutPlanRequest):
    raise _not_implemented("/think/cut-plan")


@app.post("/think/effects")
def think_effects(req: EffectsRequest):
    raise _not_implemented("/think/effects")


# ─── /strategy/* ──────────────────────────────────────────────────────────────

@app.post("/strategy/ingest")
def strategy_ingest(req: StrategyIngestRequest):
    strategy.record_cut(
        cut_id=req.cut_id,
        channel_handle=req.channel_handle,
        edit_decisions=req.edit_decisions,
        title=req.title,
        title_reasoning=req.title_reasoning,
        video_id=req.video_id,
    )
    return {"ok": True, "cut_id": req.cut_id}


@app.post("/strategy/stamp-video-id")
def strategy_stamp(req: StampVideoIdRequest):
    strategy.stamp_video_id(req.cut_id, req.video_id)
    return {"ok": True}


@app.get("/strategy/cuts")
def strategy_cuts(channel_handle: str):
    return strategy.joined_for_channel(channel_handle)


@app.get("/strategy/patterns")
def strategy_patterns(channel_handle: str):
    raise _not_implemented("/strategy/patterns (pattern reasoning)")


# ─── /experiment/* ────────────────────────────────────────────────────────────

@app.post("/experiment/design")
def experiment_design(req: ExperimentDesignRequest):
    raise _not_implemented("/experiment/design")


@app.get("/experiment/list")
def experiment_list(channel_handle: Optional[str] = None):
    return strategy.list_experiments(channel_handle)


# ─── /roadmap/* ───────────────────────────────────────────────────────────────

@app.post("/roadmap/gaps")
def roadmap_gaps(req: RoadmapGapsRequest):
    raise _not_implemented("/roadmap/gaps")


# ─── /thinker/* ───────────────────────────────────────────────────────────────

@app.get("/thinker/status")
def thinker_status():
    return thinker_mod.get().status()


@app.post("/thinker/start")
def thinker_start():
    return thinker_mod.get().start()


@app.post("/thinker/stop")
def thinker_stop():
    return thinker_mod.get().stop()


@app.post("/thinker/force")
def thinker_force(req: ThinkerForceRequest):
    return thinker_mod.get().force(task_type=req.task_type, key=req.key)


@app.get("/thinker/queue")
def thinker_queue():
    return {"tasks": thinker_mod.get().queue_snapshot()}


@app.post("/thinker/clear-stale")
def thinker_clear_stale():
    return thinker_mod.get().clear_stale()


# ─── /recommendations/* ───────────────────────────────────────────────────────

@app.get("/recommendations/categories")
def recommendations_categories():
    return {"categories": list(recs_mod.CATEGORIES.keys())}


@app.get("/recommendations/{category}")
def recommendations_list(category: str):
    if category not in recs_mod.CATEGORIES:
        raise HTTPException(404, f"Unknown category: {category}")
    return {"category": category, "items": recs_mod.list_category(category)}


@app.get("/recommendations/{category}/{key}")
def recommendations_read(category: str, key: str):
    if category not in recs_mod.CATEGORIES:
        raise HTTPException(404, f"Unknown category: {category}")
    art = recs_mod.read(category, key)
    if art is None:
        raise HTTPException(404, f"Artifact not found: {category}/{key}")
    return art


# ─── /logs ────────────────────────────────────────────────────────────────────

@app.get("/logs")
def logs_get(last: int = 300):
    return {"lines": strategist_logs.read(last=last)}


@app.delete("/logs")
def logs_delete():
    strategist_logs.clear()
    return {"ok": True}


# ─── /traces/* ────────────────────────────────────────────────────────────────

@app.get("/traces")
def traces_list(limit: int = 50):
    return traces.list_recent(limit=limit)


@app.get("/traces/{trace_id}")
def traces_read(trace_id: str):
    t = traces.read(trace_id)
    if t is None:
        raise HTTPException(404, f"Trace not found: {trace_id}")
    return t


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
