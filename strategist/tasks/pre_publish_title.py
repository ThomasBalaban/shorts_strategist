"""
Per-pre-publish-clip title recommendation.

For every ``SimpleAutoSubs/shorts_data/shorts_metadata_*.json`` on disk, emit
a verdict on the title simpleautosubs already picked plus 5 ranked
alternatives. Each alternative cites which channel patterns it uses.

Depends on the ``title_pattern_retro`` artifact: that's where ``patterns_to_use``
and ``patterns_to_avoid`` come from. If the patterns artifact doesn't exist
yet, this task degrades — it still emits alternatives but flags that the
channel-pattern grounding was missing.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .. import inputs as inputs_mod
from .. import llm
from .. import recommendations
from .. import traces
from . import title_pattern_retro
from .base import Task

TASK_TYPE = "pre_publish_title"
CATEGORY = "titles"


# ─── prompts ──────────────────────────────────────────────────────────────────

_GENERATE_PROMPT = """\
You are recommending titles for a YouTube Shorts clip on @{handle}, before it
goes live. SimpleAutoSubs has already picked a title; your job is to either
endorse it or propose better alternatives, grounded in the channel's
demonstrated title-performance patterns.

CHANNEL PATTERNS (learned from the published corpus — use these as the
primary basis for ranking):

PATTERNS THAT WORK (median breakout when present > 1.0):
{patterns_to_use}

PATTERNS THAT FAIL (median breakout when present < 1.0):
{patterns_to_avoid}

