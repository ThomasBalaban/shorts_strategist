import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

PORT = 9022

ANALYZER_URL = os.environ.get("SHORTS_ANALYZER_URL", "http://localhost:9021")
SUBTITLER_URL = os.environ.get("SIMPLEAUTOSUBS_URL", "http://localhost:9020")

DATA_DIR = os.path.join(HERE, "data")
TRACES_DIR = os.path.join(HERE, "traces")
OUTPUT_DIR = os.path.join(HERE, "output")

STRATEGY_DB = os.path.join(DATA_DIR, "strategy.db")

GEMINI_MODEL_PRIMARY = os.environ.get("STRATEGIST_GEMINI_MODEL", "gemini-3-pro-preview")
ANTHROPIC_MODEL_CRITIC = os.environ.get("STRATEGIST_CLAUDE_MODEL", "claude-opus-4-7")

for d in (DATA_DIR, TRACES_DIR, OUTPUT_DIR):
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
