"""
Pre-publish edit review.

Per pre-publish file in shorts-auto-editor's shorts_data/, score the current cut
against the channel's learned patterns + the capability manifest, and either
recommend specific edit directives (cinematography / onomatopoeia / retrim /
subtitle) or pick a final iteration to ship.

Iteration mechanics:
- shorts-auto-editor owns the iteration counter and enforces the max. The
  metadata's ``file_info.max_iterations`` tells the strategist how many
  attempts are allowed in total (typically 3).
- Each iteration's editorial decisions live in
  ``editorial_decisions`` for the current iteration plus
  ``file_info.iteration_history[*].editorial_decisions`` for earlier ones.
- On every tick the strategist scores the current iteration. If
  ``iteration < max_iterations`` AND a clear gain is identifiable, it
  emits directives (verdict=needs_edits). Otherwise it picks the best
  iteration seen so far (verdict=ship_*).

Graceful fallback: when shorts-auto-editor hasn't yet shipped the
``editorial_decisions`` field, the strategist falls back to a pattern-level
checklist marked ``advisory_only=true`` so the operator can see the task is
waiting on the shorts-auto-editor-side wire-format change.

Ship threshold: score >= 80 OR projected improvement <= 5 points.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import config

from .. import inputs as inputs_mod
from .. import llm
from .. import recommendations
from .. import traces
from . import title_pattern_retro
from .base import Task

TASK_TYPE = "pre_publish_edit_review"
CATEGORY = "edits"

# Score above which we ship the current iteration even if more attempts remain.
SHIP_SCORE_THRESHOLD = 80
# Projected improvement below which we don't bother iterating further.
MIN_IMPROVEMENT_TO_ITERATE = 5

DEFAULT_MAX_ITERATIONS = 3


# ─── manifest ────────────────────────────────────────────────────────────────

def _load_manifest() -> Optional[Tuple[Dict[str, Any], str]]:
    path = config.CAPABILITY_MANIFEST_PATH
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        raw = f.read()
    sha = hashlib.sha256(raw).hexdigest()
    return json.loads(raw.decode("utf-8")), sha


def _summarize_manifest_for_edits(manifest: Dict[str, Any]) -> str:
    """A compact 'what we can change' summary fed to the generator so its
    directives stay inside the capabilities envelope."""
    pc = manifest.get("production_capabilities", {})
    cinema = pc.get("cinematography", {})
    onomat = pc.get("onomatopoeia", {})
    cuts = pc.get("cut_planning", {})
    lines = []
    if cinema:
        actions = ", ".join(a.get("name", "?") for a in cinema.get("zoom_actions", []))
        lines.append(f"cinematography: {actions} (output {cinema.get('output_format', {}).get('resolution', '?')} @{cinema.get('output_format', {}).get('fps', '?')}fps)")
    if onomat:
        anims = [a.get("name") for a in onomat.get("animation_types", [])]
        lines.append(f"onomatopoeia: {len(anims)} animations ({', '.join(anims)})")
        lines.append(f"  density cap: {onomat.get('spacing', {}).get('max_per_minute')} effects/minute, "
                     f"{onomat.get('spacing', {}).get('min_seconds_between')}s minimum spacing")
    if cuts:
        lines.append(f"cuts: hard jump cuts only, target {cuts.get('duration_window', {}).get('target')}, "
                     f"strategy: {cuts.get('strategy', '')[:140]}")
    excluded = manifest.get("deliberately_excluded", {})
    if excluded:
        lines.append("OUT OF SCOPE (do not propose these):")
        for k, v in excluded.items():
            lines.append(f"  - {k}: {', '.join(v)[:200]}")
    return "\n".join(lines)


# ─── prompts ─────────────────────────────────────────────────────────────────

_GENERATE_PROMPT_FULL = """\
You are reviewing a pre-publish YouTube Shorts cut produced by shorts-auto-editor
for the channel @{handle}. Your job is to score it, identify concerns, and
either recommend specific edit directives OR signal that it's good to ship.

ITERATION CONTEXT:
- This is iteration {iteration} of {max_iterations} maximum.
- {history_summary}

