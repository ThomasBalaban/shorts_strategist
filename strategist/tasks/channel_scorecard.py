"""
Channel-level scorecard.

The headline product. Combines analyzer outputs (per-short, synthesis,
tailwind, context) plus the strategist's title-pattern findings into one
opinionated read of "what's working / what isn't / what to do next."

Most of the *numbers* are pre-computed in Python so they're trustworthy and
verifiable. The LLM's job is interpretation: distilling the headline,
ranking actions by leverage, and flagging caveats — not arithmetic.

One task per channel.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import inputs as inputs_mod
from .. import llm
from .. import recommendations
from .. import traces
from . import title_pattern_retro
from .base import Task

TASK_TYPE = "channel_scorecard"
CATEGORY = "channel"

RECENT_N = 30


# ─── pre-computation ──────────────────────────────────────────────────────────

def _published_key(s: Dict[str, Any]) -> str:
    return (s.get("published_at") or s.get("published_date") or "")


def _tag_set_for_short(short: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return [(axis, tag), ...] for all tags this short has across axes."""
    tags = ((short.get("gemini_analysis") or {}).get("tags")) or {}
    out: List[Tuple[str, str]] = []
    for axis, val in tags.items():
        if isinstance(val, list):
            for t in val:
                if isinstance(t, str):
                    out.append((axis, t))
        elif isinstance(val, str):
            out.append((axis, val))
    return out


def _data_quality(shorts: Dict[str, Dict[str, Any]],
                  context_body: Optional[Dict[str, Any]],
                  recent: List[Dict[str, Any]],
                  corpus_stats: Dict[str, Any]) -> Dict[str, Any]:
    breakouts = [s.get("breakout_score") for s in shorts.values()
                 if isinstance(s.get("breakout_score"), (int, float))]
    views = [s.get("views") for s in shorts.values()
             if isinstance(s.get("views"), (int, float))]
    recent_breakouts = [s.get("breakout_score") for s in recent
                       if isinstance(s.get("breakout_score"), (int, float))]
    recent_views = [s.get("views") for s in recent
                   if isinstance(s.get("views"), (int, float))]
    n_above_2x = sum(1 for b in recent_breakouts if b >= 2.0)

    dates_recent = [_published_key(s) for s in recent if _published_key(s)]
    date_span = (
        f"{min(dates_recent)[:10]} → {max(dates_recent)[:10]}"
        if dates_recent else "—"
    )

    warnings = []
    if len(shorts) < 100:
        warnings.append(f"corpus is small (n={len(shorts)}) — directional only")
    if recent_breakouts and statistics.median(recent_breakouts) < 1.0:
        warnings.append("recent cohort median breakout < 1.0 (below channel median)")

    return {
        "n_shorts":           len(shorts),
        "median_breakout":    corpus_stats.get("breakout_score", {}).get("median"),
        "median_views":       corpus_stats.get("views", {}).get("median"),
        "view_range":         f"{int(min(views))} → {int(max(views))}" if views else "—",
        "recent_window": {
            "n":               len(recent),
            "date_span":       date_span,
            "median_breakout": statistics.median(recent_breakouts) if recent_breakouts else None,
            "median_views":    statistics.median(recent_views)    if recent_views    else None,
            "n_above_2x":      n_above_2x,
        },
        "warnings": warnings,
    }


