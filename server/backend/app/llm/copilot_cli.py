"""GitHub Copilot provider: the `copilot` CLI in non-interactive mode (see shared/core/copilot.py for how it is driven).

Each user connects their own GitHub token (fine-grained, "Copilot Requests" permission). It is passed to the child process
through COPILOT_GITHUB_TOKEN, every account gets a private COPILOT_HOME, and there is no fallback to anyone else's login.
"""
import hashlib
import tempfile
import threading
from collections import defaultdict

from ..config import get_settings
from ..engine import SHARED  # noqa: F401  (puts shared/core on sys.path)
from .base import LLMError

NOT_CONNECTED = "Connect your GitHub Copilot token first (Account > AI model): each user analyses calls with their own Copilot access."


class CopilotCLI:
    def __init__(self):
        import copilot
        s = get_settings()
        self.copilot = copilot
        self.exe = copilot.find_exe(s.copilot_cli_path) or s.copilot_cli_path       # tests replace this with a fake CLI
        self.model, self.timeout = s.copilot_model, s.llm_timeout
        self.needs_user_credentials = True
        self._sem = threading.Semaphore(max(1, s.llm_concurrency))
        self._user_limit = max(1, s.llm_user_concurrency)
        self._user_sems: dict[str, threading.Semaphore] = defaultdict(lambda: threading.Semaphore(self._user_limit))
        self._lock = threading.Lock()
        self.workdir = tempfile.mkdtemp(prefix="debrief-cop-run-")
        self.homes = s.data_dir / "copilot-home"

    def structured(self, system, prompt, schema, model, images=None, auth=None):
        if not auth:
            raise LLMError(NOT_CONNECTED)
        digest = hashlib.sha256(auth.encode()).hexdigest()
        home = self.homes / digest[:24]                                              # private state per GitHub account
        home.mkdir(parents=True, exist_ok=True)
        with self._lock:
            slot = self._user_sems[digest]
        try:
            with slot, self._sem:
                return self.copilot.structured(prompt, system, schema, images, exe=self.exe, model=self.model, timeout=self.timeout,
                                               env=self.copilot.child_env(auth, str(home)), cwd=self.workdir)
        except self.copilot.CopilotError as exc:
            raise LLMError(str(exc)) from exc
