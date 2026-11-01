"""Starts the form filler for one interview and follows its progress.

window mode (default): formbrowser.py runs as a SEPARATE process because pywebview must own the main
thread.  The two sides talk through small JSON files in a work folder:
    job.json      server -> worker  (url, candidate name, answers text, paths)
    status.json   worker -> server  (state, log lines, report, error, acks); the server tails it every 0.7 s
    control.json  server -> worker  ({seq, set:[{field, value}]}) to change one answer after the fill
The window stays open after the fill so the user can review it and press Submit themselves.

playwright mode (optional, FORM_MODE=playwright): lets `claude -p` drive Edge/Chrome through the
Playwright MCP.  It only works if IT policy allows remote debugging, so a failed MCP start is
detected from the CLI init event and reported.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "shared" / "core"))   # analyzer.py, reports.py
import config
import analyzer

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _write_json_retry(path, data, tries=20):
    """Windows refuses to replace a file the other side is reading, so retry."""
    tmp = Path(str(path) + ".tmp")
    for _ in range(tries):
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError:
            time.sleep(0.1)
    return False


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


class Fill:
    def __init__(self, work_dir, job, on_change=None):
        self.dir = Path(work_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.job = dict(job)
        self.job["control_path"] = str(self.dir / "control.json")
        self.job["status_path"] = str(self.dir / "status.json")
        self.job["profile_dir"] = str(config.FORM_PROFILE_DIR)
        self.on_change = on_change or (lambda s: None)
        self.proc = None
        self.status = {"state": "starting", "log": [], "report": "", "error": "", "acks": [], "steps": 0}
        self.seq = 0
        self._cancel = threading.Event()
        self._thread = None

    # ---- lifecycle -----------------------------------------------------------
    def start(self):
        _write_json_retry(self.dir / "job.json", self.job)
        _write_json_retry(self.dir / "status.json", self.status)
        _write_json_retry(self.dir / "control.json", {"seq": 0, "set": []})
        self._thread = threading.Thread(target=self._run, daemon=True, name="formfill")
        self._thread.start()

    def _run(self):
        if config.FORM_MODE == "playwright":
            self._run_playwright()
            return
        cmd = [sys.executable, str(config.BASE_DIR / "formbrowser.py"), str(self.dir)]
        try:
            self.proc = subprocess.Popen(cmd, cwd=str(config.BASE_DIR), env=dict(os.environ))
        except OSError as exc:
            self._set(state="error", error=f"Could not start the form window: {exc}")
            return
        deadline = time.time() + config.FILL_TIMEOUT
        last = None
        while not self._cancel.is_set():
            st = _read_json(self.dir / "status.json")
            if st and st != last:
                last = st
                self.status = st
                self.on_change(st)
            alive = self.proc.poll() is None
            if st and st.get("state") in ("done", "error") and not alive:
                break
            if not alive:
                if not st or st.get("state") not in ("done", "error"):
                    self._set(state="error", error=(st or {}).get("error") or "The form window closed before the fill finished.")
                break
            if time.time() > deadline and st and st.get("state") not in ("done",):
                self._set(state="error", error="Filling took too long (FILL_TIMEOUT).")
                self.cancel(keep_window=False)
                break
            time.sleep(0.7)

    def _set(self, **kw):
        self.status = dict(self.status, **kw)
        self.on_change(self.status)

    def cancel(self, keep_window=False):
        self._cancel.set()
        if self.proc and self.proc.poll() is None and not keep_window:
            try:
                self.proc.terminate()
            except OSError:
                pass
        if self.status.get("state") not in ("done", "error"):
            self._set(state="error", error="Stopped.")

    def running(self):
        return self.status.get("state") not in ("done", "error")

    def window_open(self):
        return bool(self.proc and self.proc.poll() is None)

    def update_answer(self, field, value):
        """Ask the open window to change one field; the worker's acknowledgement shows up in status.acks."""
        if not self.window_open():
            return None
        self.seq += 1
        _write_json_retry(self.dir / "control.json", {"seq": self.seq, "set": [{"field": field, "value": value}]})
        return self.seq

    # ---- playwright mode -----------------------------------------------------
    def _run_playwright(self):
        cfg = {"mcpServers": {"playwright": {"command": "cmd", "args": [
            "/c", "npx", "-y", "@playwright/mcp@latest", "--browser", config.FORM_BROWSER,
            "--user-data-dir", str(config.FORM_PROFILE_DIR)]}}}
        cfg_path = self.dir / "mcp.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        prompt = (f"Open {self.job['url']} in the browser. Find the candidate '{self.job['name']}', open their page and "
                  f"click 'Provide Feedback'. Wait for me to sign in if needed. Fill ONLY the evaluation form fields "
                  f"from these answers, never the photo question, self-declaration, interviewer, dates or "
                  f"attachments, never type passwords and NEVER click Submit or Save.\n\n{self.job['answers']}")
        cmd = [analyzer._claude_exe(), "-p", "--output-format", "stream-json", "--verbose", "--model", config.FILL_MODEL,
               "--mcp-config", str(cfg_path), "--strict-mcp-config", "--allowedTools", "mcp__playwright",
               "--no-session-persistence"]
        self._set(state="filling", log=["Starting the browser through the Playwright MCP..."])
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         cwd=str(config.SESSIONS_DIR), env=analyzer._child_env(), creationflags=NO_WINDOW)
            self.proc.stdin.write(prompt.encode("utf-8"))
            self.proc.stdin.close()
        except OSError as exc:
            self._set(state="error", error=f"Could not start Claude: {exc}")
            return
        log, steps = [], 0
        for raw in self.proc.stdout:
            if self._cancel.is_set():
                break
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                bad = [s for s in ev.get("mcp_servers", []) if s.get("status") != "connected"]
                if bad:
                    self._set(state="error", error=("The Playwright browser could not start (your IT policy probably "
                              "blocks remote debugging). Use FORM_MODE=window instead."))
                    self.cancel()
                    return
            if ev.get("type") == "assistant":
                for c in ev.get("message", {}).get("content", []):
                    if c.get("type") == "tool_use":
                        steps += 1
                        log.append(f"{c.get('name', 'step').replace('mcp__playwright__', '')}")
            if ev.get("type") == "result":
                self._set(state="done", report=str(ev.get("result", ""))[:2000], log=log[-200:], steps=steps)
                return
            self._set(log=log[-200:], steps=steps)
        if self.status.get("state") != "done":
            self._set(state="error", error="The Playwright run ended without a result.")