def _tag_drift_table(shorts: Dict[str, Dict[str, Any]],
                     recent: List[Dict[str, Any]],
                     synthesis: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For every tag in synthesis.tag_frequencies, compute drift between
    historical top quintile, historical bottom quintile, and the recent cohort.

    A drift signal is what's actionable: a tag that's e.g. 92% in bottom and
    28% in top but 80% in the recent uploads is the user's smoking gun.
    """
    if not synthesis:
        return []
    tag_freq = synthesis.get("tag_frequencies") or {}
    if not isinstance(tag_freq, dict):
        return []

    # Count tag occurrences in the recent cohort.
    n_recent = max(1, len(recent))
    recent_counts: Counter = Counter()
    for s in recent:
        for axis_tag in _tag_set_for_short(s):
            recent_counts[axis_tag] += 1

    rows: List[Dict[str, Any]] = []
    for axis, axis_tags in tag_freq.items():
        if not isinstance(axis_tags, dict):
            continue
        for tag, stats in axis_tags.items():
            if not isinstance(stats, dict):
                continue
            top_rate    = (stats.get("top_quintile")    or {}).get("rate") or 0.0
            bottom_rate = (stats.get("bottom_quintile") or {}).get("rate") or 0.0
            overall     = (stats.get("overall")         or {}).get("rate") or 0.0
            recent_rate = recent_counts.get((axis, tag), 0) / n_recent

            # Verdict: where does the drift point?
            verdict = "stable"
            # Loose threshold — these are signals, not significance tests.
            if top_rate >= 0.20 and bottom_rate <= 0.10 and recent_rate < top_rate * 0.6:
                verdict = "winner_pattern_being_dropped"
            elif bottom_rate >= 0.40 and top_rate <= 0.20 and recent_rate >= bottom_rate * 0.7:
                verdict = "loser_pattern_dominating_recent"
            elif top_rate >= 0.20 and bottom_rate <= 0.10:
                verdict = "winner_pattern"
            elif bottom_rate >= 0.40 and top_rate <= 0.20:
                verdict = "loser_pattern"

            if verdict == "stable":
                continue  # noisy; only surface meaningful drifts

            rows.append({
                "axis": axis,
                "tag":  tag,
                "top_pct":      round(top_rate * 100, 1),
                "bottom_pct":   round(bottom_rate * 100, 1),
                "overall_pct":  round(overall * 100, 1),
                "recent_30_pct": round(recent_rate * 100, 1),
                "avg_breakout_when_present": stats.get("avg_breakout_score_when_present"),
                "verdict": verdict,
            })

    # Sort: drifts that are happening NOW (recent matches the loser side or
    # misses the winner side) come first.
    priority = {
        "loser_pattern_dominating_recent": 0,
        "winner_pattern_being_dropped":    1,
        "loser_pattern":                   2,
        "winner_pattern":                  3,
    }
    rows.sort(key=lambda r: (priority.get(r["verdict"], 9),
                              -abs(r["bottom_pct"] - r["top_pct"])))
    return rows


def _conditional_levers(synthesis: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not synthesis:
        return []
    cps = synthesis.get("conditional_patterns") or []
    if not isinstance(cps, list):
        return []
    out = []
    for cp in cps:
        if not isinstance(cp, dict):
            continue
        a_alone = cp.get("mean_breakout_when_A_not_B")
        a_and_b = cp.get("mean_breakout_when_A_and_B")
        lift = cp.get("lift_B_given_A")
        n_combined = cp.get("n_A_and_B")
        out.append({
            "when": cp.get("when"),
            "with": cp.get("with"),
            "mean_alone":    a_alone,
            "mean_combined": a_and_b,
            "multiplier":    lift,
            "n_combined":    n_combined,
            "n_alone":       cp.get("n_A_not_B"),
        })
    # Highest lift first, but require some sample. Lift can be huge with n=1
    # which is junk — sort by combined lift and break ties by sample size.
    out.sort(key=lambda r: (
        -(r.get("multiplier") or 0),
        -(r.get("n_combined") or 0),
    ))
    return out[:10]


def _monthly_trajectory(context_body: Optional[Dict[str, Any]],
                        shorts: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not context_body:
        return []
    mm = context_body.get("monthly_medians") or {}
    if not isinstance(mm, dict):
        return []
    # Count shorts per month from the corpus so the trajectory shows volume too.
    counts: Counter = Counter()
    for s in shorts.values():
        d = _published_key(s)[:7]  # "YYYY-MM"
        if d:
            counts[d] += 1
    rows = []
    for month, median_views in mm.items():
        rows.append({
            "month":        month,
            "median_views": median_views,
            "n_shorts":     counts.get(month, 0),
        })
    rows.sort(key=lambda r: r["month"])
    return rows


def _tailwind_caveats(tailwind: Optional[Dict[str, Any]],
                       shorts: Dict[str, Dict[str, Any]],
                       synthesis: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Surface top-quintile breakouts that look like timing/algorithm assist
    rather than pure craft, so action recommendations don't over-credit
    repeatable formulas.
    """
    if not tailwind:
        return []
    videos = tailwind.get("videos") or {}
    if not isinstance(videos, dict):
        return []

    top_ids = set()
    if synthesis:
        top_ids = set((synthesis.get("quintiles") or {}).get("top_video_ids") or [])

    out = []
    for vid, v in videos.items():
        if top_ids and vid not in top_ids:
            continue
        if not isinstance(v, dict):
            continue
        residual = (v.get("residual") or {}).get("residual_ratio")
        if not isinstance(residual, (int, float)) or residual < 5.0:
            # Only flag overperformance the residual model can't explain
            # via retention — that's the "algorithm/external pull" zone.
            continue
        tail = v.get("tailwind") or {}
        out.append({
            "video_id":       vid,
            "title":          v.get("title"),
            "breakout_score": v.get("breakout_score"),
            "residual_ratio": residual,
            "summary":        (tail.get("residual_summary") or "")[:600],
        })
    out.sort(key=lambda r: -(r.get("residual_ratio") or 0))
    return out[:8]


def _recent_titles(recent: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{
        "title":    s.get("title"),
        "breakout": s.get("breakout_score"),
        "views":    s.get("views"),
        "date":     _published_key(s)[:10],
    } for s in recent if s.get("title")]


# ─── prompts ──────────────────────────────────────────────────────────────────

_GENERATE_PROMPT = """\
You are producing a channel-level scorecard for @{handle}. The numbers below
are pre-computed from the published-shorts corpus, the channel synthesis,
the tailwind analysis, and the strategist's title-pattern findings. Trust
those numbers; your job is interpretation, not arithmetic.

DATA QUALITY:
{data_quality}

TAG-DRIFT TABLE — tags where historical winners and losers diverge AND the
recent cohort shows movement (top_pct / bottom_pct / recent_30_pct):
{tag_drift}

CONDITIONAL LEVERS — combinations where pattern A on its own underperforms
but A+B together overperforms (or vice versa). Multiplier is the lift of
combined vs alone. Small n_combined means directional only:
{conditional_levers}

MONTHLY TRAJECTORY (median views per month + shorts published that month):
{monthly_trajectory}

TAILWIND CAVEATS — top-quintile videos whose performance the residual
analysis attributes to algorithmic/external pull, not pure craft:
{tailwind_caveats}

LEARNED TITLE PATTERNS (from the strategist's title-pattern retrospective):
patterns_to_use:
{patterns_to_use}
patterns_to_avoid:
{patterns_to_avoid}

RECENT TITLES (most recent {recent_n} shorts, with breakout):
{recent_titles}

CHANNEL SYNTHESIS NARRATIVE (compact):
{synthesis_summary}

Your job: produce a scorecard. Be opinionated, specific, and action-first.

Return ONLY a JSON object:
{{
  "headline": "<ONE OR TWO sentences capturing the most actionable observation. Specific. Not 'consider testing X' — name the lever.>",
  "tag_drift_framing": "<one paragraph framing the drift table — what is the recent cohort drifting toward/away from?>",
  "prioritized_actions": [
    {{
      "action":          "<concrete action — 'stop X', 'force ≤N cuts', 'test misdirect titles for next 10 uploads'>",
      "rationale":       "<one or two sentences — what's the data backing>",
      "evidence":        ["<short bullet>", ...],
      "expected_impact": "<numerical when possible — e.g. '300x lift', 'shifts recent median from 0.5 to 1.5'>",
      "leverage_rank":   <1 = highest leverage>
    }}
  ],
  "experiment_substrates": [
    "<concrete cohort to use as A/B substrate for any of the actions above>"
  ],
  "open_questions": ["<what would you want more data to confirm?>"]
}}

Aim for 3-5 actions ranked by leverage. The single best action should be
unmistakable from the headline.
"""


_CRITIQUE_SYSTEM = """\
You are critiquing channel-level recommendations produced by a different
model from pre-computed tables. Your job is to challenge each proposed
action on:

1. EVIDENCE — does the table actually support the claim, or does it cherry-pick?
2. SAMPLE SIZE — if n is tiny (n_combined ≤ 3, fewer than 5 evidence titles),
   the claim is directional, not gospel. Flag overconfidence.
3. CONFOUNDS — could the cited correlation be explained by something else
   (content, IP, timing, channel-era)? The TAILWIND CAVEATS list is your
   friend here.
4. CONTRADICTION — does the recent cohort actually exhibit the loser
   patterns the action claims to fix? Check the tag-drift table.

Be honest. If an action is well-supported, mark keep. If it's overreaching,
mark weaken with narrowed scope. If it's unsupported, mark drop.
"""


_CRITIQUE_USER_TEMPLATE = """\
The same pre-computed tables the generator saw are below.

{data_payload}

PROPOSED HEADLINE + ACTIONS:
{generator_json}

Output JSON:
{{
  "headline_critique": "<one or two sentences — is the headline correct, overreaching, or wrong?>",
  "action_critiques": [
    {{
      "leverage_rank":   <int>,
      "action":          "<verbatim>",
      "verdict":         "keep" | "weaken" | "drop",
      "narrowed_action": "<only if verdict=weaken; otherwise empty string>",
      "reasoning":       "<one or two sentences>"
    }}
  ],
  "additional_actions_missed": ["<actions the generator missed that the data clearly supports>"]
}}
"""


_CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline_critique": {"type": "string"},
        "action_critiques": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "leverage_rank":    {"type": "integer"},
                    "action":           {"type": "string"},
                    "verdict":          {"type": "string", "enum": ["keep", "weaken", "drop"]},
                    "narrowed_action":  {"type": "string"},
                    "reasoning":        {"type": "string"},
                },
                "required": ["leverage_rank", "action", "verdict", "narrowed_action", "reasoning"],
                "additionalProperties": False,
            },
        },
        "additional_actions_missed": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline_critique", "action_critiques", "additional_actions_missed"],
    "additionalProperties": False,
}


