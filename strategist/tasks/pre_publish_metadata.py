"""
Per-pre-publish-clip description, hashtags, and tags.

For every ``shorts-auto-editor/shorts_data/shorts_metadata_*.json`` on disk, emit
the YouTube description, three hashtags, and a comma-separated tag list to ship
with the clip. Title is NOT produced here — ``pre_publish_title`` owns that.

The recommended title (if available) is read out of the sibling
``titles/<base>.json`` artifact at run time so the description matches the
title's voice. If that artifact is missing, the task falls back to
shorts-auto-editor's ``title`` field from the source metadata.

Hashtags / tags are grounded in the same channel-pattern artifacts the title
recommender uses, so vocabulary stays coherent across all metadata fields.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import config

from .. import inputs as inputs_mod
from .. import llm
from .. import recommendations
from .. import traces
from . import pre_publish_title, title_pattern_retro
from .base import Task

TASK_TYPE = "pre_publish_metadata"
CATEGORY = "metadata"

# Bump when the prompts change. Folded into input_hash so stale artifacts
# re-run automatically without an explicit /thinker/force call.
PROMPT_VERSION = "v3"


# ─── prompts ──────────────────────────────────────────────────────────────────

_GENERATE_PROMPT = """\
You are writing the YouTube description, hashtags, and tags for a Shorts clip
on @{handle}, immediately before publish. The title is already locked in:

TITLE: "{title}"

CHANNEL CONTEXT (synthesis of what works on this channel):
{synthesis_summary}

CHANNEL TITLE PATTERNS (signal for tone, vocabulary, audience):
patterns_to_use:
{patterns_to_use}
patterns_to_avoid:
{patterns_to_avoid}

CLIP INTERPRETATION (from shorts-auto-editor):
{clip_interpretation}

ALREADY-USED DESCRIPTIONS on this channel (do NOT reuse these phrasings
or close paraphrases — the channel needs fresh hooks each time):
{prior_descriptions}

HARD CONSTRAINT — no proper names, ever:
The description must NEVER contain a proper noun — no game names, no
character names, no streamer-friend names, no place names. Even if a
name appears in CLIP INTERPRETATION (e.g. in named_entities), do not put
it in the description. The viewer doesn't know who anyone is yet and a
name in the first line is a wall, not a hook.

HARD CONSTRAINT — no invented facts:
Use only facts present in CLIP INTERPRETATION. Do not infer a game,
genre, or setting that isn't explicitly there. Underspecified beats
confident-but-wrong.

Produce three things:

1. description — ONE short line, 12 words or fewer, that works as a HOOK.
   A hook is forward-leaning curiosity, NOT a recap of what happened.
   Make the viewer want to find out. The line should land BEFORE they've
   seen the clip — not summarise it after.

   Good hook shapes (use these as templates, not literal copies):
     - "i had one job."
     - "this should not have been allowed."
     - "i'm never playing this again."
     - "tell me why this happened."
     - "you won't believe what this thing did."
     - "didn't think it could get worse. it did."

   BAD shapes (do not produce these):
     - Recaps: "so much for a nice relaxing trip"  (looks backward)
     - Title echo: "the game is attacking me"      (duplicates the title)
     - Narration: "watch as...", "in this video..."
     - Setup pieces with names: "X promised me..."

   Lower-case is fine. One emoji at the end is allowed if it genuinely
   fits. NO hashtags inside the description string (hashtags ship as a
   separate field).

