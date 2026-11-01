"""Settings for Debrief AI, read from .env first and then from the environment.

Everything is env-driven so tests can point the app at a scratch folder and a mock
analyzer without touching the real data.  Every key is listed in .env.example.
"""
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent      # desktop/backend
APP_DIR = BASE_DIR.parent                       # desktop/ (.env, sessions, frontend)
SHARED_DIR = APP_DIR.parent / "shared"          # code and UI shared with the server edition
if str(SHARED_DIR / "core") not in sys.path:
    sys.path.insert(0, str(SHARED_DIR / "core"))


def _load_env_file():
    """Tiny .env reader (KEY=VALUE, # comments). Real environment variables win."""
    path = APP_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _s(key, default):
    return os.environ.get(key, default)


def _i(key, default):
    try:
        return int(os.environ.get(key, default))
    except ValueError:
        return int(default)


def _b(key, default):
    return str(os.environ.get(key, default)).strip().lower() in ("1", "true", "yes", "on")


# --- models and timing -------------------------------------------------------
LIVE_MODEL = _s("LIVE_MODEL", "sonnet")
FINAL_MODEL = _s("FINAL_MODEL", "opus")
CORRECT_MODEL = _s("CORRECT_MODEL", "sonnet")
EXHIBIT_MODEL = _s("EXHIBIT_MODEL", "sonnet")
CHAT_MODEL = _s("CHAT_MODEL", "sonnet")
FILL_MODEL = _s("FILL_MODEL", "sonnet")
CLI_TIMEOUT = _i("CLI_TIMEOUT", 240)
COPILOT_CLI_PATH = _s("COPILOT_CLI_PATH", "copilot")   # GitHub Copilot, the other model you can pick (npm install -g @github/copilot)
COPILOT_MODEL = _s("COPILOT_MODEL", "auto")           # "auto" lets Copilot choose; or name a model your plan has
ANALYZE_INTERVAL = _i("ANALYZE_INTERVAL", 60)
ANALYZE_MIN_GAP = _i("ANALYZE_MIN_GAP", 30)
ANALYZE_NEW_LINES = _i("ANALYZE_NEW_LINES", 4)

# --- speech ------------------------------------------------------------------
WHISPER_MODEL = _s("WHISPER_MODEL", "small.en")                 # fast finals
DRAFT_WHISPER_MODEL = _s("DRAFT_WHISPER_MODEL", "base.en")      # instant italic drafts
FINAL_WHISPER_MODEL = _s("FINAL_WHISPER_MODEL", "distil-large-v3")  # accurate refine
ACCURATE_MAX_DECODE = float(_s("ACCURATE_MAX_DECODE", "6"))      # seconds
SPEAKER_DEVICE = _s("SPEAKER_DEVICE", "")

# The speech models live inside the project (desktop/models) and are downloaded there once, on first run
# (scripts/run-desktop.bat does it before starting). After that everything is offline.
MODELS_DIR = APP_DIR / "models"
WHISPER_DIR = MODELS_DIR / "whisper"
ALLOW_MODEL_DOWNLOAD = _b("ALLOW_MODEL_DOWNLOAD", "1")
if not ALLOW_MODEL_DOWNLOAD:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# --- telling the people on a call apart (shared/core/speakers.py) ------------------
SPEAKERS_DEFAULT = _b("SPEAKERS_DEFAULT", "1")                  # the start screen offers it, ticked by default
SPEAKER_MODEL = Path(_s("SPEAKER_MODEL", str(MODELS_DIR / "wespeaker_en_voxceleb_resnet34_LM.onnx")))   # 26 MB, downloaded once
SPEAKER_THRESHOLD = float(_s("SPEAKER_THRESHOLD", "0.62"))      # similarity above which two voices are the same person

