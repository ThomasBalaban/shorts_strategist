"""
Batch experiment assignment — MISSION Phase 4.

When a batch of >= MIN_BATCH videos has fully shipped, one
generate-then-critique pass (Gemini proposes, Claude kills confounds)
selects 2-4 shorts that NATURALLY embody a variation worth isolating,
assigns each a written hypothesis + prediction BEFORE outcomes exist,
and records the assignments in the strategy.db experiments ledger plus
a per-batch artifact.

Standing rule: the strategist designs experiments; it does not route
traffic. Assignments are labels + predictions on cuts that already
shipped — nothing here edits videos or scheduling.

Re-run semantics: the input hash covers the member metadata files, so a
late video_id stamp re-runs the task; ledger rows are keyed
"<batch_id>:<cut_id>" and INSERT OR REPLACE, so re-picks update in
place (rows for cuts dropped on a re-run linger with status 'designed'
— Phase 5's evaluator treats rows absent from the latest artifact as
superseded).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import config
from .. import inputs as inputs_mod
from .. import llm
from .. import recommendations
from .. import strategy
from .. import traces
from .base import Task

TASK_TYPE = "batch_experiment_assignment"
CATEGORY = "experiments"

MIN_BATCH = 10
MIN_ASSIGNMENTS = 2
MAX_ASSIGNMENTS = 4


# ─── prompts ──────────────────────────────────────────────────────────────────

_GENERATE_PROMPT = """\
You are the experiment designer for the YouTube Shorts channel @{handle}.
A batch of {n_members} shorts (batch_id {batch_id}) has just shipped. Your
job: pick {min_n}-{max_n} of them as NATURAL EXPERIMENTS.

A short is test-worthy when it ALREADY differs from its batch-mates on
exactly ONE interesting axis (title shape, hook type, pacing outlier,
strategist-replaced title, unusual duration) AND the rest of the batch is
a plausible control group. You are labeling variation that exists — you
are NOT inventing edits.

For each assignment, commit to a prediction BEFORE outcomes exist.

CHANNEL CONTEXT (learned patterns + current strategy read):
{channel_context}

PRIOR EXPERIMENTS (do not re-test settled questions; escalate confirmed
weak signals into sharper follow-ups):
{ledger_history}

THE BATCH ({n_members} members; compact per-cut summaries):
{member_table}

Return ONLY this JSON, no fences:
{{
  "selection_rationale": "two sentences on the batch's natural contrasts",
  "assignments": [
    {{
      "cut_id": "cut_xxxxxxxx",
      "hypothesis": "one falsifiable sentence",
      "variable_isolated": "the single axis this cut differs on",
      "expected_outcome": "direction + rough magnitude vs the comparison set",
      "success_metric": "the number to compare (e.g. breakout_score vs control median)",
      "comparison_set": "which batch-mates form the control (description or cut_ids)",
      "confidence": 0.0
    }}
  ]
}}"""

_CRITIQUE_SYSTEM = """\
You are the adversarial reviewer of proposed YouTube Shorts experiments.
Kill assignments that cannot teach anything: confounded picks (the cut
differs from controls on multiple axes), undersized contrasts, variables
that duplicate a prior settled experiment, vague or untestable
predictions. Be strict — a short list of clean experiments beats a long
list of muddy ones. Keep at least one assignment unless every single one
is fatally confounded."""

_CRITIQUE_USER = """\
BATCH MEMBERS:
{member_table}

PRIOR EXPERIMENTS:
{ledger_history}

PROPOSED ASSIGNMENTS:
{generator_json}