# ─── helpers for prompt formatting ────────────────────────────────────────────

def _summarize_synthesis_narrative(syn: Optional[Dict[str, Any]]) -> str:
    if not syn:
        return "(no synthesis)"
    n = syn.get("narrative")
    if isinstance(n, str):
        return n.strip()[:1500]
    if isinstance(n, dict):
        bits = []
        for k in ("headline", "summary", "what_works", "what_doesnt"):
            v = n.get(k)
            if isinstance(v, str) and v.strip():
                bits.append(f"{k}: {v.strip()}")
        return ("\n".join(bits))[:1500] or "(narrative empty)"
    return "(narrative empty)"


def _read_title_patterns(channel_handle: str) -> Optional[Dict[str, Any]]:
    art = recommendations.read("channel", f"{channel_handle}.title_patterns")
    return art.get("payload") if art else None


# ─── enumeration ──────────────────────────────────────────────────────────────

def enumerate_tasks(snap: inputs_mod.Snapshot) -> List[Task]:
    if not snap.per_short:
        return []

    patterns_payload = _read_title_patterns(snap.channel_handle)
    patterns_hash = ""
    if patterns_payload is not None:
        patterns_hash = inputs_mod.hash_inputs({
            "patterns_to_use":   json.dumps(patterns_payload.get("patterns_to_use", []),   sort_keys=True),
            "patterns_to_avoid": json.dumps(patterns_payload.get("patterns_to_avoid", []), sort_keys=True),
        })

    input_hash = inputs_mod.hash_inputs({
        "per_short": snap.per_short.sha256,
        "synthesis": snap.synthesis.sha256 if snap.synthesis else "",
        "tailwind":  snap.tailwind.sha256  if snap.tailwind  else "",
        "context":   snap.context.sha256   if snap.context   else "",
        "patterns":  patterns_hash,
    })
    key = f"{snap.channel_handle}.scorecard"

    def _run() -> Dict[str, Any]:
        shorts = snap.shorts_by_video_id()
        synthesis = snap.synthesis.body if snap.synthesis else None
        tailwind  = snap.tailwind.body  if snap.tailwind  else None
        context_body = snap.context.body if snap.context else None

        # Recent cohort: top-N by published date desc.
        all_shorts = list(shorts.values())
        all_shorts.sort(key=_published_key, reverse=True)
        recent = all_shorts[:RECENT_N]

        corpus_stats = (synthesis or {}).get("corpus_stats") or {}
        dq      = _data_quality(shorts, context_body, recent, corpus_stats)
        drift   = _tag_drift_table(shorts, recent, synthesis)
        levers  = _conditional_levers(synthesis)
        traj    = _monthly_trajectory(context_body, shorts)
        tail    = _tailwind_caveats(tailwind, shorts, synthesis)
        rtitles = _recent_titles(recent)

        trace = traces.new_trace(TASK_TYPE, inputs={
            "channel_handle": snap.channel_handle,
            "n_shorts": len(shorts),
            "n_recent": len(recent),
            "input_hash": input_hash,
        })
        traces.add_step(trace, "precompute", {
            "drift_rows": len(drift),
            "lever_rows": len(levers),
            "monthly_rows": len(traj),
            "tailwind_caveats": len(tail),
        })

        # ── generate ──────────────────────────────────────────────────────
        generated: Dict[str, Any] = {}
        gen_skip: Optional[str] = None
        patterns_to_use   = (patterns_payload or {}).get("patterns_to_use", [])
        patterns_to_avoid = (patterns_payload or {}).get("patterns_to_avoid", [])

        if not llm.gemini_available():
            gen_skip = "GEMINI_API_KEY missing"
        else:
            try:
                import config
                client = llm.get_genai()
                prompt = _GENERATE_PROMPT.format(
                    handle=snap.channel_handle,
                    data_quality=json.dumps(dq, indent=2, ensure_ascii=False),
                    tag_drift=json.dumps(drift, indent=2, ensure_ascii=False),
                    conditional_levers=json.dumps(levers, indent=2, ensure_ascii=False),
                    monthly_trajectory=json.dumps(traj, indent=2, ensure_ascii=False),
                    tailwind_caveats=json.dumps(tail, indent=2, ensure_ascii=False),
                    patterns_to_use=json.dumps(patterns_to_use, indent=2, ensure_ascii=False)[:2500],
                    patterns_to_avoid=json.dumps(patterns_to_avoid, indent=2, ensure_ascii=False)[:2500],
                    recent_titles=json.dumps(rtitles, indent=2, ensure_ascii=False),
                    recent_n=RECENT_N,
                    synthesis_summary=_summarize_synthesis_narrative(synthesis),
                )
                resp = client.models.generate_content(
                    model=config.GEMINI_MODEL_PRIMARY,
                    contents=prompt,
                )
                generated = llm.parse_json_lenient(resp.text or "")
                traces.add_step(trace, "generate", {
                    "model": config.GEMINI_MODEL_PRIMARY,
                    "n_actions": len(generated.get("prioritized_actions", [])),
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
                # Re-pack the same pre-computed tables for the critic so it
                # judges against the same numbers the generator saw.
                data_payload = json.dumps({
                    "data_quality":       dq,
                    "tag_drift":          drift,
                    "conditional_levers": levers,
                    "monthly_trajectory": traj,
                    "tailwind_caveats":   tail,
                    "patterns_to_use":    patterns_to_use,
                    "patterns_to_avoid":  patterns_to_avoid,
                    "recent_titles":      rtitles,
                }, indent=2, ensure_ascii=False)
                user_msg = _CRITIQUE_USER_TEMPLATE.format(
                    data_payload=data_payload,
                    generator_json=json.dumps(generated, indent=2, ensure_ascii=False),
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
                    "n_critiques": len(critique.get("action_critiques", [])),
                    "usage": {
                        "input_tokens":  resp.usage.input_tokens,
                        "output_tokens": resp.usage.output_tokens,
                    },
                })
            except Exception as e:
                crit_skip = f"claude error: {e!r}"
                traces.add_step(trace, "critique_failed", {"error": str(e)})

        # ── merge ─────────────────────────────────────────────────────────
        # Layer the critic's verdict onto each action; drop "drop"-verdict
        # actions; for "weaken", swap in the narrowed action text.
        crit_by_rank = {c["leverage_rank"]: c for c in critique.get("action_critiques", [])}
        proposed_actions = generated.get("prioritized_actions", [])
        # If the critic returned fewer critiques than the generator proposed
        # (typically caused by malformed JSON in the critic's output), record
        # the gap so the artifact doesn't silently look fully reviewed.
        partial_critique = bool(crit_by_rank) and len(crit_by_rank) < len(proposed_actions)
        if partial_critique:
            traces.add_step(trace, "partial_critique_detected", {
                "n_proposed": len(proposed_actions),
                "n_critiques_received": len(crit_by_rank),
                "missing_ranks": [
                    a.get("leverage_rank") for a in proposed_actions
                    if a.get("leverage_rank") not in crit_by_rank
                ],
            })
        merged_actions = []
        for a in proposed_actions:
            cs = crit_by_rank.get(a.get("leverage_rank"), {})
            verdict = cs.get("verdict", "unreviewed")
            if verdict == "drop":
                continue
            text = a.get("action") or ""
            if verdict == "weaken" and cs.get("narrowed_action"):
                text = cs["narrowed_action"]
            merged_actions.append({
                **a,
                "action": text,
                "critic_verdict":   verdict,
                "critic_reasoning": cs.get("reasoning"),
            })

        payload = {
            "channel_handle": snap.channel_handle,
            "data_quality":   dq,
            "headline":       generated.get("headline"),
            "headline_critique": critique.get("headline_critique"),
            "tag_drift": {
                "framing": generated.get("tag_drift_framing"),
                "table":   drift,
            },
            "conditional_levers":   levers,
            "monthly_trajectory":   traj,
            "tailwind_caveats":     tail,
            "prioritized_actions":  merged_actions,
            "additional_actions_missed_by_critic":
                critique.get("additional_actions_missed", []),
            "partial_critique": partial_critique,
            "experiment_substrates": generated.get("experiment_substrates", []),
            "open_questions":        generated.get("open_questions", []),
            "title_patterns_grounded_on": (
                f"channel/{snap.channel_handle}.title_patterns" if patterns_payload else None
            ),
            "skips": {
                "generation_skipped_reason": gen_skip,
                "critique_skipped_reason":   crit_skip,
            },
        }

        traces.finalize(trace, result={
            "headline":  generated.get("headline"),
            "n_actions": len(merged_actions),
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
        depends_on=[(title_pattern_retro.TASK_TYPE, f"{snap.channel_handle}.title_patterns")],
        run=_run,
        description=f"channel scorecard for {snap.channel_handle}",
    )]
