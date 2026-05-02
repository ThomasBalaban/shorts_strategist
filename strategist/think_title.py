"""Phase 1: /think/title — generate-then-critique title reasoning.

Flow:
1. Pull channel synthesis from shorts_analyzer (graceful if unavailable).
2. Generate 5 candidates with Gemini 3 Pro.
3. Critique with Claude Opus 4.7 (different model = independent eyes).
4. Select highest-score candidate.
5. Persist a trace at every step, even on failure.

Hard rules from the gameplan:
- No fallback titles — on failure, return clear error + trace_id; never invent placeholder text.
- If critique unavailable (Anthropic key missing), select Gemini's first candidate and record skip in trace.
- If generation fails, the call fails — there's nothing to fall back to.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from strategist import llm, traces
from strategist.analyzer_client import AnalyzerClient


def _fetch_channel_context(channel_handle: str) -> Dict[str, Any]:
    """Pull both corpus and synthesis from the analyzer. Returns a structured
    bundle with concrete examples (top/bottom/recent shorts) plus the synthesis
    narrative and tag-lift data. Empty fields when the analyzer is offline or
    files are missing — caller still gets a usable shape.
    """
    client = AnalyzerClient()
    corpus_name = f"{channel_handle}.json"
    syn_name = f"{channel_handle}.synthesis.json"
    corpus = client.read_result(corpus_name) or {}
    syn = client.read_result(syn_name) or {}

    shorts: List[Dict[str, Any]] = corpus.get("shorts") or []
    by_id = {s.get("video_id"): s for s in shorts if s.get("video_id")}

    quint = syn.get("quintiles") or {}
    top_ids = quint.get("top_video_ids") or []
    bottom_ids = quint.get("bottom_video_ids") or []

    # Top 5 breakouts: prefer the synthesis quintile list (analyzer-blessed),
    # fall back to highest breakout_score in the corpus
    def _by_score(items: List[Dict[str, Any]], desc: bool = True) -> List[Dict[str, Any]]:
        return sorted(
            [s for s in items if s.get("breakout_score") is not None],
            key=lambda s: s["breakout_score"],
            reverse=desc,
        )

    if top_ids:
        top = [by_id[i] for i in top_ids if i in by_id][:5]
        top = _by_score(top)[:5]
    else:
        top = _by_score(shorts)[:5]

    if bottom_ids:
        bot = [by_id[i] for i in bottom_ids if i in by_id]
        bot = _by_score(bot, desc=False)[:3]
    else:
        bot = _by_score(shorts, desc=False)[:3]

    # Most-recent 5 titles (any score) — for staleness/repetition awareness
    recent = sorted(
        [s for s in shorts if s.get("published_at") or s.get("published_date")],
        key=lambda s: s.get("published_at") or s.get("published_date") or "",
        reverse=True,
    )[:5]

    # Top tag lifts — these are the "tags only present in breakouts" signal,
    # already sorted desc by lift in the analyzer's output
    lifts = (syn.get("unique_to_breakouts") or [])[:10]

    return {
        "available": bool(corpus or syn),
        "corpus_present": bool(corpus),
        "synthesis_present": bool(syn),
        "narrative": (syn.get("narrative") or {}),
        "top": top,
        "bottom": bot,
        "recent": recent,
        "lifts": lifts,
        "corpus_size": len(shorts),
    }


def _short_summary(s: Dict[str, Any], with_reason: bool = False, max_reason: int = 220) -> str:
    title = (s.get("title") or "").strip()
    score = s.get("breakout_score")
    score_str = f"breakout={score:.2f}" if isinstance(score, (int, float)) else "breakout=?"
    line = f'"{title}" ({score_str})'
    if with_reason:
        ga = s.get("gemini_analysis") or {}
        reason = ((ga.get("title") or {}).get("why_it_worked")) or ga.get("why_the_video_worked") or ""
        reason = reason.strip().replace("\n", " ")
        if reason:
            if len(reason) > max_reason:
                reason = reason[: max_reason - 1].rstrip() + "…"
            line += f"\n   why: {reason}"
    return line


def _format_channel_context(ctx: Dict[str, Any]) -> str:
    if not ctx.get("available"):
        return ""

    parts: List[str] = []
    n = ctx.get("narrative") or {}

    if n.get("top_quintile_signature"):
        parts.append(f"TOP-QUINTILE SIGNATURE:\n{n['top_quintile_signature']}")
    if n.get("load_bearing_patterns"):
        parts.append(f"LOAD-BEARING PATTERNS:\n{n['load_bearing_patterns']}")
    if n.get("conditional_insights"):
        parts.append(f"CONDITIONAL INSIGHTS:\n{n['conditional_insights']}")
    if n.get("cautions"):
        parts.append(f"CAUTIONS:\n{n['cautions']}")

    top = ctx.get("top") or []
    if top:
        lines = [f"{i+1}. {_short_summary(s, with_reason=True)}" for i, s in enumerate(top)]
        parts.append("WHAT WORKED — actual breakout titles on this channel:\n" + "\n".join(lines))

    bot = ctx.get("bottom") or []
    if bot:
        lines = [f"{i+1}. {_short_summary(s, with_reason=False)}" for i, s in enumerate(bot)]
        parts.append("WHAT FLOPPED — actual underperformer titles:\n" + "\n".join(lines))

    recent = ctx.get("recent") or []
    if recent:
        lines = []
        for i, s in enumerate(recent):
            date = s.get("published_date") or (s.get("published_at") or "")[:10]
            score = s.get("breakout_score")
            score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "?"
            lines.append(f'{i+1}. "{(s.get("title") or "").strip()}" ({date}, breakout={score_str})')
        parts.append("RECENT TITLES (avoid repeating these):\n" + "\n".join(lines))

    lifts = ctx.get("lifts") or []
    if lifts:
        lines = []
        for L in lifts:
            axis = L.get("axis", "?")
            tag = L.get("tag", "?")
            lift = L.get("lift")
            top_rate = L.get("top_rate")
            overall_rate = L.get("overall_rate")
            lift_str = f"+{lift:.2f}" if isinstance(lift, (int, float)) else "?"
            tr = f"{top_rate*100:.0f}%" if isinstance(top_rate, (int, float)) else "?"
            o_r = f"{overall_rate*100:.0f}%" if isinstance(overall_rate, (int, float)) else "?"
            lines.append(f"- {axis}:{tag} (lift {lift_str}, top={tr} vs overall={o_r})")
        parts.append("HIGH-LIFT TAGS (mostly present in breakouts):\n" + "\n".join(lines))

    return "\n\n".join(parts)


def _format_transcript(transcript: List[Dict[str, Any]], max_chars: int = 6000) -> str:
    """Render the transcript as compact lines: [t.tt] text. Truncates politely."""
    lines = []
    total = 0
    for seg in transcript or []:
        t = seg.get("start") or seg.get("t") or 0
        text = (seg.get("text") or seg.get("content") or "").strip()
        if not text:
            continue
        line = f"[{float(t):.2f}] {text}"
        if total + len(line) + 1 > max_chars:
            lines.append("… (truncated)")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines) if lines else "(empty transcript)"


def _select(scored: List[Dict[str, Any]], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pick the highest-score candidate. Tie-breaks favor the earlier candidate
    (Gemini's own ordering is its preference)."""
    if not scored:
        # No critique — fall back to Gemini's first candidate
        if not candidates:
            raise ValueError("No candidates and no scores — nothing to select")
        first = candidates[0]
        return {
            "title": first.get("title", ""),
            "reasoning": first.get("rationale", ""),
            "score": None,
            "critique_skipped": True,
        }

    # Index original candidate order so ties prefer earlier (Gemini-ranked) ones
    order = {c.get("title", ""): i for i, c in enumerate(candidates)}
    best = max(scored, key=lambda s: (s.get("score", 0), -order.get(s.get("title", ""), 999)))

    # Compose reasoning from generator rationale + critique
    gen_rationale = next(
        (c.get("rationale", "") for c in candidates if c.get("title") == best["title"]),
        "",
    )
    reasoning_parts = []
    if gen_rationale:
        reasoning_parts.append(f"Generator: {gen_rationale}")
    reasoning_parts.append(
        f"Critic: alignment={best.get('alignment')}, "
        f"click={best.get('clickability')}/10, "
        f"accuracy={best.get('accuracy')}/10. "
        f"{best.get('weaknesses', '')}"
    )
    return {
        "title": best["title"],
        "reasoning": " | ".join(reasoning_parts),
        "score": best.get("score"),
        "critique_skipped": False,
    }


