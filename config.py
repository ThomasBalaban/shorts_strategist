import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

PORT = 9022

ANALYZER_URL = os.environ.get("SHORTS_ANALYZER_URL", "http://localhost:9021")
SUBTITLER_URL = os.environ.get("SHORTS_AUTO_EDITOR_URL", "http://localhost:9020")

DATA_DIR = os.path.join(HERE, "data")
TRACES_DIR = os.path.join(HERE, "traces")
OUTPUT_DIR = os.path.join(HERE, "output")
RECOMMENDATIONS_DIR = os.path.join(OUTPUT_DIR, "recommendations")

STRATEGY_DB = os.path.join(DATA_DIR, "strategy.db")
THINKER_STATE_FILE = os.path.join(DATA_DIR, "thinker_state.json")
CAPABILITY_MANIFEST_PATH = os.path.join(DATA_DIR, "capability_manifest.json")

# Sibling project paths. Default layout assumes shorts_strategist,
# shorts_analyzer, and shorts-auto-editor are sibling directories of one parent.
_SIBLING_ROOT = os.path.dirname(HERE)
ANALYZER_OUTPUT_DIR = os.environ.get(
    "SHORTS_ANALYZER_OUTPUT_DIR",
    os.path.join(_SIBLING_ROOT, "shorts_analyzer", "output"),
)
SUBTITLER_SHORTS_DATA_DIR = os.environ.get(
    "SHORTS_AUTO_EDITOR_SHORTS_DATA_DIR",
    os.path.join(_SIBLING_ROOT, "shorts-auto-editor", "shorts_data"),
)

CHANNEL_HANDLE = os.environ.get("STRATEGIST_CHANNEL_HANDLE", "PeepingOtter")

# gemini-3-pro-preview was deprecated by Google (404 NOT_FOUND), which
# silently broke every strategist generation. Matches the editor's working
# model id (shorts-auto-editor/utils/models.py MODEL_PRO).
GEMINI_MODEL_PRIMARY = os.environ.get("STRATEGIST_GEMINI_MODEL", "gemini-3.1-pro-preview")
ANTHROPIC_MODEL_CRITIC = os.environ.get("STRATEGIST_CLAUDE_MODEL", "claude-opus-4-8")

for d in (DATA_DIR, TRACES_DIR, OUTPUT_DIR, RECOMMENDATIONS_DIR):
    os.makedirs(d, exist_ok=True)


# ── API keys ──────────────────────────────────────────────────────────────
# Keys come from the centralized youtube_hub/config/secrets.json. Existing
# environment variables still win — the launcher or operator can override
# per-process without editing the shared file.
import sys as _sys

_HUB_CONFIG = os.path.abspath(os.path.join(HERE, "..", "youtube_hub", "config"))
if _HUB_CONFIG not in _sys.path:
    _sys.path.insert(0, _HUB_CONFIG)

try:
    from shared_secrets import export_to_env as _export_to_env
    _export_to_env()
except Exception as _e:
    print(
        f"⚠️  Could not load shared secrets from {_HUB_CONFIG}: {_e}",
        flush=True,
    )
