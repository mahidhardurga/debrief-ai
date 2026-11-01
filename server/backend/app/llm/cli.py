"""Claude Code CLI provider: `claude -p` headless.

CLAUDE_AUTH=per_user (default): every call runs with the calling user's OWN Claude login, a long-lived token the
user created with `claude setup-token` and connected in the app. It is passed to the child process through
CLAUDE_CODE_OAUTH_TOKEN, each user gets a private CLAUDE_CONFIG_DIR so nothing is shared between accounts, and
there is no fallback to anyone else's credentials.
CLAUDE_AUTH=shared: the old behaviour, one login stored for the server's OS user and used for everybody
(only appropriate for you or a small team).
ANTHROPIC_API_KEY is always removed from the child environment so a subscription login is never replaced by a key.
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import defaultdict

from ..config import get_settings
from .base import LLMError

LOGIN_HINT = re.compile(r"(not logged in|please run /login|invalid api key|invalid.*token|oauth|login|authenticat|expired|unauthori[sz]ed|401)", re.I)
LOGIN_MSG_SHARED = "Claude is not signed in on the server or the login expired. Run `claude` on the server and use /login."
LOGIN_MSG_USER = ("Your Claude connection was rejected or has expired. Open Claude account, create a new token with "
                  "`claude setup-token` and connect it again.")
NOT_CONNECTED = "Connect your Claude account first (Claude account page): each user analyses calls with their own Claude login."


def _json_from_text(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None


class ClaudeCLI:
    def __init__(self):
        s = get_settings()
        self.prefix = [shutil.which(s.claude_cli_path) or s.claude_cli_path]     # tests replace this with a fake CLI
        self.timeout = s.llm_timeout
        self.per_user = s.claude_auth != "shared"
        self.needs_user_credentials = self.per_user
        self._sem = threading.Semaphore(max(1, s.llm_concurrency))               # protects Claude's rate limits
        self._user_limit = max(1, s.llm_user_concurrency)
        self._user_sems: dict[str, threading.Semaphore] = defaultdict(lambda: threading.Semaphore(self._user_limit))
        self._lock = threading.Lock()
        self.workdir = tempfile.mkdtemp(prefix="debrief-cli-")                   # no project files to read
        self.homes = s.data_dir / "claude-home"

    def _user_slot(self, key: str) -> threading.Semaphore:
        with self._lock:
            return self._user_sems[key]

    def _env(self, auth: str | None):
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if self.per_user:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = auth or ""
            home = self.homes / hashlib.sha256((auth or "").encode()).hexdigest()[:24]   # private state per Claude account
            home.mkdir(parents=True, exist_ok=True)
            env["CLAUDE_CONFIG_DIR"] = str(home)
        return env

    def structured(self, system, prompt, schema, model, images=None, auth=None):
        if self.per_user and not auth:
            raise LLMError(NOT_CONNECTED)
        login_msg = LOGIN_MSG_USER if self.per_user else LOGIN_MSG_SHARED
        slot = self._user_slot(hashlib.sha256((auth or "shared").encode()).hexdigest())
        base = [*self.prefix, "-p", "--model", model, "--no-session-persistence", "--tools", "",
                "--system-prompt", system, "--json-schema", json.dumps(schema)]
        if images:
            cmd = base + ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
            content = [{"type": "text", "text": prompt}] + [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(i).decode()}}
                for i in images]
            stdin = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
        else:
            cmd, stdin = base + ["--output-format", "json"], prompt
        with slot, self._sem:
            try:
                proc = subprocess.run(cmd, input=stdin.encode(), capture_output=True, cwd=self.workdir, env=self._env(auth),
                                      timeout=self.timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except FileNotFoundError:
                raise LLMError("The `claude` CLI is not installed on the server.")
            except subprocess.TimeoutExpired:
                raise LLMError("Claude took too long to answer (timeout).")
        out, err = proc.stdout.decode("utf-8", "replace").strip(), proc.stderr.decode("utf-8", "replace")
        result = None
        if images:
            for line in out.splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "result":
                    result = ev
        else:
            try:
                result = json.loads(out)
            except ValueError:
                result = None
        if result is None:
            text = err or out
            raise LLMError(login_msg if LOGIN_HINT.search(text) else f"Claude CLI failed: {text[:300]}")
        if result.get("is_error"):
            text = str(result.get("result") or "")
            raise LLMError(login_msg if LOGIN_HINT.search(text) else f"Claude error: {text[:300]}")
        data = result.get("structured_output")
        if data is None:
            data = _json_from_text(str(result.get("result") or ""))
        if data is None:
            raise LLMError("Claude did not return structured data.")
        return data