def think_title(
    channel_handle: str,
    transcript: List[Dict[str, Any]],
    trim_summary: Optional[Dict[str, Any]] = None,
    cut_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the title pipeline. Always writes a trace; raises on hard failure
    (after writing the trace) so the caller can return an HTTP error with a
    trace_id to debug from."""
    trace = traces.new_trace(
        kind="think_title",
        inputs={
            "channel_handle": channel_handle,
            "cut_id": cut_id,
            "transcript_len": len(transcript or []),
            "trim_summary_present": trim_summary is not None,
        },
    )

    # Step 0: pull channel context — graceful if analyzer is offline
    ctx = _fetch_channel_context(channel_handle)
    traces.add_step(trace, "channel_context", {
        "available": ctx["available"],
        "corpus_present": ctx.get("corpus_present"),
        "synthesis_present": ctx.get("synthesis_present"),
        "corpus_size": ctx.get("corpus_size"),
        "narrative_keys": list((ctx.get("narrative") or {}).keys()),
        "top_count": len(ctx.get("top") or []),
        "bottom_count": len(ctx.get("bottom") or []),
        "recent_count": len(ctx.get("recent") or []),
        "lift_count": len(ctx.get("lifts") or []),
    })

    transcript_str = _format_transcript(transcript)
    context_str = _format_channel_context(ctx)
    trim_str = json.dumps(trim_summary, indent=2) if trim_summary else ""

    # Step 1: generate (Gemini)
    if not llm.gemini_available():
        traces.add_step(trace, "generate", {"skipped": True, "reason": "no_gemini_key"})
        traces.finalize(trace, error="GEMINI_API_KEY (or GOOGLE_API_KEY) not set; cannot generate")
        raise RuntimeError("GEMINI_API_KEY not set — strategist cannot generate titles")

    try:
        gen = llm.gemini_generate_titles(channel_handle, context_str, transcript_str, trim_str)
    except Exception as e:
        traces.add_step(trace, "generate", {"error": repr(e)})
        traces.finalize(trace, error=f"generate failed: {e}")
        raise

    candidates = gen["candidates"]
    traces.add_step(trace, "generate", {
        "model": gen["model"],
        "candidate_count": len(candidates),
        "candidates": candidates,
    })

    # Step 2: critique (Claude) — gracefully skip if no key
    scored: List[Dict[str, Any]] = []
    critique_skipped_reason: Optional[str] = None
    if not llm.anthropic_available():
        critique_skipped_reason = "no_anthropic_key"
        traces.add_step(trace, "critique", {"skipped": True, "reason": critique_skipped_reason})
    else:
        try:
            crit = llm.claude_critique_titles(context_str, transcript_str, candidates)
            scored = crit["scored"]
            traces.add_step(trace, "critique", {
                "model": crit["model"],
                "scored": scored,
                "usage": crit["usage"],
            })
        except Exception as e:
            critique_skipped_reason = f"critique_error: {e!r}"
            traces.add_step(trace, "critique", {"error": repr(e), "skipped": True})

    # Step 3: select
    chosen = _select(scored, candidates)
    traces.add_step(trace, "select", chosen)

    result = {
        "title": chosen["title"],
        "reasoning": chosen["reasoning"],
        "score": chosen["score"],
        "critique_skipped": chosen["critique_skipped"] or critique_skipped_reason is not None,
        "critique_skipped_reason": critique_skipped_reason,
        "trace_id": trace["trace_id"],
        "candidates": candidates,
        "scored": scored,
    }
    traces.finalize(trace, result=result)
    return result
