"""Server session engine.

The analysis code (prompts, schemas, scoring, dynamic topic lists, chat) is shared with the desktop edition and
lives in `shared/core/analyzer.py`; this package only adds what a multi-user server needs: per-user storage,
sessions driven by browser audio/screen/recording uploads, and the user's own Claude login for every call.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve()


def _find_shared() -> Path:
    for p in HERE.parents:                                   # repo: <root>/shared, Docker image: /app/shared
        if (p / "shared" / "core" / "analyzer.py").exists():
            return p / "shared"
    raise RuntimeError("The shared/ folder (shared/core/analyzer.py) was not found")


SHARED = _find_shared()
BACKEND = HERE.parents[2]                                    # server/backend (or /app): holds the `config` shim
for _p in (str(SHARED / "core"), str(BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import analyzer  # noqa: E402  (shared/core)

from ..llm import LLMError, get_llm_for  # noqa: E402


def _backend(prompt, system, schema, model, images, timeout, auth):
    """Route every analyzer call through the model the calling user picked, with their own token for it."""
    try:
        return get_llm_for(analyzer.current_provider()).structured(system, prompt, schema, model, images=images, auth=auth)
    except LLMError as exc:
        raise analyzer.AnalyzerError(str(exc)) from exc


analyzer.set_backend(_backend)
