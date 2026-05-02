"""
Corpus-level retrospective on title patterns.

Reads every published short's title + breakout_score, asks Gemini to surface
title-structure patterns that correlate with above- and below-median
performance, then asks Claude to challenge those claims (thin evidence,
confounds, tautologies). The merged result is what downstream tasks like
``pre_publish_title`` use to know what title shapes work on this channel.

One task per channel. Inputs hashed: per-short corpus + synthesis. If either
LLM key is missing, the task still writes an artifact recording what it
could and couldn't do.
"""
from __future__ import annotations

import json
import statistics
from typing import Any, Dict, List, Optional

from .. import inputs as inputs_mod
from .. import llm
from .. import recommendations
from .. import traces
from .base import Task

TASK_TYPE = "title_pattern_retro"
CATEGORY = "channel"


# ─── prompts ──────────────────────────────────────────────────────────────────

_GENERATE_PROMPT = """\
You are analyzing the title corpus of the YouTube Shorts channel @{handle} to
extract TITLE PATTERNS that correlate with breakout performance.

A breakout_score is a multiple of channel-median views: 1.0 = at the median,
>1 = above, <1 = below. Channel size: {n} shorts. Channel-median breakout: {median:.2f}.

CHANNEL SYNTHESIS (corpus-level findings the analyzer already produced — use
as background, but your job is to focus on title STRUCTURE, not content):
{synthesis_summary}

ALL SHORTS (sorted by breakout descending; "title" | breakout):
{title_list}

A "title pattern" is a STRUCTURAL/LEXICAL/RHETORICAL property of the title
TEXT itself. Examples of valid patterns:
  - first-person present tense ("I get clapped by ...")
  - reaction-with-em-dash structure ("Chat said X — and then Y")
  - specific lexical hook ("the moment / the second / instantly")
  - punctuation/length ("under 40 chars + no punctuation")

Examples of INVALID patterns (these are content, not title patterns, and the
analyzer already covers them):
  - "videos about X game"
  - "clips with chat reactions"
  - "high-energy moments"

Identify 3-5 ``patterns_to_use`` (correlate with above-median performance)
and 2-4 ``patterns_to_avoid`` (correlate with below-median performance).
Each pattern MUST cite at least 2 actual titles from the list as evidence.

Return ONLY a JSON object, no prose, no code fences:
{{
  "patterns_to_use": [
    {{
      "name": "<short_slug>",
      "description": "<one-sentence definition of the pattern>",
      "evidence_titles_above_median": ["<verbatim title>", ...],
      "evidence_titles_below_median": ["<verbatim title or empty list>"],
      "median_breakout_when_present": <number>,
      "rationale": "<why this might work on this channel>"
    }}
  ],
  "patterns_to_avoid": [ ... same shape but inverted ... ],
  "open_questions": ["<gaps you'd want more data to confirm>"]
}}
"""


_CRITIQUE_SYSTEM = """\
You are critiquing claimed title patterns produced by a different model from a
corpus of YouTube Shorts titles + breakout scores. Your job is to find:

1. Patterns whose evidence is too thin (n=1 or n=2 is not a pattern).
2. Confounded patterns (the title shape is correlated with content/series/
   format that's the real cause, not the title text itself).
3. Tautologies ("titles in the top quintile have... high breakout scores").
4. Patterns that have prominent counter-examples in the same corpus.

Be honest. If a claim is bad, drop it. If two are equally well-supported,
mark them both keep.
"""


_CRITIQUE_USER_TEMPLATE = """\
CHANNEL: @{handle}   |   N shorts: {n}   |   median breakout: {median:.2f}

CLAIMED PATTERNS (from the generator):
{claims_json}

THE CORPUS (full title list, sorted desc by breakout):
{title_list}

Output JSON:
{{
  "critiques": [
    {{
      "name": "<the pattern name verbatim>",
      "side": "use" | "avoid",
      "verdict": "keep" | "weaken" | "drop",
      "reasoning": "<one or two sentences>",
      "narrowed_description": "<only if verdict=weaken; otherwise empty string>"
    }}
  ],
  "missing_patterns": ["<patterns the generator missed that the corpus actually supports>"]
}}
"""


_CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "critiques": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name":      {"type": "string"},
                    "side":      {"type": "string", "enum": ["use", "avoid"]},
                    "verdict":   {"type": "string", "enum": ["keep", "weaken", "drop"]},
                    "reasoning": {"type": "string"},
                    "narrowed_description": {"type": "string"},
                },
                "required": ["name", "side", "verdict", "reasoning", "narrowed_description"],
                "additionalProperties": False,
            },
        },
        "missing_patterns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["critiques", "missing_patterns"],
    "additionalProperties": False,
}


# ─── helpers ──────────────────────────────────────────────────────────────────

def _format_title_list(shorts: Dict[str, Dict[str, Any]], limit: int = 125) -> str:
    rows = []
    for s in shorts.values():
        title = (s.get("title") or "").strip()
        bo = s.get("breakout_score")
        if not title or not isinstance(bo, (int, float)):
            continue
        rows.append((bo, title))
    rows.sort(key=lambda r: r[0], reverse=True)
    rows = rows[:limit]
    return "\n".join(f'"{t}" | {bo:.2f}' for bo, t in rows)


def _summarize_synthesis(syn: Optional[Dict[str, Any]]) -> str:
    if not syn:
        return "(synthesis unavailable)"
    narrative = syn.get("narrative")
    if isinstance(narrative, dict):
        # Pull the most useful free-text fields if present.
        pieces = []
        for k in ("what_works", "what_doesnt", "summary", "headline"):
            v = narrative.get(k)
            if isinstance(v, str) and v.strip():
                pieces.append(f"{k}: {v.strip()}")
        if pieces:
            return "\n".join(pieces)
    if isinstance(narrative, str):
        return narrative.strip()[:2000]
    return "(synthesis present but narrative empty)"