For every proposed assignment give a verdict."""

_CRITIQUE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assignment_critiques"],
    "properties": {
        "assignment_critiques": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["cut_id", "verdict", "reason"],
                "properties": {
                    "cut_id":  {"type": "string"},
                    "verdict": {"type": "string",
                                "enum": ["keep", "weaken", "drop"]},
                    "reason":  {"type": "string"},
                },
            },
        },
        "missed_opportunities": {
            "type": "array", "items": {"type": "string"},
        },
    },
}


# ─── helpers ──────────────────────────────────────────────────────────────────

def _member_summary(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fi = record.get("file_info") or {}
    if not fi.get("cut_id"):
        return None
    ed = record.get("editorial_decisions") or {}
    tp = record.get("title_provenance") or {}
    segs = ed.get("trim_segments_kept") or []
    return {
        "cut_id":        fi.get("cut_id"),
        "batch_index":   fi.get("batch_index"),
        "title":         tp.get("shipped_title") or record.get("title"),
        "title_source":  tp.get("title_source"),
        "strategist_verdict": tp.get("strategist_verdict"),
        "n_segments":    len(segs),
        "final_duration": fi.get("final_duration"),
        "punch_point":   ed.get("trim_punch_point"),
        "iterations":    fi.get("iteration"),
        "has_video_id":  bool(fi.get("video_id")),
    }


def _channel_context(handle: str) -> str:
    parts: List[str] = []
    tp = recommendations.read("channel", f"{handle}.title_patterns") or {}
    tpp = tp.get("payload") or {}
    if tpp and not tpp.get("skipped_by_operator"):
        parts.append("title_patterns_to_use: " + json.dumps(
            [p.get("name") for p in tpp.get("patterns_to_use", [])]))
        parts.append("title_patterns_to_avoid: " + json.dumps(
            [p.get("name") for p in tpp.get("patterns_to_avoid", [])]))
    sc = recommendations.read("channel", f"{handle}.scorecard") or {}
    scp = sc.get("payload") or {}
    if scp and not scp.get("skipped_by_operator"):
        if scp.get("headline"):
            parts.append("scorecard_headline: " + scp["headline"])
        subs = scp.get("experiment_substrates") or []
        if subs:
            parts.append("scorecard_experiment_substrates: "
                         + json.dumps(subs))
    return "\n".join(parts) or "(no channel artifacts available)"


def _ledger_history(handle: str) -> str:
    rows = strategy.list_experiments(handle)
    if not rows:
        return "(ledger is empty — this is the first assignment round)"
    compact = [{
        "experiment_id": r["experiment_id"],
        "hypothesis":    r["hypothesis"],
        "status":        r["status"],
        "conclusion":    r["conclusion"],
    } for r in rows[:30]]
    return json.dumps(compact, indent=1, ensure_ascii=False)


# ─── enumeration ──────────────────────────────────────────────────────────────

def enumerate_tasks(snap: inputs_mod.Snapshot) -> List[Task]:
    if not snap.pre_publish:
        return []

    # Group shipped metadata records by batch_id.
    batches: Dict[str, List] = {}
    for sf in snap.pre_publish:
        record = sf.body[0] if isinstance(sf.body, list) and sf.body else sf.body
        if not isinstance(record, dict):
            continue
        fi = record.get("file_info") or {}
        bid = fi.get("batch_id")
        if not bid or not fi.get("batch_size"):
            continue
        batches.setdefault(bid, []).append((sf, record))

    tasks: List[Task] = []
    for bid, members in sorted(batches.items()):
        size = (members[0][1].get("file_info") or {}).get("batch_size") or 0
        if size < MIN_BATCH:
            continue
        if len(members) < size:
            continue  # not all members' metadata present yet
        if not all((r.get("file_info") or {}).get("shipped_at")
                   for _, r in members):
            continue  # fires only once the whole batch has shipped

        input_hash = inputs_mod.hash_inputs({
            "members": json.dumps(sorted(sf.sha256 for sf, _ in members)),
        })
        tasks.append(_build_task(snap, bid, members, input_hash))
    return tasks


def _build_task(snap, batch_id: str, members: List, input_hash: str) -> Task:

    def _run() -> Dict[str, Any]:
        handle = snap.channel_handle
        summaries = [s for s in (_member_summary(r) for _, r in members) if s]
        member_table = json.dumps(summaries, indent=1, ensure_ascii=False)
        channel_context = _channel_context(handle)
        ledger_history = _ledger_history(handle)

        trace = traces.new_trace(TASK_TYPE, inputs={
            "channel_handle": handle,
            "batch_id": batch_id,
            "n_members": len(summaries),
            "input_hash": input_hash,
        })

        # ── generate ──────────────────────────────────────────────────
        generated: Dict[str, Any] = {}
        gen_skip: Optional[str] = None
        if not llm.gemini_available():
            gen_skip = "GEMINI_API_KEY missing"
        else:
            try:
                client = llm.get_genai()
                prompt = _GENERATE_PROMPT.format(
                    handle=handle, batch_id=batch_id,
                    n_members=len(summaries),
                    min_n=MIN_ASSIGNMENTS, max_n=MAX_ASSIGNMENTS,
                    channel_context=channel_context,
                    ledger_history=ledger_history,
                    member_table=member_table,
                )
                resp = client.models.generate_content(
                    model=config.GEMINI_MODEL_PRIMARY, contents=prompt)
                generated = llm.parse_json_lenient(resp.text or "")
                traces.add_step(trace, "generate", {
                    "model": config.GEMINI_MODEL_PRIMARY,
                    "n_assignments": len(generated.get("assignments", [])),
                })
            except Exception as e:
                gen_skip = f"gemini error: {e!r}"
                traces.add_step(trace, "generate_failed", {"error": str(e)})

        # ── critique ──────────────────────────────────────────────────
        critique: Dict[str, Any] = {}
        crit_skip: Optional[str] = None
        if not generated.get("assignments"):
            crit_skip = "nothing to critique"
        elif not llm.anthropic_available():
            crit_skip = "ANTHROPIC_API_KEY missing"
        else:
            try:
                client = llm.get_anthropic()
                resp = client.messages.create(
                    model=config.ANTHROPIC_MODEL_CRITIC,
                    max_tokens=2048,
                    system=[{
                        "type": "text",
                        "text": _CRITIQUE_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    output_config={"format": {
                        "type": "json_schema", "schema": _CRITIQUE_SCHEMA}},
                    messages=[{"role": "user", "content": _CRITIQUE_USER.format(
                        member_table=member_table,
                        ledger_history=ledger_history,
                        generator_json=json.dumps(
                            generated, indent=1, ensure_ascii=False),
                    )}],
                )
                text = next(
                    (b.text for b in resp.content if b.type == "text"), "")
                critique = llm.parse_json_lenient(text)
                traces.add_step(trace, "critique", {
                    "model": config.ANTHROPIC_MODEL_CRITIC,
                    "usage": {"input_tokens": resp.usage.input_tokens,
                              "output_tokens": resp.usage.output_tokens},
                })
            except Exception as e:
                crit_skip = f"claude error: {e!r}"
                traces.add_step(trace, "critique_failed", {"error": str(e)})

        # ── merge: drop critic-killed assignments ─────────────────────
        verdicts = {c["cut_id"]: c for c
                    in critique.get("assignment_critiques", [])}
        kept: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []
        for a in generated.get("assignments", [])[:MAX_ASSIGNMENTS]:
            v = verdicts.get(a.get("cut_id"), {})
            a["critic_verdict"] = v.get("verdict", "uncritiqued")
            a["critic_reason"] = v.get("reason")
            if v.get("verdict") == "drop":
                dropped.append(a)
            else:
                kept.append(a)

        # ── ledger rows (dormant table wakes up here) ─────────────────
        ledger_ids: List[str] = []
        for a in kept:
            exp_id = f"{batch_id}:{a.get('cut_id')}"
            try:
                strategy.save_experiment(
                    experiment_id=exp_id,
                    channel_handle=handle,
                    hypothesis=a.get("hypothesis") or "",
                    arms=[
                        {"role": "test", "cut_ids": [a.get("cut_id")],
                         "variable_isolated": a.get("variable_isolated"),
                         "expected_outcome": a.get("expected_outcome"),
                         "confidence": a.get("confidence")},
                        {"role": "control",
                         "comparison_set": a.get("comparison_set")},
                    ],
                    success_metric=a.get("success_metric") or "",
                )
                ledger_ids.append(exp_id)
            except Exception as e:
                traces.add_step(trace, "ledger_write_failed", {
                    "experiment_id": exp_id, "error": str(e)})

        payload = {
            "channel_handle": handle,
            "batch_id": batch_id,
            "n_members": len(summaries),
            "selection_rationale": generated.get("selection_rationale"),
            "assignments": kept,
            "dropped_by_critic": dropped,
            "missed_opportunities": critique.get("missed_opportunities", []),
            "ledger_experiment_ids": ledger_ids,
            "skips": {"generation": gen_skip, "critique": crit_skip},
            "trace_id": trace.get("trace_id"),
        }
        recommendations.write(
            CATEGORY, batch_id,
            task_type=TASK_TYPE, input_hash=input_hash, payload=payload)
        traces.finalize(trace, result={
            "n_assigned": len(kept), "n_dropped": len(dropped)})
        return payload

    return Task(
        task_type=TASK_TYPE,
        key=batch_id,
        category=CATEGORY,
        input_hash=input_hash,
        run=_run,
        description=f"experiment assignment for {batch_id} "
                    f"({len(members)} shorts)",
    )
