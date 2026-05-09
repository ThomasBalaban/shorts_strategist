"""
Capability-gap recommendations for shorts-auto-editor.

Crosses the analyzer's per-tag breakout signals against the shorts-auto-editor
capability manifest. Output is the SHORT list — top 3 (up to 5 if there's a
clear case) capabilities to consider building, ranked by leverage.

Design principles:
- Pre-compute the score table in Python (lift × sample × avg breakout) so
  the LLM is doing interpretation, not arithmetic.
- Keep negative signals (tags MORE common in losers than winners) on a
  separate "do not build" list so the generator isn't tempted to recommend
  loser patterns just because we lack the capability.
- Bias toward small numbers — the user wants to build slowly. The prompt
  asks for 3, allows up to 5 only when the data clearly justifies more.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
from typing import Any, Dict, List, Optional, Tuple

import config

from .. import inputs as inputs_mod
from .. import llm
from .. import recommendations
from .. import traces
from .base import Task

TASK_TYPE = "capability_gaps"
CATEGORY = "capabilities"

# Maximum gaps to ask the generator to surface. The prompt encourages 3.
MAX_GAPS = 5
TARGET_GAPS = 3

# Coverage statuses that are eligible to be "built." `out_of_scope_content`
# and `depends_on_prompt` are excluded — those aren't capabilities to add.
ELIGIBLE_COVERAGE = {"not_supported", "partially_supported"}


# ─── manifest loading ────────────────────────────────────────────────────────

def _load_manifest() -> Optional[Tuple[Dict[str, Any], str]]:
    path = config.CAPABILITY_MANIFEST_PATH
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        raw = f.read()
    sha = hashlib.sha256(raw).hexdigest()
    return json.loads(raw.decode("utf-8")), sha


# ─── pre-computation ─────────────────────────────────────────────────────────

def _tag_stats(tag_freq: Dict[str, Any], axis: str, tag: str) -> Optional[Dict[str, Any]]:
    """Pull the per-tag stats block from synthesis.tag_frequencies."""
    axis_block = tag_freq.get(axis)
    if not isinstance(axis_block, dict):
        return None
    block = axis_block.get(tag)
    if not isinstance(block, dict):
        return None
    return block


def _build_candidate_table(manifest: Dict[str, Any],
                            synthesis: Dict[str, Any]
                            ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (build_candidates, do_not_build_warnings).

    Both lists are pre-scored and sorted. Build candidates: positive signals
    (top_rate >= bottom_rate) eligible for capability addition. Warnings:
    negative signals where adding the capability would likely hurt.
    """
    tag_freq = synthesis.get("tag_frequencies") or {}
    coverage_rows = manifest.get("analyzer_tag_coverage") or []

    build: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    for cov in coverage_rows:
        if not isinstance(cov, dict):
            continue
        if cov.get("coverage") not in ELIGIBLE_COVERAGE:
            continue
        axis = cov.get("axis")
        tag  = cov.get("tag")
        if not axis or not tag:
            continue
        stats = _tag_stats(tag_freq, axis, tag)
        if not stats:
            continue

        top    = (stats.get("top_quintile")    or {}).get("rate") or 0.0
        bottom = (stats.get("bottom_quintile") or {}).get("rate") or 0.0
        overall = (stats.get("overall")        or {}).get("rate") or 0.0
        n_top   = (stats.get("top_quintile")   or {}).get("count") or 0
        avg_bo  = stats.get("avg_breakout_score_when_present") or 0.0
        lift_top = stats.get("lift_top_vs_overall") or 0.0

        row = {
            "axis":     axis,
            "tag":      tag,
            "coverage": cov.get("coverage"),
            "implementation_note": cov.get("implementation"),
            "manifest_notes":      cov.get("notes"),
            "top_rate":    round(top, 3),
            "bottom_rate": round(bottom, 3),
            "overall_rate": round(overall, 3),
            "lift_top_vs_overall": round(lift_top, 3),
            "n_in_top":            n_top,
            "avg_breakout_when_present": round(avg_bo, 3),
        }

        if bottom > top:
            # Negative signal: the tag dominates losers more than winners.
            row["severity"] = round(bottom - top, 3)
            warnings.append(row)
        else:
            # Positive signal: leverage = lift × sample × absolute breakout
            score = lift_top * max(n_top, 1) * max(avg_bo, 1.0)
            row["leverage_score"] = round(score, 2)
            build.append(row)

    build.sort(key=lambda r: -(r.get("leverage_score") or 0))
    warnings.sort(key=lambda r: -(r.get("severity") or 0))
    return build, warnings


