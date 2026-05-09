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

GEMINI_MODEL_PRIMARY = os.environ.get("STRATEGIST_GEMINI_MODEL", "gemini-3-pro-preview")
ANTHROPIC_MODEL_CRITIC = os.environ.get("STRATEGIST_CLAUDE_MODEL", "claude-opus-4-7")

for d in (DATA_DIR, TRACES_DIR, OUTPUT_DIR, RECOMMENDATIONS_DIR):
    os.makedirs(d, exist_ok=True)


# ── API keys ──────────────────────────────────────────────────────────────
# Load from config.json if present. Existing environment variables win — the
# launcher or operator can override per-process without editing the file.
# config.json shape: {"GEMINI_API_KEY": "...", "CLAUDE_API_KEY": "..."}
_CONFIG_JSON = os.path.join(HERE, "config.json")

# Map our config.json field names → the env vars the SDKs actually read.
# Anthropic SDK reads ANTHROPIC_API_KEY; google-genai reads GEMINI_API_KEY
# or GOOGLE_API_KEY.
_KEY_MAP = {
    "GEMINI_API_KEY": "GEMINI_API_KEY",
    "CLAUDE_API_KEY": "ANTHROPIC_API_KEY",
}


def _load_config_json() -> None:
    if not os.path.isfile(_CONFIG_JSON):
        return
    try:
        with open(_CONFIG_JSON) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"⚠️  Could not load {_CONFIG_JSON}: {e}", flush=True)
        return
    for src_key, env_var in _KEY_MAP.items():
        val = data.get(src_key)
        if val and not os.environ.get(env_var):
            os.environ[env_var] = val


_load_config_json()
