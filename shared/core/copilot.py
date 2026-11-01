"""GitHub Copilot as the model, through the `copilot` CLI (npm i -g @github/copilot) in non-interactive mode.

Used by both editions, so a Copilot call behaves the same on the desktop and on the server. The Copilot CLI has no
"force this JSON schema" switch like `claude --json-schema`, so the schema goes into the prompt, the reply is parsed,
and one repair attempt is made if it does not fit. No tools are enabled: the model only answers.

Sign-in: the user's own GitHub account with a Copilot plan. On the desktop that is the login the CLI already has
(`copilot login`); on the server each user pastes a GitHub token (fine-grained, "Copilot Requests" permission) that is
passed to the CLI as COPILOT_GITHUB_TOKEN with a private COPILOT_HOME, exactly like the per-user Claude token.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_MODEL = "auto"          # Copilot picks the model; set COPILOT_MODEL (or the server's setting) to pin one
LOGIN_HINT = re.compile(r"(not (?:logged|signed) in|please (?:run|use) /?login|copilot login|authenticat|unauthori[sz]ed|bad credentials|"
                        r"invalid.*token|token.*(?:invalid|expired)|401|no (?:copilot )?(?:subscription|access)|not.*entitled)", re.I)
LOGIN_MSG = "GitHub Copilot is not signed in, or the token was rejected or expired. Sign in again (desktop: run `copilot login`; server: paste a new token)."
MISSING_MSG = "The `copilot` CLI is not installed (npm install -g @github/copilot)."


class CopilotError(Exception):
    """A message that is safe to show to the user."""


def find_exe(configured: str = "copilot") -> str:
    return shutil.which(configured) or shutil.which(configured + ".cmd") or shutil.which(configured + ".exe") or ""


def json_from_text(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:                                       # the first balanced {...} that parses
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                esc = (c == "\\" and not esc)
                if c == '"' and not esc:
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def _fits(data, schema) -> str:
    """'' when `data` has the shape the schema asks for at the top level (and for array items), else why not."""
    if not isinstance(data, dict):
        return "the reply is not a JSON object"
    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        return "missing keys: " + ", ".join(missing)
    for key, spec in (schema.get("properties") or {}).items():
        if key in data and spec.get("type") == "array" and not isinstance(data[key], list):
            return f"{key} must be a list"
        if key in data and spec.get("type") == "object" and not isinstance(data[key], dict):
            return f"{key} must be an object"
    return ""


def build_prompt(system: str, prompt: str, schema: dict, n_images: int = 0, problem: str = "") -> str:
    parts = [f"# Instructions\n{system.strip()}", f"# Task\n{prompt.strip()}"]
    if n_images:
        parts.append(f"# Screenshots\n{n_images} screenshot file(s) are attached to this message, in order.")
    parts.append("# Answer format\nReply with ONE JSON object and nothing else: no explanation, no markdown, no code fence. "
                 "It must conform exactly to this JSON Schema:\n" + json.dumps(schema))
    if problem:
        parts.append(f"Your previous reply was rejected ({problem}). Reply again with only the corrected JSON object.")
    return "\n\n".join(parts)


def _once(text, images, *, exe, model, timeout, env, cwd):
    tmp = Path(tempfile.mkdtemp(prefix="debrief-cop-", dir=cwd))
    try:
        cmd = [exe, "-s", "--no-auto-update", "--no-ask-user", "--no-custom-instructions", "--no-color", "--no-experimental",
               "--disable-mcp-server", "github-mcp-server", "--available-tools", "--model", model or DEFAULT_MODEL]
        for i, png in enumerate(images or []):
            f = tmp / f"screenshot{i + 1}.png"
            f.write_bytes(png)
            cmd += ["--attachment", str(f)]
        try:
            proc = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, cwd=cwd, env=env, timeout=timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError:
            raise CopilotError(MISSING_MSG)
        except subprocess.TimeoutExpired:
            raise CopilotError("Copilot took too long to answer (timeout).")
        except OSError as exc:
            raise CopilotError(f"Could not start the copilot CLI: {exc}")
        out = proc.stdout.decode("utf-8", "replace").strip()
        err = proc.stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not out:
            msg = err or out or f"exit {proc.returncode}"
            raise CopilotError(LOGIN_MSG if LOGIN_HINT.search(msg) else f"Copilot CLI failed: {msg[:300]}")
        if out.startswith("Error:"):                        # e.g. the model is not available on this plan
            raise CopilotError(LOGIN_MSG if LOGIN_HINT.search(out) else f"Copilot: {out[:300]}")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def structured(prompt, system, schema, images=None, *, exe, model="", timeout=240, env=None, cwd=None) -> dict:
    """One structured answer from Copilot as a dict; raises CopilotError."""
    cwd = str(cwd or tempfile.gettempdir())
    env = env if env is not None else dict(os.environ)
    problem = ""
    for attempt in range(2):
        out = _once(build_prompt(system, prompt, schema, len(images or []), problem), images, exe=exe, model=model,
                    timeout=timeout, env=env, cwd=cwd)
        data = json_from_text(out)
        problem = "it was not valid JSON" if data is None else _fits(data, schema)
        if not problem:
            return data
    raise CopilotError(f"Copilot did not return the structured data that was asked for ({problem}). Try again.")


def child_env(token: str | None = None, home: str | None = None) -> dict:
    """The environment for a CLI call. A token (server) replaces whatever login the machine has; with none (desktop)
    the CLI uses the login of the person at this PC."""
    env = dict(os.environ)
    if token is not None:
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        env["COPILOT_GITHUB_TOKEN"] = token
    if home:
        env["COPILOT_HOME"] = home
    env["COPILOT_AUTO_UPDATE"] = "false"
    return env