def _summarize_manifest(manifest: Dict[str, Any]) -> str:
    """Compact textual summary of what shorts-auto-editor *can* do, fed to the
    generator so its build proposals are grounded in the existing modules."""
    pc = manifest.get("production_capabilities") or {}
    lines = []
    for section, body in pc.items():
        if isinstance(body, dict):
            mod = body.get("module") or body.get("modules") or "?"
            summary = body.get("summary") or body.get("strategy") or ""
            lines.append(f"- {section} ({mod}): {summary[:200]}")
    excluded = manifest.get("deliberately_excluded") or {}
    if excluded:
        lines.append("Out-of-scope on purpose:")
        for k, v in excluded.items():
            lines.append(f"  · {k}: {', '.join(v)[:200]}")
    return "\n".join(lines)


# ─── prompts ──────────────────────────────────────────────────────────────────

_GENERATE_PROMPT = """\
You are recommending the SHORT LIST of editing capabilities to add to
shorts-auto-editor based on what the analyzer's data shows.

CHANNEL: @{handle}
N candidate gaps under review: {n_candidates}

CURRENT shorts-auto-editor CAPABILITIES (relevant excerpts):
{capability_summary}

CANDIDATE GAPS (top {n_shown} by leverage_score = lift × n_in_top × avg_breakout_when_present;
each tag is something shorts-auto-editor's manifest reports as not_supported or partially_supported,
and which the analyzer's data shows correlates with breakout performance):
{candidate_table}

NEGATIVE SIGNALS (DO NOT propose building these — they are MORE common in
losers than winners; recommending them would actively hurt the channel):
{negative_table}

Pick the **top {target} gaps** to recommend building. You may go up to {max_gaps}
ONLY if there's a clear, evidence-backed case for additional ones. The
operator wants to build SLOWLY — a focused 3-item backlog beats a 10-item
wishlist.

For each chosen gap, propose:
- A concrete build target: which shorts-auto-editor module would house it, and
  what minimal primitive needs to exist
- The evidence (cite the candidate-table numbers)
- An experiment that would VALIDATE whether adding it actually moves the
  needle on this channel (concrete A/B substrate, not generic)
- An effort estimate: "small" (a few hours), "medium" (a day), "large"
  (multi-day or new subsystem)

Return JSON only, no prose, no code fences:
{{
  "headline": "<ONE sentence — the single biggest gap and why it matters>",
  "prioritized_gaps": [
    {{
      "rank": 1,
      "axis": "<from candidate row>",
      "tag":  "<from candidate row>",
      "build_proposal":         "<concrete target>",
      "implementation_module":  "<shorts-auto-editor module path>",
      "rationale":              "<one or two sentences>",
      "evidence_cited":         {{"lift_top_vs_overall": <num>, "n_in_top": <int>, "avg_breakout_when_present": <num>}},
      "experiment_to_validate": "<one or two sentences, concrete>",
      "effort_estimate":        "small" | "medium" | "large"
    }}
  ]
}}
"""


_CRITIQUE_SYSTEM = """\
You are critiquing capability-build proposals. Your job is to challenge each
proposal on:

1. EVIDENCE STRENGTH — is n_in_top >= 5? lift_top_vs_overall >= 1.5? Below
   those thresholds the signal is directional, not strong.
2. CONFOUNDS — could the tag's correlation be explained by content / IP /
   timing rather than the editing capability itself?
3. OPPORTUNITY COST — would building this distract from higher-leverage
   moves on the same backlog?
4. IMPLEMENTATION PLAUSIBILITY — is the proposed module/primitive a
   reasonable fit, or is the proposer hand-waving?

Be honest. The operator wants a SHORT list. If a gap is well-supported,
keep it. If it's overreaching, weaken it (narrowed scope). If it's
unsupported, drop it.
"""


