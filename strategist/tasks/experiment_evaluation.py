"""
Experiment evaluation — MISSION Phase 5.

For every ledger experiment still in status "designed": once its test
cut has a video_id, the outcomes table holds a mature breakout score
for it, and enough of its batch-mates (the control group) have mature
outcomes too, this task:

  1. computes the numbers in Python (test breakout vs control median —
     trustworthy arithmetic, per house posture),
  2. asks Gemini to judge the written prediction against those numbers
     and state what the result does to the channel beliefs that
     generated it (a refuted prediction is as valuable as a confirmed
     one),
  3. writes conclusion + status="concluded" onto the ledger row
     (append-only memory — never deleted) and a human-readable verdict
     artifact under recommendations/experiments/.

Concluded experiments then feed title_pattern_retro and
channel_scorecard via their prompt/input-hash (part 2 of Phase 5), so
causal evidence starts outranking correlation.
"""
from __future__ import annotations

import datetime
import json
import statistics
from typing import Any, Dict, List, Optional, Tuple

import config
from .. import inputs as inputs_mod
from .. import llm
from .. import recommendations
from .. import strategy
from .. import traces
from .base import Task

TASK_TYPE = "experiment_evaluation"
CATEGORY = "experiments"

# A short needs public life before its breakout score means anything.
MIN_DAYS_PUBLIC = 7
# Don't judge against a control group smaller than this.
MIN_CONTROLS = 5

_VERDICT_PROMPT = """\
You are scoring a pre-registered YouTube Shorts experiment for @{handle}.
The prediction was written BEFORE any outcome data existed. Judge it
honestly — a refuted prediction is as valuable as a confirmed one, because
it prunes the belief that generated it.

THE EXPERIMENT (as registered):
{experiment_json}

MEASURED RESULTS (computed from real YouTube analytics; trust these
numbers, do not recompute):
{numbers_json}

Return ONLY this JSON, no fences:
{{
  "verdict": "confirmed" | "refuted" | "inconclusive",
  "conclusion": "2-3 sentences: what happened vs what was predicted, with the key numbers",
  "belief_implication": "one sentence: which channel belief this strengthens or prunes",
  "follow_up_worth_running": "one sentence or null"
}}"""


def _published_at(outcome: Dict[str, Any]) -> Optional[str]:
    return (outcome.get("raw") or {}).get("published_at")


def _mature(outcome: Dict[str, Any]) -> bool:
    pa = _published_at(outcome)
    if not pa:
        return False
    try:
        pub = datetime.datetime.fromisoformat(pa.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - pub).days >= MIN_DAYS_PUBLIC


def _batch_members(snap, batch_id: str) -> List[Dict[str, Any]]:
    """cut_id/video_id pairs for every metadata record in the batch."""
    members = []
    for sf in snap.pre_publish:
        record = sf.body[0] if isinstance(sf.body, list) and sf.body else sf.body
        if not isinstance(record, dict):
            continue
        fi = record.get("file_info") or {}
        if fi.get("batch_id") == batch_id and fi.get("cut_id"):
            members.append({"cut_id": fi["cut_id"],
                            "video_id": fi.get("video_id")})
    return members


def _ripeness(snap, exp: Dict[str, Any]) -> Optional[Tuple[Dict, List[Dict]]]:
    """Return (test_numbers, control_numbers) when the experiment is
    ready to score, else None.

    Ready = test cut has a mature outcome AND >= MIN_CONTROLS batch-mates
    have mature outcomes.
    """
    exp_id = exp["experiment_id"]
    if ":" not in exp_id:
        return None
    batch_id, test_cut = exp_id.split(":", 1)

    members = _batch_members(snap, batch_id)
    by_cut = {m["cut_id"]: m for m in members}
    test = by_cut.get(test_cut)
    if not test or not test.get("video_id"):
        return None

    vids = [m["video_id"] for m in members if m.get("video_id")]
    outcomes = strategy.outcomes_for_video_ids(vids)

    t_out = outcomes.get(test["video_id"])
    if not t_out or t_out.get("breakout_score") is None or not _mature(t_out):
        return None

    controls = []
    for m in members:
        if m["cut_id"] == test_cut or not m.get("video_id"):
            continue
        o = outcomes.get(m["video_id"])
        if o and o.get("breakout_score") is not None and _mature(o):
            controls.append({"cut_id": m["cut_id"],
                             "video_id": m["video_id"],
                             "breakout_score": o["breakout_score"]})
    if len(controls) < MIN_CONTROLS:
        return None

    test_numbers = {
        "cut_id": test_cut,
        "video_id": test["video_id"],
        "breakout_score": t_out["breakout_score"],
        "views": (t_out.get("raw") or {}).get("views"),
        "published_at": _published_at(t_out),
    }
    return test_numbers, controls


