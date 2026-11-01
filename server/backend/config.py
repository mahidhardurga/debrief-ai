"""Compatibility shim: the analyzer in shared/core was written for the desktop edition and does `import config`.
Inside the server these names come from the server settings (app/config.py), read lazily on every access."""
from app.config import get_settings


def __getattr__(name):
    s = get_settings()
    table = {
        "LIVE_MODEL": s.live_model, "FINAL_MODEL": s.final_model, "CORRECT_MODEL": s.live_model,
        "EXHIBIT_MODEL": s.live_model, "CHAT_MODEL": s.live_model, "FILL_MODEL": s.live_model,
        "CLI_TIMEOUT": s.llm_timeout, "MOCK_ANALYZER": s.llm_provider == "mock",
        "SESSIONS_DIR": s.data_dir, "ANALYZE_INTERVAL": s.analyze_interval, "ANALYZE_MIN_GAP": s.analyze_min_gap,
        "ANALYZE_NEW_LINES": s.analyze_new_lines, "REQUIRE_CONSENT": s.require_consent, "MIN_FREE_MB": s.min_free_mb,
        "WATCH_STABLE": s.watch_stable, "WATCH_CHANGE": s.watch_change, "WATCH_FORCE": s.watch_force,
    }
    if name in table:
        return table[name]
    raise AttributeError(name)