_CRITIQUE_USER_TEMPLATE = """\
The same data the generator saw is below.

CANDIDATE TABLE (top by leverage):
{candidate_table}

NEGATIVE SIGNALS:
{negative_table}

PROPOSED GAPS:
{generator_json}

Output JSON:
{{
  "headline_critique": "<one or two sentences — is the headline correct?>",
  "gap_critiques": [
    {{
      "rank":             <int>,
      "tag":              "<verbatim>",
      "verdict":          "keep" | "weaken" | "drop",
      "narrowed_proposal":"<only if verdict=weaken; otherwise empty string>",
      "reasoning":        "<one or two sentences>"
    }}
  ],
  "missed_gaps_worth_considering": ["<axis/tag the generator skipped that the data clearly supports>"]
}}
"""


_CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline_critique": {"type": "string"},
        "gap_critiques": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rank":              {"type": "integer"},
                    "tag":               {"type": "string"},
                    "verdict":           {"type": "string", "enum": ["keep", "weaken", "drop"]},
                    "narrowed_proposal": {"type": "string"},
                    "reasoning":         {"type": "string"},
                },
                "required": ["rank", "tag", "verdict", "narrowed_proposal", "reasoning"],
                "additionalProperties": False,
            },
        },
        "missed_gaps_worth_considering": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline_critique", "gap_critiques", "missed_gaps_worth_considering"],
    "additionalProperties": False,
}


# ─── enumeration ─────────────────────────────────────────────────────────────

