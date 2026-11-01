"""Settings, read from environment variables (and an optional .env). Everything has a safe default for dev."""
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"                                    # dev | prod (prod refuses the default secret)
    database_url: str = "sqlite:///./data/debrief.db"
    data_dir: Path = Path("./data")                     # exhibits, exports
    secret_key: str = ""
    access_token_minutes: int = 60 * 12
    allow_registration: bool = True                     # the first user is always allowed and becomes admin
    cors_origins: str = "http://localhost:5173"

    # --- LLM ------------------------------------------------------------------
    llm_provider: str = "cli"                           # cli | anthropic | mock
    live_model: str = "sonnet"
    final_model: str = "opus"
    llm_timeout: int = 240
    llm_concurrency: int = 4                            # parallel `claude` processes on the whole server
    llm_user_concurrency: int = 2                       # ... and per user, so one user cannot starve the others
    claude_auth: str = "per_user"                       # cli provider: per_user = every user connects their own Claude
                                                        # login; shared = one login stored on the server for everyone
    encryption_key: str = ""                            # Fernet key for stored Claude tokens; derived from SECRET_KEY if empty
    claude_cli_path: str = "claude"
    copilot_cli_path: str = "copilot"                   # GitHub Copilot as the other model users can choose (npm i -g @github/copilot)
    copilot_model: str = "auto"                         # "auto" lets Copilot pick; or pin a model your plan has
    anthropic_api_key: str = ""                         # only for llm_provider=anthropic

    # --- analysis cadence -----------------------------------------------------------
    analyze_interval: int = 60
    analyze_min_gap: int = 30
    analyze_new_lines: int = 4

    # --- sessions, recording and screen watching (mirrors the desktop edition's settings) ------------------------
    require_consent: bool = True                        # recording needs the "everyone was told" confirmation
    min_free_mb: int = 500                              # stop recording / refuse uploads below this free disk space
    recording_retention_days: int = 30                  # whole recordings older than this are deleted (0 keeps everything)
    max_upload_mb: int = 2048                           # largest single recording upload
    watch_stable: int = 2                               # screen watching: seconds the screen must stay unchanged
    watch_change: float = 0.02                          # fraction of pixels that counts as changed
    watch_force: int = 90                               # keep a frame after this long of constant change
    corrections: bool = True                            # AI fixes of garbled technical terms in the live transcript
    demo_delay: float = 2.0                             # LLM_PROVIDER=mock: seconds between replayed sample lines
    cookie_secure: str = "auto"                         # auto (secure when served over https) | always | never

    # --- speech (server-side Whisper) -------------------------------------------------
    asr_enabled: bool = True
    asr_model: str = "small.en"
    asr_device: str = "cpu"                             # cpu | cuda
    asr_compute_type: str = "int8"
    asr_workers: int = 2
    asr_allow_download: bool = False
    speakers_default: bool = True                       # the start screen offers "tell the people on the call apart", ticked by default
    speaker_model: str = ""                            # path of the speaker model; default <DATA_DIR>/models/<file> (downloaded once if allowed)
    speaker_threshold: float = 0.62                     # cosine similarity above which two voices count as the same person
    asr_refine_model: str = ""                         # optional second, more accurate pass (e.g. distil-large-v3) that quietly replaces a line's text
    asr_refine_backlog: int = 4                         # lines waiting for the second pass; more are skipped so the server never falls behind
    asr_partials: bool = True                           # italic live drafts while someone is still speaking

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.env == "prod" and len(s.secret_key) < 32:
        raise RuntimeError("SECRET_KEY must be set to a random string of 32+ characters when ENV=prod")
    if not s.secret_key:
        s.secret_key = secrets.token_urlsafe(32)        # dev only: tokens die on restart
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