2. hashtags — exactly 3, chosen for diversity (NOT redundancy):
   - one BROAD discovery tag (e.g. #gaming, #funnymoments, #horrorgaming)
   - one SPECIFIC genre/vibe tag (e.g. #jumpscare, #ragemoment, #wholesome)
   - one TRENDING/FORMAT tag (e.g. #shorts, #clip, #fyp)

3. tags — comma-separated, under 250 characters total. Mix of broad and
   specific. NO leading hash on tags. Lower case is fine.

   HARD ACCURACY RULE for tags (same as description):
   Do NOT invent a genre, setting, or game type that isn't explicitly in
   CLIP INTERPRETATION. The clip interpretation rarely identifies the
   game — when it doesn't, your tags must NOT either.

   FORBIDDEN tag patterns when the source doesn't confirm them:
     - "horror game", "survival horror", "horror gaming"
     - "underwater game", "underwater survival", "deep sea"
     - "vehicle survival game", "scary game"
     - "jumpscare" (unless the clip interpretation explicitly says one happened)
     - Any "<setting> game" or "<genre> game" inferred from vibes

   If you don't know the genre, use generic gameplay-reaction tags
   ("gaming clip", "funny gaming moments", "gamer reaction", "streamer
   moment") rather than confidently-wrong specifics. Also avoid filler
   quote-style tags like "hilarious gaming clips" or "funny gamer quotes".

Return ONLY this JSON:
{{
  "description": "<1-2 sentences>",
  "hashtags":    ["#a", "#b", "#c"],
  "tags":        "comma,separated,list,under,250,chars"
}}
"""


_CRITIQUE_SYSTEM = """\
You are critiquing pre-publish metadata for a YouTube Short — description,
hashtags, and tags. You are explicitly a different model from the one that
generated them; find weaknesses they may have rationalized away.

Check:

1. DESCRIPTION shape — ONE short line, 12 words or fewer. It must be a
   forward-leaning HOOK that creates curiosity, NOT a backwards-looking
   recap or a verbatim echo of the title. Dock hard for: recaps ("so
   much for…"), title echoes, narration phrasing ("watch as…"), or any
   setup that reads like a longer description got truncated.
2. NO PROPER NAMES — the description must not contain any proper noun.
   No game names, character names, friend names, or place names. If you
   see one, dock the score severely; this is a hard rule.
3. NO HASHTAGS inside the description string itself.
4. HASHTAG diversity — are the 3 truly distinct (broad / specific / format)
   or near-duplicates? Are any obviously off-topic for the clip?
5. TAG quality — under 250 chars total? Mix of broad and narrow? Any
   filler/garbage tags or tags that invent a genre/setting not in the
   summary? Drop those.

Be direct. Drop tags or hashtags that don't earn their place. Score the
description 0-100 — a hook that actually creates curiosity scores 80+;
a recap/echo/named-entity description scores below 50.
"""


_CRITIQUE_USER_TEMPLATE = """\
TITLE: "{title}"
CHANNEL: @{handle}
CLIP SUMMARY: {clip_summary}

GENERATOR OUTPUT:
{generator_json}

Output JSON:
{{
  "description_score":      <int 0-100>,
  "description_concerns":   "<empty string if none>",
  "hashtag_critiques": [
    {{"hashtag": "#x", "verdict": "keep" | "drop", "reason": "<one sentence>"}}
  ],
  "tags_critique":          "<one sentence on the comma-separated tags>",
  "tags_to_drop":           ["<exact tag string>", ...],
  "verdict":                "approve" | "revise"
}}
"""


_CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "description_score":    {"type": "integer"},
        "description_concerns": {"type": "string"},
        "hashtag_critiques": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "hashtag": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["keep", "drop"]},
                    "reason":  {"type": "string"},
                },
                "required": ["hashtag", "verdict", "reason"],
                "additionalProperties": False,
            },
        },
        "tags_critique":  {"type": "string"},
        "tags_to_drop":   {"type": "array", "items": {"type": "string"}},
        "verdict":        {"type": "string", "enum": ["approve", "revise"]},
    },
    "required": ["description_score", "description_concerns",
                 "hashtag_critiques", "tags_critique", "tags_to_drop", "verdict"],
    "additionalProperties": False,
}


# ─── helpers ──────────────────────────────────────────────────────────────────

def _format_patterns(patterns: List[Dict[str, Any]]) -> str:
    if not patterns:
        return "(none)"
    return "\n".join(
        f"- {p.get('name','?')}: {p.get('description','')}"
        for p in patterns
    )


def _read_title_patterns(channel_handle: str) -> Optional[Dict[str, Any]]:
    art = recommendations.read("channel", f"{channel_handle}.title_patterns")
    return art.get("payload") if art else None


def _read_title_rec(key: str) -> Optional[Dict[str, Any]]:
    art = recommendations.read("titles", key)
    return art.get("payload") if art else None


def _read_prior_descriptions(exclude_key: str) -> List[str]:
    """Collect every existing description on disk so the generator can
    avoid reusing the same template phrasing across similar clips.

    Read at run time (not enumerate time) so each task sees descriptions
    from earlier tasks in the same batch. Intentionally NOT in input_hash:
    we don't want the artifact to look stale every time a sibling task
    completes — the anti-repetition signal is best-effort, not part of
    the deterministic input."""
    descriptions: List[str] = []
    for item in recommendations.list_category(CATEGORY):
        if item.get("key") == exclude_key:
            continue
        art = recommendations.read(CATEGORY, item["key"])
        if not art:
            continue
        desc = ((art.get("payload") or {}).get("description") or "").strip()
        if desc:
            descriptions.append(desc)
    return descriptions


def _format_prior_descriptions(priors: List[str]) -> str:
    if not priors:
        return "(none yet — this is one of the first metadata records on this channel)"
    return "\n".join(f"- {d}" for d in priors)


def _summarize_synthesis(syn: Optional[Dict[str, Any]]) -> str:
    if not syn:
        return "(no synthesis)"
    n = syn.get("narrative")
    if isinstance(n, str):
        return n.strip()[:1000]
    if isinstance(n, dict):
        bits = []
        for k in ("headline", "summary", "what_works"):
            v = n.get(k)
            if isinstance(v, str) and v.strip():
                bits.append(f"{k}: {v.strip()}")
        return ("\n".join(bits))[:1000] or "(narrative empty)"
    return "(narrative empty)"


def _key_for(basename: str) -> str:
    return basename[:-5] if basename.endswith(".json") else basename


def _strip_drops_from_tags(tags: str, drops: List[str]) -> str:
    drops_norm = {d.strip().lower() for d in drops if d}
    parts = [p.strip() for p in tags.split(",") if p.strip()]
    kept = [p for p in parts if p.lower() not in drops_norm]
    return ", ".join(kept)


# ─── enumeration ──────────────────────────────────────────────────────────────

def enumerate_tasks(snap: inputs_mod.Snapshot) -> List[Task]:
    if not snap.pre_publish:
        return []

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
        # Re-run when the title rec changes so description voice keeps pace.
        title_rec_hash = recommendations.existing_input_hash("titles", key) or ""
        synthesis_hash = snap.synthesis.sha256 if snap.synthesis else ""
        input_hash = inputs_mod.hash_inputs({
            "source":     sf.sha256,
            "patterns":   patterns_hash,
            "synthesis":  synthesis_hash,
            "title_rec":  title_rec_hash,
            "prompt_ver": PROMPT_VERSION,
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

    def _run() -> Dict[str, Any]:
        trace = traces.new_trace(TASK_TYPE, inputs={
            "channel_handle": snap.channel_handle,
            "source": sf.basename,
            "input_hash": input_hash,
        })

        # Resolve which title to write metadata for: prefer the strategist's
        # recommendation, fall back to shorts-auto-editor's current title.
        title_rec = _read_title_rec(key) or {}
        recommended = title_rec.get("recommended_title")
        title_used = recommended or current_title
        title_source = (
            "strategist_title_rec" if recommended else "shorts_auto_editor_current_title"
        )
        traces.add_step(trace, "title_resolved", {
            "title": title_used, "source": title_source,
        })

        patterns_to_use   = (patterns_payload or {}).get("patterns_to_use", [])
        patterns_to_avoid = (patterns_payload or {}).get("patterns_to_avoid", [])

        # Read-at-run-time so each task in a batch sees descriptions written
        # by the tasks that ran before it. Not part of input_hash.
        prior_descs = _read_prior_descriptions(exclude_key=key)
        traces.add_step(trace, "prior_descriptions_loaded", {
            "count": len(prior_descs),
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
                    title=title_used,
                    synthesis_summary=_summarize_synthesis(
                        snap.synthesis.body if snap.synthesis else None
                    ),
                    patterns_to_use=_format_patterns(patterns_to_use),
                    patterns_to_avoid=_format_patterns(patterns_to_avoid),
                    clip_interpretation=json.dumps(
                        clip_interpretation, indent=2, ensure_ascii=False
                    )[:2000],
                    prior_descriptions=_format_prior_descriptions(prior_descs),
                )
                resp = client.models.generate_content(
                    model=config.GEMINI_MODEL_PRIMARY,
                    contents=prompt,
                )
                generated = llm.parse_json_lenient(resp.text or "")
                traces.add_step(trace, "generate", {
                    "model": config.GEMINI_MODEL_PRIMARY,
                    "n_hashtags": len(generated.get("hashtags", []) or []),
                    "tags_chars": len(generated.get("tags", "") or ""),
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
                clip_summary = (
                    (clip_interpretation.get("literal_event") or {}).get("description")
                    or clip_interpretation.get("summary")
                    or json.dumps(clip_interpretation)[:300]
                )
                user_msg = _CRITIQUE_USER_TEMPLATE.format(
                    handle=snap.channel_handle,
                    title=title_used,
                    clip_summary=clip_summary,
                    generator_json=json.dumps(generated, indent=2, ensure_ascii=False),
                )
                resp = client.messages.create(
                    model=config.ANTHROPIC_MODEL_CRITIC,
                    max_tokens=1024,
                    system=[{
                        "type": "text",
                        "text": _CRITIQUE_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    output_config={
                        "format": {"type": "json_schema", "schema": _CRITIQUE_SCHEMA}
                    },
                    messages=[{"role": "user", "content": user_msg}],
                )
                text = next((b.text for b in resp.content if b.type == "text"), "")
                critique = llm.parse_json_lenient(text)
                traces.add_step(trace, "critique", {
                    "model": config.ANTHROPIC_MODEL_CRITIC,
                    "verdict": critique.get("verdict"),
                    "description_score": critique.get("description_score"),
                    "usage": {
                        "input_tokens":  resp.usage.input_tokens,
                        "output_tokens": resp.usage.output_tokens,
                    },
                })
            except Exception as e:
                crit_skip = f"claude error: {e!r}"
                traces.add_step(trace, "critique_failed", {"error": str(e)})

        # ── merge: drop critic-killed hashtags + tags ─────────────────────
        gen_hashtags: List[str] = list(generated.get("hashtags", []) or [])
        gen_tags: str = generated.get("tags", "") or ""

        hashtag_drops_norm = {
            (c.get("hashtag") or "").strip().lower()
            for c in (critique.get("hashtag_critiques") or [])
            if c.get("verdict") == "drop"
        }
        kept_hashtags = [
            h for h in gen_hashtags
            if h.strip().lower() not in hashtag_drops_norm
        ]

        kept_tags = _strip_drops_from_tags(
            gen_tags, critique.get("tags_to_drop") or []
        )

        partial_critique = bool(critique) and (
            len(critique.get("hashtag_critiques", []) or []) < len(gen_hashtags)
        )
        if partial_critique:
            traces.add_step(trace, "partial_critique_detected", {
                "n_proposed": len(gen_hashtags),
                "n_critiques_received": len(critique.get("hashtag_critiques") or []),
            })

        payload = {
            "channel_handle":            snap.channel_handle,
            "source":                    sf.basename,
            "title_used":                title_used,
            "title_source":              title_source,
            "description":               generated.get("description"),
            "description_score":         critique.get("description_score"),
            "description_concerns":      critique.get("description_concerns"),
            "hashtags":                  kept_hashtags,
            "hashtags_dropped_by_critic": [
                c for c in (critique.get("hashtag_critiques") or [])
                if c.get("verdict") == "drop"
            ],
            "tags":                      kept_tags,
            "tags_critique":             critique.get("tags_critique"),
            "tags_dropped_by_critic":    critique.get("tags_to_drop") or [],
            "critic_verdict":            critique.get("verdict"),
            "partial_critique":          partial_critique,
            "patterns_grounded_on": {
                "patterns_to_use_count":   len(patterns_to_use),
                "patterns_to_avoid_count": len(patterns_to_avoid),
                "source_artifact": (
                    f"channel/{snap.channel_handle}.title_patterns"
                    if patterns_payload else None
                ),
            },
            "skips": {
                "generation_skipped_reason": gen_skip,
                "critique_skipped_reason":   crit_skip,
            },
        }

        traces.finalize(trace, result={
            "verdict":     critique.get("verdict"),
            "n_hashtags":  len(kept_hashtags),
            "tags_chars":  len(kept_tags),
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
        depends_on=[
            (pre_publish_title.TASK_TYPE, key),
            (title_pattern_retro.TASK_TYPE, f"{snap.channel_handle}.title_patterns"),
        ],
        run=_run,
        description=f"description+hashtags+tags for {sf.basename}",
    )
