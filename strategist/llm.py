"""LLM clients: Gemini 3 Pro for generation, Claude Opus 4.7 for critique.

Both clients are lazy and graceful — missing API keys turn into a "skipped"
record in the trace rather than a crash. This matches the gameplan's hard
rule: "If only one is available, the endpoint should still work but record
in the trace that critique was skipped."
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

import config


_anthropic_client: Optional[Any] = None
_genai_client: Optional[Any] = None


def anthropic_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def gemini_available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def get_genai():
    global _genai_client
    if _genai_client is None:
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        _genai_client = genai.Client(api_key=api_key)
    return _genai_client


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)```$", t, flags=re.DOTALL)
    if m:
        return m.group(1).strip()
    return t


# Models occasionally emit typographic quotes inside JSON strings (e.g.
# `"reasoning": "...overreach.”},{...next object..."`) — visually correct,
# but it confuses the model itself into emitting structurally broken JSON
# afterwards. We normalize these to ASCII before parsing so the model's
# intent ("close the string and start the next object") still parses.
_SMART_QUOTE_MAP = str.maketrans({
    "“": '"',  # left double curly
    "”": '"',  # right double curly
    "‘": "'",  # left single curly
    "’": "'",  # right single curly
    "„": '"',  # double low-9
    "‚": "'",  # single low-9
})


def parse_json_lenient(text: str) -> Any:
    """Parse JSON from a model response, stripping code fences and
    normalizing typographic quotes that LLMs sometimes emit mid-string."""
    cleaned = _strip_code_fence(text).translate(_SMART_QUOTE_MAP)
    return json.loads(cleaned)


# ── Gemini: generate titles ───────────────────────────────────────────────

GENERATE_PROMPT = """\
You are generating titles for a short-form YouTube gaming clip on the channel @{channel_handle}.

CHANNEL CONTEXT — what works on this channel (from corpus synthesis):
{channel_context}

CLIP TRANSCRIPT (timestamps in seconds):
{transcript}

TRIM SUMMARY:
{trim_summary}

Generate exactly 5 candidate titles. Each must be:
- 60 characters or fewer
- Punchy and clickable
- Aligned with the load-bearing patterns from the channel context
- Accurate to the clip content (no clickbait that misrepresents)

Return ONLY a JSON array (no prose, no code fences). Each element:
{{"title": "<the title>", "rationale": "<one sentence on why this should work>"}}
"""


def gemini_generate_titles(
    channel_handle: str,
    channel_context: str,
    transcript_excerpt: str,
    trim_summary: str,
) -> Dict[str, Any]:
    """Run Gemini 3 Pro to produce candidate titles. Returns:
    {"candidates": [...], "raw_text": "...", "model": "..."}
    Raises on failure — caller writes the failure into the trace.
    """
    client = get_genai()
    prompt = GENERATE_PROMPT.format(
        channel_handle=channel_handle,
        channel_context=channel_context or "(no synthesis available)",
        transcript=transcript_excerpt,
        trim_summary=trim_summary or "(none)",
    )
    response = client.models.generate_content(
        model=config.GEMINI_MODEL_PRIMARY,
        contents=prompt,
    )
    text = response.text or ""
    candidates = parse_json_lenient(text)
    if not isinstance(candidates, list):
        raise ValueError(f"Gemini did not return a JSON array, got: {type(candidates).__name__}")
    return {
        "candidates": candidates,
        "raw_text": text,
        "model": config.GEMINI_MODEL_PRIMARY,
    }


# ── Claude: critique candidates ────────────────────────────────────────────

CRITIQUE_SYSTEM = """\
You are critiquing candidate YouTube Shorts titles. You are explicitly a different model from the one that generated them — your job is to find weaknesses they may have rationalized away.

For each candidate, evaluate:
- alignment with the channel patterns (high / medium / low)
- clickability (1-10)
- accuracy to the clip content (1-10)
- specific weaknesses (one to two sentences)
- a composite score (0-100) — your overall verdict

Be honest. If a candidate is bad, score it low. If two are equally good, score them equally."""


CRITIQUE_USER_TEMPLATE = """\
CHANNEL CONTEXT:
{channel_context}

CLIP TRANSCRIPT:
{transcript}

CANDIDATES:
{candidates_json}
"""


CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "scored": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "alignment": {"type": "string", "enum": ["high", "medium", "low"]},
                    "clickability": {"type": "integer"},
                    "accuracy": {"type": "integer"},
                    "weaknesses": {"type": "string"},
                    "score": {"type": "integer"},
                },
                "required": ["title", "alignment", "clickability", "accuracy", "weaknesses", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scored"],
    "additionalProperties": False,
}


def claude_critique_titles(
    channel_context: str,
    transcript_excerpt: str,
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Run Claude Opus 4.7 to critique and score candidates. Returns:
    {"scored": [...], "raw_text": "...", "model": "...", "usage": {...}}
    """
    client = get_anthropic()
    user_msg = CRITIQUE_USER_TEMPLATE.format(
        channel_context=channel_context or "(no synthesis available)",
        transcript=transcript_excerpt,
        candidates_json=json.dumps(candidates, indent=2),
    )
    response = client.messages.create(
        model=config.ANTHROPIC_MODEL_CRITIC,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": CRITIQUE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        output_config={
            "format": {"type": "json_schema", "schema": CRITIQUE_SCHEMA}
        },
        messages=[{"role": "user", "content": user_msg}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    parsed = parse_json_lenient(text)
    return {
        "scored": parsed.get("scored", []),
        "raw_text": text,
        "model": config.ANTHROPIC_MODEL_CRITIC,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
        },
    }