def _artifact_key(experiment_id: str) -> str:
    return experiment_id.replace(":", "__") + ".verdict"


def enumerate_tasks(snap: inputs_mod.Snapshot) -> List[Task]:
    if not snap.pre_publish:
        return []
    try:
        strategy.init_schema()
        designed = [e for e in strategy.list_experiments(snap.channel_handle)
                    if e.get("status") == "designed"]
    except Exception:
        return []

    tasks: List[Task] = []
    for exp in designed:
        ripe = _ripeness(snap, exp)
        if ripe is None:
            continue
        test_numbers, controls = ripe
        input_hash = inputs_mod.hash_inputs({
            "experiment": json.dumps(exp, sort_keys=True, default=str),
            "test": json.dumps(test_numbers, sort_keys=True),
            "controls": json.dumps(sorted(
                (c["cut_id"], c["breakout_score"]) for c in controls)),
        })
        tasks.append(_build_task(snap, exp, test_numbers, controls, input_hash))
    return tasks


def _build_task(snap, exp: Dict[str, Any], test_numbers: Dict[str, Any],
                controls: List[Dict[str, Any]], input_hash: str) -> Task:
    exp_id = exp["experiment_id"]
    key = _artifact_key(exp_id)

    def _run() -> Dict[str, Any]:
        handle = snap.channel_handle
        scores = [c["breakout_score"] for c in controls]
        control_median = statistics.median(scores)
        ratio = (test_numbers["breakout_score"] / control_median
                 if control_median else None)
        numbers = {
            "test": test_numbers,
            "control_n": len(controls),
            "control_median_breakout": control_median,
            "control_range": [min(scores), max(scores)],
            "test_vs_control_median_ratio": ratio,
            "test_rank_in_batch": 1 + sum(
                1 for s in scores if s > test_numbers["breakout_score"]),
        }

        trace = traces.new_trace(TASK_TYPE, inputs={
            "channel_handle": handle, "experiment_id": exp_id,
            "input_hash": input_hash,
        })
        traces.add_step(trace, "numbers", numbers)

        verdict: Dict[str, Any] = {}
        gen_skip: Optional[str] = None
        if not llm.gemini_available():
            gen_skip = "GEMINI_API_KEY missing"
        else:
            try:
                client = llm.get_genai()
                resp = client.models.generate_content(
                    model=config.GEMINI_MODEL_PRIMARY,
                    contents=_VERDICT_PROMPT.format(
                        handle=handle,
                        experiment_json=json.dumps(
                            exp, indent=1, ensure_ascii=False, default=str),
                        numbers_json=json.dumps(numbers, indent=1),
                    ),
                )
                verdict = llm.parse_json_lenient(resp.text or "")
                traces.add_step(trace, "verdict", {
                    "model": config.GEMINI_MODEL_PRIMARY,
                    "verdict": verdict.get("verdict"),
                })
            except Exception as e:
                gen_skip = f"gemini error: {e!r}"
                traces.add_step(trace, "verdict_failed", {"error": str(e)})

        # Ledger write: numbers always land; the narrative is best-effort.
        v = verdict.get("verdict") or "inconclusive"
        conclusion = verdict.get("conclusion") or (
            f"auto: test breakout {test_numbers['breakout_score']} vs "
            f"control median {control_median} (n={len(controls)}); "
            f"LLM verdict unavailable ({gen_skip})")
        status = "concluded" if verdict else "concluded_numbers_only"
        strategy.conclude_experiment(
            exp_id,
            json.dumps({
                "verdict": v,
                "conclusion": conclusion,
                "belief_implication": verdict.get("belief_implication"),
                "numbers": numbers,
            }, ensure_ascii=False),
            status=status,
        )

        payload = {
            "channel_handle": handle,
            "experiment_id": exp_id,
            "hypothesis": exp.get("hypothesis"),
            "verdict": v,
            "conclusion": conclusion,
            "belief_implication": verdict.get("belief_implication"),
            "follow_up_worth_running": verdict.get("follow_up_worth_running"),
            "numbers": numbers,
            "skips": {"generation": gen_skip},
            "trace_id": trace.get("trace_id"),
        }
        recommendations.write(
            CATEGORY, key,
            task_type=TASK_TYPE, input_hash=input_hash, payload=payload)
        traces.finalize(trace, result={"verdict": v})
        return payload

    return Task(
        task_type=TASK_TYPE,
        key=key,
        category=CATEGORY,
        input_hash=input_hash,
        depends_on=[("outcome_sync", f"{snap.channel_handle}.outcome_sync")],
        run=_run,
        description=f"score experiment {exp_id}",
    )