def _merge_with_critique(
    generated: Dict[str, Any],
    critique: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply Claude's verdicts to Gemini's pattern lists.

    keep   → unchanged
    weaken → description replaced with critic's narrowed_description
    drop   → removed
    """
    verdict_by_name = {c["name"]: c for c in critique.get("critiques", [])}
    out = {"patterns_to_use": [], "patterns_to_avoid": []}
    for side, key in (("use", "patterns_to_use"), ("avoid", "patterns_to_avoid")):
        for p in generated.get(key, []):
            name = p.get("name")
            v = verdict_by_name.get(name)
            if not v or v.get("side") != side:
                # No critique for this pattern — keep as-is, mark unreviewed.
                out[key].append({**p, "critic_verdict": "unreviewed"})
                continue
            verdict = v.get("verdict")
            if verdict == "drop":
                continue
            merged = {**p, "critic_verdict": verdict, "critic_reasoning": v.get("reasoning")}
            if verdict == "weaken" and v.get("narrowed_description"):
                merged["description"] = v["narrowed_description"]
            out[key].append(merged)
    out["missing_patterns_flagged_by_critic"] = critique.get("missing_patterns", [])
    return out


# ─── enumeration ──────────────────────────────────────────────────────────────

def enumerate_tasks(snap: inputs_mod.Snapshot) -> List[Task]:
    if not snap.per_short:
        return []

    input_hash = inputs_mod.hash_inputs({
        "per_short": snap.per_short.sha256,
        "synthesis": snap.synthesis.sha256 if snap.synthesis else "",
    })
    key = f"{snap.channel_handle}.title_patterns"

    def _run() -> Dict[str, Any]:
        shorts = snap.shorts_by_video_id()
        scores = [s.get("breakout_score") for s in shorts.values()
                  if isinstance(s.get("breakout_score"), (int, float))]
        median = statistics.median(scores) if scores else 0.0

        title_list = _format_title_list(shorts)
        synthesis_summary = _summarize_synthesis(snap.synthesis.body if snap.synthesis else None)

        trace = traces.new_trace(TASK_TYPE, inputs={
            "channel_handle": snap.channel_handle,
            "n_shorts": len(shorts),
            "median_breakout": median,
            "input_hash": input_hash,
        })

        # ── generate ──────────────────────────────────────────────────────
        generated: Dict[str, Any] = {}
        gen_skip_reason: Optional[str] = None
        if not llm.gemini_available():
            gen_skip_reason = "GEMINI_API_KEY missing"
        else:
            try:
                client = llm.get_genai()
                import config  # local import keeps module-load cheap
                prompt = _GENERATE_PROMPT.format(
                    handle=snap.channel_handle,
                    n=len(shorts),
                    median=median,
                    synthesis_summary=synthesis_summary,
                    title_list=title_list,
                )
                resp = client.models.generate_content(
                    model=config.GEMINI_MODEL_PRIMARY,
                    contents=prompt,
                )
                generated = llm.parse_json_lenient(resp.text or "")
                traces.add_step(trace, "generate", {
                    "model": config.GEMINI_MODEL_PRIMARY,
                    "patterns_to_use_count":  len(generated.get("patterns_to_use", [])),
                    "patterns_to_avoid_count": len(generated.get("patterns_to_avoid", [])),
                })
            except Exception as e:
                gen_skip_reason = f"gemini error: {e!r}"
                traces.add_step(trace, "generate_failed", {"error": str(e)})

        # ── critique ──────────────────────────────────────────────────────
        critique: Dict[str, Any] = {"critiques": [], "missing_patterns": []}
        crit_skip_reason: Optional[str] = None
        all_claims = (
            [{**p, "side": "use"}   for p in generated.get("patterns_to_use", [])] +
            [{**p, "side": "avoid"} for p in generated.get("patterns_to_avoid", [])]
        )
        if not generated:
            crit_skip_reason = "nothing to critique"
        elif not llm.anthropic_available():
            crit_skip_reason = "ANTHROPIC_API_KEY missing"
        else:
            try:
                client = llm.get_anthropic()
                import config
                user_msg = _CRITIQUE_USER_TEMPLATE.format(
                    handle=snap.channel_handle,
                    n=len(shorts),
                    median=median,
                    claims_json=json.dumps(all_claims, indent=2, ensure_ascii=False),
                    title_list=title_list,
                )
                resp = client.messages.create(
                    model=config.ANTHROPIC_MODEL_CRITIC,
                    max_tokens=4096,
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
                    "critique_count": len(critique.get("critiques", [])),
                    "usage": {
                        "input_tokens":  resp.usage.input_tokens,
                        "output_tokens": resp.usage.output_tokens,
                    },
                })
            except Exception as e:
                crit_skip_reason = f"claude error: {e!r}"
                traces.add_step(trace, "critique_failed", {"error": str(e)})

        # ── select / merge ────────────────────────────────────────────────
        merged = _merge_with_critique(generated, critique)
        traces.add_step(trace, "select", {
            "kept_use":   len(merged["patterns_to_use"]),
            "kept_avoid": len(merged["patterns_to_avoid"]),
        })

        payload = {
            "channel_handle": snap.channel_handle,
            "n_shorts": len(shorts),
            "median_breakout": median,
            "patterns_to_use":   merged["patterns_to_use"],
            "patterns_to_avoid": merged["patterns_to_avoid"],
            "missing_patterns_flagged_by_critic": merged.get("missing_patterns_flagged_by_critic", []),
            "open_questions": generated.get("open_questions", []),
            "skips": {
                "generation_skipped_reason": gen_skip_reason,
                "critique_skipped_reason":   crit_skip_reason,
            },
            "models": {
                "generator": "gemini" if not gen_skip_reason else None,
                "critic":    "claude" if not crit_skip_reason else None,
            },
        }

        trace_id = traces.finalize(trace, result={"payload_keys": list(payload.keys())})
        # finalize() returns the path; pull the trace_id off the trace object itself.
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
        depends_on=[],
        run=_run,
        description=f"Title-pattern retrospective over {snap.channel_handle}'s corpus",
    )]