def enumerate_tasks(snap: inputs_mod.Snapshot) -> List[Task]:
    if not snap.per_short or not snap.synthesis:
        return []

    loaded = _load_manifest()
    if loaded is None:
        # No manifest = no task. Without it the gap math has no reference.
        return []
    manifest, manifest_sha = loaded

    input_hash = inputs_mod.hash_inputs({
        "per_short": snap.per_short.sha256,
        "synthesis": snap.synthesis.sha256,
        "manifest":  manifest_sha,
    })
    key = f"{snap.channel_handle}.capability_gaps"

    def _run() -> Dict[str, Any]:
        synthesis = snap.synthesis.body
        build_candidates, warnings = _build_candidate_table(manifest, synthesis)

        # We feed the generator the top ~12 to keep the prompt scoped, but
        # the candidate count we surface in the artifact reflects the full
        # eligible universe so the user sees what was considered.
        shown = build_candidates[:12]
        warnings_shown = warnings[:8]

        trace = traces.new_trace(TASK_TYPE, inputs={
            "channel_handle": snap.channel_handle,
            "manifest_version": manifest.get("manifest_version"),
            "n_candidates": len(build_candidates),
            "n_negative_signals": len(warnings),
            "input_hash": input_hash,
        })
        traces.add_step(trace, "precompute", {
            "n_eligible_build_candidates": len(build_candidates),
            "n_negative_signals":          len(warnings),
            "n_shown_to_generator":        len(shown),
        })

        # ── generate ──────────────────────────────────────────────────────
        generated: Dict[str, Any] = {}
        gen_skip: Optional[str] = None
        if not llm.gemini_available():
            gen_skip = "GEMINI_API_KEY missing"
        else:
            try:
                client = llm.get_genai()
                prompt = _GENERATE_PROMPT.format(
                    handle=snap.channel_handle,
                    n_candidates=len(build_candidates),
                    n_shown=len(shown),
                    capability_summary=_summarize_manifest(manifest),
                    candidate_table=json.dumps(shown, indent=2, ensure_ascii=False),
                    negative_table=json.dumps(warnings_shown, indent=2, ensure_ascii=False),
                    target=TARGET_GAPS,
                    max_gaps=MAX_GAPS,
                )
                resp = client.models.generate_content(
                    model=config.GEMINI_MODEL_PRIMARY,
                    contents=prompt,
                )
                generated = llm.parse_json_lenient(resp.text or "")
                traces.add_step(trace, "generate", {
                    "model": config.GEMINI_MODEL_PRIMARY,
                    "n_gaps": len(generated.get("prioritized_gaps", [])),
                })
            except Exception as e:
                gen_skip = f"gemini error: {e!r}"
                traces.add_step(trace, "generate_failed", {"error": str(e)})

        # ── critique ──────────────────────────────────────────────────────
        critique: Dict[str, Any] = {}
        crit_skip: Optional[str] = None
        if not generated:
            crit_skip = "nothing to critique"
        elif not llm.anthropic_available():
            crit_skip = "ANTHROPIC_API_KEY missing"
        else:
            try:
                client = llm.get_anthropic()
                user_msg = _CRITIQUE_USER_TEMPLATE.format(
                    candidate_table=json.dumps(shown, indent=2, ensure_ascii=False),
                    negative_table=json.dumps(warnings_shown, indent=2, ensure_ascii=False),
                    generator_json=json.dumps(generated, indent=2, ensure_ascii=False),
                )
                resp = client.messages.create(
                    model=config.ANTHROPIC_MODEL_CRITIC,
                    max_tokens=2048,
                    system=[{
                        "type": "text",
                        "text": _CRITIQUE_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    output_config={"format": {"type": "json_schema", "schema": _CRITIQUE_SCHEMA}},
                    messages=[{"role": "user", "content": user_msg}],
                )
                text = next((b.text for b in resp.content if b.type == "text"), "")
                critique = llm.parse_json_lenient(text)
                traces.add_step(trace, "critique", {
                    "model": config.ANTHROPIC_MODEL_CRITIC,
                    "n_critiques": len(critique.get("gap_critiques", [])),
                    "usage": {
                        "input_tokens":  resp.usage.input_tokens,
                        "output_tokens": resp.usage.output_tokens,
                    },
                })
            except Exception as e:
                crit_skip = f"claude error: {e!r}"
                traces.add_step(trace, "critique_failed", {"error": str(e)})

        # ── merge ─────────────────────────────────────────────────────────
        proposed = generated.get("prioritized_gaps", [])
        crit_by_rank = {c["rank"]: c for c in critique.get("gap_critiques", [])}
        partial_critique = bool(crit_by_rank) and len(crit_by_rank) < len(proposed)
        if partial_critique:
            traces.add_step(trace, "partial_critique_detected", {
                "n_proposed": len(proposed),
                "n_critiques": len(crit_by_rank),
            })

        merged: List[Dict[str, Any]] = []
        for g in proposed:
            cs = crit_by_rank.get(g.get("rank"), {})
            verdict = cs.get("verdict", "unreviewed")
            if verdict == "drop":
                continue
            entry = {**g, "critic_verdict": verdict, "critic_reasoning": cs.get("reasoning")}
            if verdict == "weaken" and cs.get("narrowed_proposal"):
                entry["build_proposal"] = cs["narrowed_proposal"]
            merged.append(entry)

        payload = {
            "channel_handle":         snap.channel_handle,
            "manifest_version":       manifest.get("manifest_version"),
            "n_candidate_gaps":       len(build_candidates),
            "n_negative_signals":     len(warnings),
            "headline":               generated.get("headline"),
            "headline_critique":      critique.get("headline_critique"),
            "prioritized_gaps":       merged,
            "warnings_against_building": warnings_shown,
            "missed_gaps_worth_considering":
                critique.get("missed_gaps_worth_considering", []),
            "partial_critique":       partial_critique,
            "skips": {
                "generation_skipped_reason": gen_skip,
                "critique_skipped_reason":   crit_skip,
            },
        }

        traces.finalize(trace, result={
            "headline":     generated.get("headline"),
            "n_gaps":       len(merged),
        })
        recommendations.write(
            CATEGORY, key,
            task_type=TASK_TYPE, input_hash=input_hash,
            payload=payload, trace_id=trace["trace_id"],
        )
        return payload

    return [Task(
        task_type=TASK_TYPE,
        key=key,
        category=CATEGORY,
        input_hash=input_hash,
        depends_on=[],  # Manifest + analyzer signals are independent of other tasks
        run=_run,
        description=f"capability gaps for {snap.channel_handle}",
    )]