# --- screen watching ----------------------------------------------------------
WATCH_MODE = _s("WATCH_MODE", "interval")   # interval: look every N seconds, keep a frame when something new is on screen | settled: only after the screen changed and then stood still
WATCH_DROP_IRRELEVANT = _b("WATCH_DROP_IRRELEVANT", "0")   # 1 = delete watched frames the AI judges unrelated to the call; 0 = keep everything
WATCH_STABLE = _i("WATCH_STABLE", 2)        # seconds the screen must stay unchanged
WATCH_CHANGE = float(_s("WATCH_CHANGE", "0.02"))  # fraction of pixels that counts as changed
WATCH_FORCE = _i("WATCH_FORCE", 90)         # keep a frame after this long of constant change

# --- recording ----------------------------------------------------------------
RECORDINGS_DIR = Path(_s("RECORDINGS_DIR", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DebriefAI" / "recordings")))
RECORD_AUDIO = _b("RECORD_AUDIO", "1")
VIDEO_RECORD = _b("VIDEO_RECORD", "1")
REQUIRE_CONSENT = _b("REQUIRE_CONSENT", "1")
RECORDING_RETENTION_DAYS = _i("RECORDING_RETENTION_DAYS", 30)
MIN_FREE_MB = _i("MIN_FREE_MB", 500)
VIDEO_FPS = _i("VIDEO_FPS", 5)
VIDEO_MAX_WIDTH = _i("VIDEO_MAX_WIDTH", 1280)
VIDEO_CRF = _i("VIDEO_CRF", 30)

# --- app ----------------------------------------------------------------------
PORT = _i("PORT", 5057)
SESSIONS_DIR = Path(_s("SESSIONS_DIR", str(APP_DIR / "sessions")))
MOCK_ANALYZER = _b("MOCK_ANALYZER", "0")
PROFILES_DIR = SHARED_DIR / "profiles"
SAMPLES_DIR = SHARED_DIR / "samples"

# --- form filling -------------------------------------------------------------
FORM_URL = _s("FORM_URL", "")          # address of the portal page that holds your evaluation form (optional)
FORM_MODE = _s("FORM_MODE", "window")        # window | playwright
FORM_BROWSER = _s("FORM_BROWSER", "msedge")
FORM_PROFILE_DIR = Path(_s("FORM_PROFILE_DIR", str(RECORDINGS_DIR.parent / "formprofile")))
FILL_TIMEOUT = _i("FILL_TIMEOUT", 900)


SETTINGS_FILE = APP_DIR / "settings.json"        # choices made in the app (which AI model), kept next to .env


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_setting(key, value):
    data = load_settings()
    data[key] = value
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def saved_token(provider):
    """The token pasted into the app for this model (encrypted with the Windows login), or None."""
    import secrets_store
    return secrets_store.load(provider)


AI_CHOICE = load_settings().get("ai") or _s("AI_PROVIDER", "claude")      # claude | copilot


def load_profiles():
    """Return {id: profile} from profiles/*.json, interview first."""
    out = {}
    for path in sorted(PROFILES_DIR.glob("*.json")):
        try:
            prof = json.loads(path.read_text(encoding="utf-8"))
            out[prof["id"]] = prof
        except Exception as exc:  # a broken profile must not stop the app
            print(f"[config] skipping {path.name}: {exc}")
    return out


def is_cloud_synced(path):
    """True if the folder sits inside OneDrive/Dropbox/etc. (recordings should not)."""
    text = str(path).lower()
    return any(k in text for k in ("onedrive", "dropbox", "google drive", "icloud"))


def public_config():
    return {
        "mock": MOCK_ANALYZER, "port": PORT,
        "recordings_dir": str(RECORDINGS_DIR),
        "recordings_cloud_warning": is_cloud_synced(RECORDINGS_DIR),
        "record_audio": RECORD_AUDIO, "video_record": VIDEO_RECORD,
        "require_consent": REQUIRE_CONSENT,
        "retention_days": RECORDING_RETENTION_DAYS,
        "form_url": FORM_URL, "form_mode": FORM_MODE,
        "analyze_interval": ANALYZE_INTERVAL, "platform": "desktop",
    }
