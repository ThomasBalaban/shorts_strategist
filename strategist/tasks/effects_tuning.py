"""
Effects tuning — MISSION Phase 6 (opt-in via the editor's Thinker
Tuning toggle).

When experiments have CONCLUDED, this task may propose new values for
the editor's fx_* knobs. Hard rules:
  - proposals must cite the concluded experiment(s) that justify them
    (unjustified proposals are dropped in Python, not trusted to the
    prompt);
  - at most MAX_CHANGES knobs per round, each clamped to the editor's
    safety bounds (mirrored here);
  - the editor applies directives only while the operator's
    fx_thinker_control toggle is ON, and stamps full provenance into
    every video's metadata.

Until the first experiment concludes, enumerate() returns nothing.
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

TASK_TYPE = "effects_tuning"
CATEGORY = "channel"

MAX_CHANGES = 3

# Mirror of the editor's FX_BOUNDS (api_server.py) — defense in depth;
# the editor clamps again on its side.
FX_BOUNDS: Dict[str, tuple] = {
    "fx_trim_target_min_s": (5.0, 30.0),
    "fx_trim_target_max_s": (15.0, 60.0),
    "fx_trim_max_pause_s": (0.5, 4.0),
    "fx_trim_end_within_s": (1.0, 8.0),
    "fx_pre_punch_beat_s": (0.0, 4.0),
    "fx_zoom_intensity": (0.3, 2.5),
    "fx_zoom_ramp_portion": (0.1, 0.95),
    "fx_end_card_seconds": (1.0, 4.0),
}

FX_DEFAULTS = {
    "fx_trim_target_min_s": 12.0, "fx_trim_target_max_s": 45.0,
    "fx_trim_max_pause_s": 1.5, "fx_trim_end_within_s": 3.0,
    "fx_pre_punch_beat_s": 1.5, "fx_zoom_intensity": 1.0,
    "fx_zoom_ramp_portion": 0.6, "fx_end_card_seconds": 2.0,
}

_PROMPT = """\
You tune the automated shorts editor for @{handle}. Below are the
editor's tunable knobs (current defaults and allowed ranges) and the
channel's CONCLUDED pre-registered experiments — real causal evidence.

Propose a new value ONLY for a knob where a concluded experiment
directly justifies the change. No experiment, no proposal. At most
{max_changes} knobs. Conservative moves beat bold ones: this changes
every future video.

KNOBS (name: default, [min, max]):
{knob_catalog}

CONCLUDED EXPERIMENTS:
{concluded}

Return ONLY this JSON, no fences:
{{
  "directives": {{
    "<knob_name>": {{
      "value": 0.0,
      "justified_by": ["<experiment_id>", "..."],
      "reasoning": "one sentence tying the experiment result to this value"
    }}
  }},
  "no_change_reason": "fill ONLY if directives is empty"
}}"""


def enumerate_tasks(snap: inputs_mod.Snapshot) -> List[Task]:
    try:
        blob, _ = strategy.concluded_digest(snap.channel_handle)
    except Exception:
        return []
    if not blob:
        return []  # no concluded experiments yet — nothing to tune from

    input_hash = inputs_mod.hash_inputs({"concluded": blob})
    key = f"{snap.channel_handle}.effects_directives"

    def _run() -> Dict[str, Any]:
        handle = snap.channel_handle
        trace = traces.new_trace(TASK_TYPE, inputs={
            "channel_handle": handle, "input_hash": input_hash,
        })

        generated: Dict[str, Any] = {}
        gen_skip: Optional[str] = None
        if not llm.gemini_available():
            gen_skip = "GEMINI_API_KEY missing"
        else:
            try:
                client = llm.get_genai()
                knob_catalog = "\n".join(
                    f"- {k}: {FX_DEFAULTS[k]}, "
                    f"[{FX_BOUNDS[k][0]}, {FX_BOUNDS[k][1]}]"
                    for k in FX_DEFAULTS)
                resp = client.models.generate_content(
                    model=config.GEMINI_MODEL_PRIMARY,
                    contents=_PROMPT.format(
                        handle=handle, max_changes=MAX_CHANGES,
                        knob_catalog=knob_catalog, concluded=blob),
                )
                generated = llm.parse_json_lenient(resp.text or "")
                traces.add_step(trace, "generate", {
                    "model": config.GEMINI_MODEL_PRIMARY,
                    "n_proposed": len(generated.get("directives") or {}),
                })
            except Exception as e:
                gen_skip = f"gemini error: {e!r}"
                traces.add_step(trace, "generate_failed", {"error": str(e)})

        # Python-side enforcement: bounds, provenance, change cap.
        valid_ids = {e["experiment_id"]
                     for e in strategy.list_experiments(handle)
                     if str(e.get("status", "")).startswith("concluded")}
        directives: Dict[str, Any] = {}
        dropped: List[Dict[str, Any]] = []
        for k, d in list((generated.get("directives") or {}).items()):
            if len(directives) >= MAX_CHANGES:
                break
            if k not in FX_BOUNDS or not isinstance(d, dict):
                continue
            cited = [x for x in (d.get("justified_by") or [])
                     if x in valid_ids]
            if not cited:
                dropped.append({"knob": k,
                                "reason": "no valid concluded experiment cited"})
                continue
            lo, hi = FX_BOUNDS[k]
            try:
                value = min(hi, max(lo, float(d.get("value"))))
            except (TypeError, ValueError):
                continue
            directives[k] = {
                "value": value,
                "justified_by": cited,
                "reasoning": d.get("reasoning"),
            }

        payload = {
            "channel_handle": handle,
            "directives": directives,
            "dropped": dropped,
            "no_change_reason": generated.get("no_change_reason"),
            "based_on_experiments": sorted(valid_ids),
            "skips": {"generation": gen_skip},
            "trace_id": trace.get("trace_id"),
        }
        recommendations.write(
            CATEGORY, key,
            task_type=TASK_TYPE, input_hash=input_hash, payload=payload)
        traces.finalize(trace, result={"n_directives": len(directives)})
        return payload

    return [Task(
        task_type=TASK_TYPE,
        key=key,
        category=CATEGORY,
        input_hash=input_hash,
        run=_run,
        description=f"propose fx knob changes for @{snap.channel_handle} "
                    f"from concluded experiments",
    )]