CLIP CONTEXT (from SimpleAutoSubs's pre-publish metadata):
- Source file: {source_file}
- Final duration: {duration}s
- Current title: "{current_title}"

CLIP INTERPRETATION (what the clip is actually about):
{clip_interpretation}

WHY SIMPLEAUTOSUBS PICKED ITS CURRENT TITLE:
{current_title_reasoning}

PRIOR CANDIDATES SIMPLEAUTOSUBS CONSIDERED (and rejected):
{prior_candidates}

Generate exactly 5 alternative titles ranked by expected performance on this
channel. EACH alternative MUST:
- Cite the specific channel pattern(s) it uses (by ``name`` from the lists above)
- Stay accurate to the clip content (no clickbait that misrepresents)
- Be 60 characters or fewer
- NOT use any of the patterns_to_avoid

Return ONLY a JSON object:
{{
  "ranked_alternatives": [
    {{
      "rank": 1,
      "title": "<the title>",
      "patterns_used": ["<pattern name from patterns_to_use>", ...],
      "rationale": "<one or two sentences — WHY this beats the current title>",
      "accuracy_check": "<one sentence: how this stays true to the clip>"
    }}
  ],
  "current_title_assessment": {{
    "patterns_it_uses":   ["<from patterns_to_use, if any>"],
    "patterns_it_violates": ["<from patterns_to_avoid, if any>"],
    "summary": "<one sentence verdict on the current title>"
  }}
}}
"""


_CRITIQUE_SYSTEM = """\
You are critiquing pre-publish title recommendations. Five alternatives have
been proposed by a different model alongside an assessment of the current
title. Your job is to:

1. Score each alternative against the channel patterns honestly. If an
   alternative claims to use a pattern but doesn't actually exhibit that
   pattern, dock it.
2. Spot accuracy slippage. If an alternative reads as more clickbait than the
   clip actually delivers, flag it.
3. Decide the final verdict: keep the current title, replace with a specific
   alternative (by rank), or call it tied (current title is roughly as good
   as the best alternative).
"""


_CRITIQUE_USER_TEMPLATE = """\
CHANNEL: @{handle}

CHANNEL PATTERNS:
patterns_to_use:
{patterns_to_use_short}
patterns_to_avoid:
{patterns_to_avoid_short}

CLIP CONTEXT:
- Current title: "{current_title}"
- Clip about: {clip_summary}

GENERATOR OUTPUT (5 alternatives + assessment of current):
{generator_json}

Output JSON:
{{
  "alternative_critiques": [
    {{
      "rank": <int>,
      "title": "<verbatim>",
      "actually_uses_claimed_patterns": true | false,
      "accuracy_concern": "<empty string if none>",
      "score_0_100": <int>
    }}
  ],
  "verdict": "keep" | "replace" | "tied",
  "replace_with_rank": <int or null>,
  "verdict_reasoning": "<one or two sentences>"
}}
"""


_CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "alternative_critiques": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rank": {"type": "integer"},
                    "title": {"type": "string"},
                    "actually_uses_claimed_patterns": {"type": "boolean"},
                    "accuracy_concern": {"type": "string"},
                    "score_0_100": {"type": "integer"},
                },
                "required": ["rank", "title", "actually_uses_claimed_patterns", "accuracy_concern", "score_0_100"],
                "additionalProperties": False,
            },
        },
        "verdict": {"type": "string", "enum": ["keep", "replace", "tied"]},
        "replace_with_rank": {"type": ["integer", "null"]},
        "verdict_reasoning": {"type": "string"},
    },
    "required": ["alternative_critiques", "verdict", "replace_with_rank", "verdict_reasoning"],
    "additionalProperties": False,
}


# ─── helpers ──────────────────────────────────────────────────────────────────

def _format_patterns(patterns: List[Dict[str, Any]]) -> str:
    if not patterns:
        return "(none — title-pattern retrospective hasn't run successfully yet)"
    lines = []
    for p in patterns:
        name = p.get("name", "?")
        desc = p.get("description", "")
        median = p.get("median_breakout_when_present")
        median_str = f"{median:.2f}" if isinstance(median, (int, float)) else "?"
        lines.append(f"- {name}: {desc} (median breakout when present: {median_str})")
    return "\n".join(lines)


def _format_patterns_short(patterns: List[Dict[str, Any]]) -> str:
    if not patterns:
        return "  (none)"
    return "\n".join(f"  - {p.get('name','?')}: {p.get('description','')}" for p in patterns)


def _read_title_patterns(channel_handle: str) -> Optional[Dict[str, Any]]:
    art = recommendations.read("channel", f"{channel_handle}.title_patterns")
    if not art:
        return None
    return art.get("payload")


def _key_for(basename: str) -> str:
    """Recommendation file key derived from the source filename.

    e.g. ``shorts_metadata_1.json`` → ``shorts_metadata_1``.
    """
    return basename[:-5] if basename.endswith(".json") else basename


# ─── enumeration ──────────────────────────────────────────────────────────────

def enumerate_tasks(snap: inputs_mod.Snapshot) -> List[Task]:
    if not snap.pre_publish:
        return []

    # The title-patterns artifact is part of the input hash so this task
    # re-runs whenever the channel's learned patterns change.
    patterns_payload = _read_title_patterns(snap.channel_handle)
    patterns_hash = ""
    if patterns_payload is not None:
        patterns_hash = inputs_mod.hash_inputs({
            "patterns_to_use":   json.dumps(patterns_payload.get("patterns_to_use", []),   sort_keys=True),
            "patterns_to_avoid": json.dumps(patterns_payload.get("patterns_to_avoid", []), sort_keys=True),
        })

    tasks: List[Task] = []
    for sf in snap.pre_publish:
        record = sf.body[0] if isinstance(sf.body, list) and sf.body else sf.body
        if not isinstance(record, dict):
            continue
        key = _key_for(sf.basename)
        input_hash = inputs_mod.hash_inputs({
            "source": sf.sha256,
            "patterns": patterns_hash,
        })

        tasks.append(_build_task(snap, sf, record, key, input_hash, patterns_payload))
    return tasks


def _build_task(
    snap: inputs_mod.Snapshot,
    sf: Any,
    record: Dict[str, Any],
    key: str,
    input_hash: str,
    patterns_payload: Optional[Dict[str, Any]],
) -> Task:
    file_info = record.get("file_info", {}) or {}
    current_title = (record.get("title") or "").strip()
    title_analysis = record.get("title_analysis", {}) or {}
    clip_interpretation = title_analysis.get("clip_interpretation") or {}
    current_reasoning = title_analysis.get("chosen_title_reasoning") or ""
    prior_candidates = title_analysis.get("candidates_considered") or []

    def _run() -> Dict[str, Any]:
        trace = traces.new_trace(TASK_TYPE, inputs={
            "channel_handle": snap.channel_handle,
            "source": sf.basename,
            "current_title": current_title,
            "input_hash": input_hash,
        })

        patterns_to_use   = (patterns_payload or {}).get("patterns_to_use", [])
        patterns_to_avoid = (patterns_payload or {}).get("patterns_to_avoid", [])

        # ── generate ──────────────────────────────────────────────────────
        generated: Dict[str, Any] = {}
        gen_skip: Optional[str] = None
        if not llm.gemini_available():
            gen_skip = "GEMINI_API_KEY missing"
        else:
            try:
                import config
                client = llm.get_genai()
                prompt = _GENERATE_PROMPT.format(
                    handle=snap.channel_handle,
                    patterns_to_use=_format_patterns(patterns_to_use),
                    patterns_to_avoid=_format_patterns(patterns_to_avoid),
                    source_file=file_info.get("original_filename", sf.basename),
                    duration=file_info.get("final_duration", "?"),
                    current_title=current_title,
                    clip_interpretation=json.dumps(clip_interpretation, indent=2, ensure_ascii=False)[:2000],
                    current_title_reasoning=str(current_reasoning)[:1500],
                    prior_candidates=json.dumps(prior_candidates, indent=2, ensure_ascii=False)[:1500],
                )
                resp = client.models.generate_content(
                    model=config.GEMINI_MODEL_PRIMARY,
                    contents=prompt,
                )
                generated = llm.parse_json_lenient(resp.text or "")
                traces.add_step(trace, "generate", {
                    "model": config.GEMINI_MODEL_PRIMARY,
                    "n_alternatives": len(generated.get("ranked_alternatives", [])),
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
                import config
                client = llm.get_anthropic()
                clip_summary = (
                    clip_interpretation.get("literal_event")
                    or clip_interpretation.get("summary")
                    or json.dumps(clip_interpretation)[:300]
                )
                user_msg = _CRITIQUE_USER_TEMPLATE.format(
                    handle=snap.channel_handle,
                    patterns_to_use_short=_format_patterns_short(patterns_to_use),
                    patterns_to_avoid_short=_format_patterns_short(patterns_to_avoid),
                    current_title=current_title,
                    clip_summary=clip_summary,
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
                    "verdict": critique.get("verdict"),
                    "replace_with_rank": critique.get("replace_with_rank"),
                    "usage": {
                        "input_tokens":  resp.usage.input_tokens,
                        "output_tokens": resp.usage.output_tokens,
                    },
                })
            except Exception as e:
                crit_skip = f"claude error: {e!r}"
                traces.add_step(trace, "critique_failed", {"error": str(e)})

        # ── merge ─────────────────────────────────────────────────────────
        # Layer the critic's per-alternative scores onto the generator's
        # ranked list so a downstream consumer sees both views in one place.
        scored_by_rank = {c["rank"]: c for c in critique.get("alternative_critiques", [])}
        proposed_alts = generated.get("ranked_alternatives", [])
        partial_critique = bool(scored_by_rank) and len(scored_by_rank) < len(proposed_alts)
        if partial_critique:
            traces.add_step(trace, "partial_critique_detected", {
                "n_proposed": len(proposed_alts),
                "n_critiques_received": len(scored_by_rank),
            })
        merged_alts = []
        for alt in proposed_alts:
            cs = scored_by_rank.get(alt.get("rank"), {})
            merged_alts.append({
                **alt,
                "critic_score": cs.get("score_0_100"),
                "critic_says_uses_patterns": cs.get("actually_uses_claimed_patterns"),
                "critic_accuracy_concern":   cs.get("accuracy_concern"),
            })

        verdict = critique.get("verdict") or ("tied" if generated else "unknown")
        replace_with_rank = critique.get("replace_with_rank")
        recommended_title: Optional[str] = None
        if verdict == "keep":
            recommended_title = current_title
        elif verdict == "replace" and isinstance(replace_with_rank, int):
            for a in merged_alts:
                if a.get("rank") == replace_with_rank:
                    recommended_title = a.get("title")
                    break

        payload = {
            "channel_handle":        snap.channel_handle,
            "source":                sf.basename,
            "current_title":         current_title,
            "current_title_assessment": generated.get("current_title_assessment"),
            "ranked_alternatives":   merged_alts,
            "verdict":               verdict,
            "replace_with_rank":     replace_with_rank,
            "verdict_reasoning":     critique.get("verdict_reasoning"),
            "recommended_title":     recommended_title,
            "patterns_grounded_on": {
                "patterns_to_use_count":   len(patterns_to_use),
                "patterns_to_avoid_count": len(patterns_to_avoid),
                "source_artifact":         f"channel/{snap.channel_handle}.title_patterns" if patterns_payload else None,
            },
            "partial_critique": partial_critique,
            "skips": {
                "generation_skipped_reason": gen_skip,
                "critique_skipped_reason":   crit_skip,
            },
        }

        traces.finalize(trace, result={"verdict": verdict, "n_alternatives": len(merged_alts)})
        recommendations.write(
            CATEGORY, key,
            task_type=TASK_TYPE, input_hash=input_hash,
            payload=payload, trace_id=trace["trace_id"],
        )
        return payload

    return Task(
        task_type=TASK_TYPE,
        key=key,
        category=CATEGORY,
        input_hash=input_hash,
        depends_on=[(title_pattern_retro.TASK_TYPE, f"{snap.channel_handle}.title_patterns")],
        run=_run,
        description=f"title rec for {sf.basename}",
    )