CHANNEL PATTERNS (what the analyzer + strategist have learned about this channel):
title_patterns_to_use:
{patterns_use}
title_patterns_to_avoid:
{patterns_avoid}
synthesis_summary:
{synthesis_summary}

CAPABILITY MANIFEST (what shorts-auto-editor CAN actually do — directives must
stay inside this envelope):
{capability_summary}

CLIP CONTEXT (from shorts-auto-editor's pre-publish metadata):
- Source file: {source_file}
- Final duration: {final_duration}s (trimmed from {original_duration}s)
- Current title: "{title}"
- Clip about: {clip_summary}

CURRENT EDITORIAL DECISIONS (iteration {iteration}):
{editorial_decisions_json}

PRIOR ITERATIONS (with strategist scores):
{prior_iterations_json}

Your task:

1. SCORE this iteration 0-100 across:
   - channel_pattern_alignment: does it use winning patterns / avoid losing ones?
   - capability_use: is it leveraging the cinematography/onomatopoeia/cuts well?
   - punch_clarity: does the cut land its punch point cleanly?
   - structural_coherence: does the cut hold together as a single watchable
     beat, or does it stitch unrelated fragments? See STRUCTURAL RUBRIC
     below — be ruthless here, viewers feel jump cuts even when they "serve
     pacing".

   STRUCTURAL RUBRIC (look at trim_segments_kept and judge):
     • **1 segment**: the gold standard. Score 90-100 unless the segment
       itself is bloated.
     • **2 segments, both ≥5s with clearly complementary roles**
       (e.g. setup + payoff with a deliberate tonal pivot): 70-90.
     • **2 segments where one is <5s** (an orphan opener tacked on for
       context): 40-65 — the short opener almost always creates jarring
       whiplash, propose a retrim that drops it.
     • **3+ segments**: 20-50 by default. Only score higher if each segment
       is ≥5s AND serves a distinct narrative beat AND the energy curve
       through them is monotonically rising toward the punch. Multi-segment
       cuts rarely earn this. Watch specifically for:
         - Tonal whiplash (chaotic → calm → chaotic)
         - Length disparity (5s + 5s + 21s — the openers don't earn their
           cost; viewer engagement breaks at each cut)
         - Diluted punch runway (every segment boundary before the punch
           weakens the build)

2. IDENTIFY CONCERNS — concrete things you'd change. If structural_coherence
   is below 70, the FIRST concern must name the specific structural problem
   (which segments to drop or merge, and why).

3. PROPOSE EDIT DIRECTIVES (only if iteration < max_iterations AND you can name
   a SPECIFIC, IMPLEMENTABLE change worth trying). If the current iteration is
   good (>= {ship_threshold}) OR the achievable improvement is tiny
   (<= {min_improvement}), don't propose more edits — recommend ship instead.

   For STRUCTURAL fixes, use the retrim directive: propose a
   `new_segments_to_keep` that consolidates to fewer (ideally one) segments.
   Drop short orphan openers. Prefer a single contiguous range covering
   minimum setup + punch + immediate reaction over multi-segment stitching.

   When you propose a retrim that drops or shifts segments, ALSO inspect
   the existing zoom_timeline and onomatopoeia_events. Any event whose
   timestamp falls outside the new structure (or no longer makes sense in
   it) must be removed via cinematography_changes / onomatopoeia_changes
   in the same directive bundle. Old events from the prior structure don't
   silently follow the retrim — they get applied as-is unless you remove
   them.

Directive types you can propose (each must reference a specific timestamp):
- retrim: change which segments are kept (also the structural fix lever —
  consolidate orphan openers into the dominant segment, or drop them)
- cinematography_changes: add/remove/replace zoom actions at specific times
- onomatopoeia_changes: add/remove/move onomatopoeia events
- subtitle_changes: rare; usually skip

Return ONLY this JSON:
{{
  "score":            <int 0-100>,
  "score_breakdown":  {{
    "channel_pattern_alignment": <int>,
    "capability_use":            <int>,
    "punch_clarity":             <int>,
    "structural_coherence":      <int>
  }},
  "concerns":         ["<one bullet per concern>"],
  "projected_improvement_if_iterated": <int 0-100>,
  "verdict":          "needs_edits" | "ship_current" | "ship_prior",
  "ship_which_iteration": <int|null>,
  "edit_directives": {{
    "retrim": {{
      "should_retrim": <bool>,
      "new_segments_to_keep": [[<start>, <end>], ...] | null,
      "reason": "<one sentence>" | null
    }},
    "cinematography_changes": [
      {{"type": "add"|"remove"|"replace",
        "at_time": <seconds>,
        "from_action": "<existing zoom action or null>",
        "to_action":   "<zoom action from manifest or null>",
        "duration":    <seconds or null>,
        "reason":      "<one sentence>"}}
    ],
    "onomatopoeia_changes": [
      {{"type": "add"|"remove"|"move",
        "at_time": <seconds>,
        "word":      "<from manifest vocabulary>" | null,
        "animation": "<from manifest animation_types>" | null,
        "intensity": <0..1> | null,
        "reason":    "<one sentence>"}}
    ],
    "subtitle_changes": []
  }},
  "review_summary": "<one paragraph: what the reviewer thinks of this iteration overall>"
}}

If verdict is "ship_current" or "ship_prior", omit edit_directives content (it
won't be acted on). If "ship_prior", set ship_which_iteration to the prior
iteration number that scored highest.
"""

_GENERATE_PROMPT_ADVISORY = """\
You are reviewing a pre-publish YouTube Shorts clip for @{handle}, but the
shorts-auto-editor metadata does NOT include editorial_decisions yet — meaning
the shorts-auto-editor-side wire-format change to expose trim segments / zoom
timeline / onomatopoeia events hasn't shipped. So you cannot score the cut
itself; you can only do a pattern-level checklist based on what the clip is
about.

CLIP CONTEXT:
- Title: "{title}"
- Final duration: {final_duration}s (trimmed from {original_duration}s)
- Clip about: {clip_summary}

CHANNEL PATTERNS:
{patterns_use}

Return ONLY this JSON:
{{
  "advisory_summary": "<one paragraph — what to verify when shipping this clip>",
  "checklist": [
    {{"check": "<short label>", "rationale": "<one sentence>"}}
  ]
}}
Aim for 3-6 checklist items.
"""


_CRITIQUE_SYSTEM = """\
You are critiquing a pre-publish cut review produced by a different model.
Your job is to challenge:

1. SCORE accuracy — is the score consistent with the concerns listed? A clip
   with 4+ serious concerns shouldn't be scoring 75.
2. DIRECTIVE concreteness — does each directive name a real timestamp and a
   specific, implementable change? Vague directives like "improve pacing"
   are worthless to a programmatic editor.
3. CAPABILITY adherence — directives must stay inside the manifest. No
   proposing freeze_frame, slow_motion, color grading, etc.
4. PROJECTED improvement realism — if the score is already 78 and projected
   improvement is 25 points, that's overconfident. Push back.
5. SHIP timing — has the iteration count been respected?
6. STRUCTURAL coherence reality-check — pull `editorial_decisions.trim_segments_kept`
   from the generator output and judge it independently. If the cut has 3+
   segments OR any segment under 5s, the generator's `structural_coherence`
   score should reflect that (≤65 typically). If it doesn't and there's no
   retrim directive, the generator is asleep at the wheel — push back hard
   in your critique and recommend a retrim. Stitched openers (short setup
   segment before a much longer dominant segment) almost never serve the
   viewer; they read as "I tried two openings, kept both."

Be direct. If a directive is unactionable, drop it. If a structural problem
was missed, say so explicitly in `score_critique`.
"""


_CRITIQUE_USER_TEMPLATE = """\
ITERATION: {iteration} of {max_iterations}
CAPABILITY MANIFEST (compact):
{capability_summary}
GENERATOR OUTPUT:
{generator_json}

Output JSON:
{{
  "score_critique":      "<is the score consistent with the concerns?>",
  "directive_critiques": [
    {{"directive_path": "<e.g. cinematography_changes[0]>",
      "verdict":        "keep" | "weaken" | "drop",
      "reason":         "<one or two sentences>"}}
  ],
  "verdict_critique":    "<is the ship/iterate decision correct given the score and remaining iterations?>",
  "final_recommended_verdict":          "needs_edits" | "ship_current" | "ship_prior",
  "final_ship_which_iteration":         <int|null>
}}
"""


_CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "score_critique":      {"type": "string"},
        "directive_critiques": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "directive_path": {"type": "string"},
                    "verdict":        {"type": "string", "enum": ["keep", "weaken", "drop"]},
                    "reason":         {"type": "string"},
                },
                "required": ["directive_path", "verdict", "reason"],
                "additionalProperties": False,
            },
        },
        "verdict_critique":            {"type": "string"},
        "final_recommended_verdict":   {"type": "string", "enum": ["needs_edits", "ship_current", "ship_prior"]},
        "final_ship_which_iteration":  {"type": ["integer", "null"]},
    },
    "required": ["score_critique", "directive_critiques", "verdict_critique",
                 "final_recommended_verdict", "final_ship_which_iteration"],
    "additionalProperties": False,
}


# ─── helpers ─────────────────────────────────────────────────────────────────

def _summarize_history(history: List[Dict[str, Any]],
                       prior_iterations_seen: List[Dict[str, Any]]) -> str:
    if not history and not prior_iterations_seen:
        return "No prior iterations."
    lines = [f"{len(history)} prior cut(s) on disk; strategist has scored "
             f"{len(prior_iterations_seen)} of them."]
    by_iter = {p["iteration"]: p for p in prior_iterations_seen}
    for h in history:
        it = h.get("iteration")
        scored = by_iter.get(it)
        if scored:
            lines.append(f"  iteration {it}: score={scored.get('score')} "
                         f"verdict={scored.get('verdict')} ({scored.get('summary', '')[:120]})")
        else:
            lines.append(f"  iteration {it}: not yet scored ({h.get('trigger', 'unknown')})")
    return "\n".join(lines)


def _read_title_patterns(channel_handle: str) -> Optional[Dict[str, Any]]:
    art = recommendations.read("channel", f"{channel_handle}.title_patterns")
    return art.get("payload") if art else None


def _summarize_synthesis(syn: Optional[Dict[str, Any]]) -> str:
    if not syn:
        return "(no synthesis)"
    n = syn.get("narrative")
    if isinstance(n, str):
        return n.strip()[:1000]
    if isinstance(n, dict):
        bits = []
        for k in ("headline", "summary", "what_works", "what_doesnt"):
            v = n.get(k)
            if isinstance(v, str) and v.strip():
                bits.append(f"{k}: {v.strip()}")
        return ("\n".join(bits))[:1000] or "(narrative empty)"
    return "(narrative empty)"


def _key_for(basename: str) -> str:
    return basename[:-5] if basename.endswith(".json") else basename


def _existing_iterations_seen(category: str, key: str) -> List[Dict[str, Any]]:
    art = recommendations.read(category, key)
    if not art:
        return []
    payload = art.get("payload") or {}
    seen = payload.get("iterations_seen") or []
    return seen if isinstance(seen, list) else []


def _pick_best_iteration(iterations_seen: List[Dict[str, Any]]) -> Optional[int]:
    scored = [it for it in iterations_seen if isinstance(it.get("score"), (int, float))]
    if not scored:
        return None
    best = max(scored, key=lambda it: it["score"])
    return best.get("iteration")


# ─── enumeration ─────────────────────────────────────────────────────────────

def enumerate_tasks(snap: inputs_mod.Snapshot) -> List[Task]:
    if not snap.pre_publish:
        return []

    loaded = _load_manifest()
    if loaded is None:
        return []
    manifest, manifest_sha = loaded

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
            "source":    sf.sha256,
            "patterns":  patterns_hash,
            "manifest":  manifest_sha,
            "synthesis": snap.synthesis.sha256 if snap.synthesis else "",
        })
        tasks.append(_build_task(snap, sf, record, key, input_hash, manifest, patterns_payload))
    return tasks


def _build_task(
    snap: inputs_mod.Snapshot,
    sf: Any,
    record: Dict[str, Any],
    key: str,
    input_hash: str,
    manifest: Dict[str, Any],
    patterns_payload: Optional[Dict[str, Any]],
) -> Task:
    file_info = record.get("file_info", {}) or {}
    title = (record.get("title") or "").strip()
    title_analysis = record.get("title_analysis", {}) or {}
    clip_interpretation = title_analysis.get("clip_interpretation") or {}
    editorial = record.get("editorial_decisions")
    iteration = file_info.get("iteration", 1)
    max_iterations = file_info.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    iteration_history = file_info.get("iteration_history", []) or []

    has_editorial = isinstance(editorial, dict) and bool(editorial)

    def _run() -> Dict[str, Any]:
        trace = traces.new_trace(TASK_TYPE, inputs={
            "channel_handle": snap.channel_handle,
            "source": sf.basename,
            "iteration": iteration,
            "max_iterations": max_iterations,
            "has_editorial_decisions": has_editorial,
            "input_hash": input_hash,
        })

        # ── Advisory path: editorial_decisions missing ────────────────────
        if not has_editorial:
            traces.add_step(trace, "advisory_path", {
                "reason": "editorial_decisions absent — shorts-auto-editor has not "
                          "yet shipped the wire-format change."
            })
            payload = _run_advisory(snap, sf, file_info, title, clip_interpretation,
                                    patterns_payload, manifest, trace)
            payload["advisory_only"] = True
            payload["input_hash"] = input_hash
            traces.finalize(trace, result={"advisory": True})
            recommendations.write(
                CATEGORY, key,
                task_type=TASK_TYPE, input_hash=input_hash,
                payload=payload, trace_id=trace["trace_id"],
            )
            return payload

        # ── Full review path ──────────────────────────────────────────────
        prior_seen = _existing_iterations_seen(CATEGORY, key)
        history_summary = _summarize_history(iteration_history, prior_seen)
        patterns_use   = (patterns_payload or {}).get("patterns_to_use", [])
        patterns_avoid = (patterns_payload or {}).get("patterns_to_avoid", [])

        clip_summary = (
            clip_interpretation.get("literal_event", {}).get("description")
            or json.dumps(clip_interpretation)[:300]
        )

        # ── generate ──────────────────────────────────────────────────────
        generated: Dict[str, Any] = {}
        gen_skip: Optional[str] = None
        if not llm.gemini_available():
            gen_skip = "GEMINI_API_KEY missing"
        else:
            try:
                client = llm.get_genai()
                prompt = _GENERATE_PROMPT_FULL.format(
                    handle=snap.channel_handle,
                    iteration=iteration,
                    max_iterations=max_iterations,
                    history_summary=history_summary,
                    patterns_use=json.dumps(patterns_use, indent=2)[:2000],
                    patterns_avoid=json.dumps(patterns_avoid, indent=2)[:2000],
                    synthesis_summary=_summarize_synthesis(snap.synthesis.body if snap.synthesis else None),
                    capability_summary=_summarize_manifest_for_edits(manifest),
                    source_file=file_info.get("original_filename", sf.basename),
                    final_duration=file_info.get("final_duration", "?"),
                    original_duration=file_info.get("original_duration", "?"),
                    title=title,
                    clip_summary=clip_summary,
                    editorial_decisions_json=json.dumps(editorial, indent=2)[:3500],
                    prior_iterations_json=json.dumps(prior_seen, indent=2)[:2500],
                    ship_threshold=SHIP_SCORE_THRESHOLD,
                    min_improvement=MIN_IMPROVEMENT_TO_ITERATE,
                )
                resp = client.models.generate_content(
                    model=config.GEMINI_MODEL_PRIMARY,
                    contents=prompt,
                )
                generated = llm.parse_json_lenient(resp.text or "")
                traces.add_step(trace, "generate", {
                    "model": config.GEMINI_MODEL_PRIMARY,
                    "score": generated.get("score"),
                    "verdict": generated.get("verdict"),
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
                    iteration=iteration,
                    max_iterations=max_iterations,
                    capability_summary=_summarize_manifest_for_edits(manifest),
                    generator_json=json.dumps(generated, indent=2)[:6000],
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
                    "final_verdict": critique.get("final_recommended_verdict"),
                    "usage": {"input_tokens": resp.usage.input_tokens,
                              "output_tokens": resp.usage.output_tokens},
                })
            except Exception as e:
                crit_skip = f"claude error: {e!r}"
                traces.add_step(trace, "critique_failed", {"error": str(e)})

        # ── merge directives (drop critic-killed ones) ────────────────────
        directives = (generated.get("edit_directives") or {}).copy()
        kept_paths: List[str] = []
        dropped: List[Dict[str, Any]] = []
        if critique:
            for c in critique.get("directive_critiques", []):
                path = c.get("directive_path", "")
                verdict = c.get("verdict")
                if verdict == "drop":
                    _remove_directive_at(directives, path)
                    dropped.append({"directive_path": path, "reason": c.get("reason")})
                else:
                    kept_paths.append(path)

        # ── final verdict logic ───────────────────────────────────────────
        score = generated.get("score") or 0
        proj_improvement = generated.get("projected_improvement_if_iterated") or 0
        final_verdict = critique.get("final_recommended_verdict") or generated.get("verdict")
        final_ship_iter = critique.get("final_ship_which_iteration") or generated.get("ship_which_iteration")

        # Structural override: if the cut is structurally weak we must not
        # auto-ship just because other axes are strong. The point of the
        # structural_coherence axis is to stop the strategist from rubber-
        # stamping stitched-reaction openers that read as fine on paper but
        # feel jarring to a viewer. Iteration cap still wins (line below).
        structural_score = (
            (generated.get("score_breakdown") or {}).get("structural_coherence")
        )
        STRUCTURAL_FLOOR = 60
        if (
            isinstance(structural_score, (int, float))
            and structural_score < STRUCTURAL_FLOOR
            and iteration < max_iterations
            and final_verdict in ("ship_current", "ship_prior")
        ):
            final_verdict = "needs_edits"
            traces.add_step(trace, "structural_floor_forced_iterate", {
                "structural_coherence": structural_score,
                "floor":                STRUCTURAL_FLOOR,
            })

        # Apply hard rules even if the LLM disagrees:
        if iteration >= max_iterations and final_verdict == "needs_edits":
            # No more iterations allowed — pick best.
            all_seen = prior_seen + [{
                "iteration": iteration, "score": score,
                "verdict": "forced_ship_at_max", "summary": generated.get("review_summary", ""),
            }]
            best = _pick_best_iteration(all_seen)
            if best == iteration:
                final_verdict = "ship_current"
            else:
                final_verdict = "ship_prior"
                final_ship_iter = best
            traces.add_step(trace, "max_iterations_reached", {
                "best_iteration": best, "forced_verdict": final_verdict,
            })

        if final_verdict == "needs_edits" and proj_improvement <= MIN_IMPROVEMENT_TO_ITERATE:
            # Improvement isn't worth another iteration.
            final_verdict = "ship_current"
            final_ship_iter = iteration
            traces.add_step(trace, "small_improvement_collapsed_to_ship", {
                "projected_improvement": proj_improvement,
                "min_threshold":         MIN_IMPROVEMENT_TO_ITERATE,
            })

        if final_verdict == "needs_edits" and score >= SHIP_SCORE_THRESHOLD:
            final_verdict = "ship_current"
            final_ship_iter = iteration
            traces.add_step(trace, "ship_threshold_collapsed_to_ship", {"score": score})

        # ── update iterations_seen accumulator ────────────────────────────
        new_seen_entry = {
            "iteration": iteration,
            "score":     score,
            "verdict":   final_verdict,
            "summary":   (generated.get("review_summary") or "")[:400],
        }
        # Replace any prior entry for this iteration (re-run safety).
        merged_seen = [it for it in prior_seen if it.get("iteration") != iteration]
        merged_seen.append(new_seen_entry)
        merged_seen.sort(key=lambda it: it.get("iteration", 0))

        # ── payload ───────────────────────────────────────────────────────
        ship_iter_value: Optional[int] = None
        if final_verdict == "ship_current":
            ship_iter_value = iteration
        elif final_verdict == "ship_prior":
            ship_iter_value = final_ship_iter or _pick_best_iteration(merged_seen)

        payload = {
            "channel_handle":          snap.channel_handle,
            "source_metadata":         sf.basename,
            "iteration_reviewed":      iteration,
            "max_iterations_known":    max_iterations,
            "verdict":                 final_verdict or "needs_edits",
            "ship_which_iteration":    ship_iter_value,
            "current_score":           score,
            "score_breakdown":         generated.get("score_breakdown"),
            "concerns":                generated.get("concerns", []),
            "projected_improvement":   proj_improvement,
            "edit_directives":         directives if final_verdict == "needs_edits" else None,
            "directives_dropped_by_critic": dropped,
            "iterations_seen":         merged_seen,
            "review_summary":          generated.get("review_summary"),
            "score_critique":          critique.get("score_critique"),
            "verdict_critique":        critique.get("verdict_critique"),
            "advisory_only":           False,
            "input_hash":              input_hash,
            "skips": {
                "generation_skipped_reason": gen_skip,
                "critique_skipped_reason":   crit_skip,
            },
            "patterns_grounded_on": (
                f"channel/{snap.channel_handle}.title_patterns"
                if patterns_payload else None
            ),
        }

        traces.finalize(trace, result={
            "verdict": final_verdict,
            "score":   score,
            "ship":    ship_iter_value,
        })
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
        description=f"edit review for {sf.basename} (iter {iteration}/{max_iterations})",
    )


# ─── advisory path helper ────────────────────────────────────────────────────

def _run_advisory(snap: inputs_mod.Snapshot, sf: Any, file_info: Dict[str, Any],
                  title: str, clip_interpretation: Dict[str, Any],
                  patterns_payload: Optional[Dict[str, Any]],
                  manifest: Dict[str, Any],
                  trace: Dict[str, Any]) -> Dict[str, Any]:
    """Pattern-level checklist when editorial_decisions isn't shipped yet."""
    advisory = {
        "channel_handle": snap.channel_handle,
        "source_metadata": sf.basename,
        "iteration_reviewed": file_info.get("iteration", 1),
        "max_iterations_known": file_info.get("max_iterations", DEFAULT_MAX_ITERATIONS),
        "verdict": "awaiting_editorial_data",
        "ship_which_iteration": None,
        "advisory_summary": (
            "shorts-auto-editor hasn't yet shipped editorial_decisions in metadata, "
            "so this task can only run a pattern-level checklist. Once the "
            "metadata's editorial_decisions field exists, full scoring + "
            "directives become available automatically."
        ),
        "checklist": [],
    }
    if not llm.gemini_available():
        advisory["skips"] = {"generation_skipped_reason": "GEMINI_API_KEY missing"}
        return advisory

    patterns_use = (patterns_payload or {}).get("patterns_to_use", [])
    clip_summary = (
        (clip_interpretation.get("literal_event") or {}).get("description")
        or json.dumps(clip_interpretation)[:300]
    )
    try:
        client = llm.get_genai()
        prompt = _GENERATE_PROMPT_ADVISORY.format(
            handle=snap.channel_handle,
            title=title,
            final_duration=file_info.get("final_duration", "?"),
            original_duration=file_info.get("original_duration", "?"),
            clip_summary=clip_summary,
            patterns_use=json.dumps(patterns_use, indent=2)[:2000],
        )
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL_PRIMARY,
            contents=prompt,
        )
        body = llm.parse_json_lenient(resp.text or "")
        advisory["advisory_summary"] = body.get("advisory_summary") or advisory["advisory_summary"]
        advisory["checklist"] = body.get("checklist") or []
        traces.add_step(trace, "advisory_generate", {
            "model": config.GEMINI_MODEL_PRIMARY,
            "checklist_len": len(advisory["checklist"]),
        })
    except Exception as e:
        advisory["skips"] = {"generation_skipped_reason": f"gemini error: {e!r}"}
        traces.add_step(trace, "advisory_generate_failed", {"error": str(e)})
    return advisory


# ─── directive-path helper for critique-driven removal ───────────────────────

def _remove_directive_at(directives: Dict[str, Any], path: str) -> None:
    """Best-effort removal of a directive identified by path like
    'cinematography_changes[0]' or 'onomatopoeia_changes[2]'.

    Silently ignores paths that don't match — the critic might invent paths
    that don't exist if it misreads the structure. We don't want one bad path
    to crash the merge.
    """
    if not path:
        return
    field = path.split("[", 1)[0]
    if field not in directives or not isinstance(directives[field], list):
        return
    try:
        idx = int(path.split("[", 1)[1].rstrip("]"))
    except (ValueError, IndexError):
        return
    if 0 <= idx < len(directives[field]):
        directives[field].pop(idx)
