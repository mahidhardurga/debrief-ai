"""All Claude calls for Debrief AI.

Only transcript text, the user's notes, context lines and screenshots ever leave the PC,
and only through the `claude` CLI in headless mode (`claude -p`) so the user's Claude
subscription login is used.  ANTHROPIC_API_KEY is stripped from the child environment so
an API key can never be picked up by accident, and the `anthropic` package is never imported.

With MOCK_ANALYZER=1 every function returns deterministic fake data so the whole app can be
tested offline without spending any quota.
"""
import base64
import contextlib
import contextvars
import json
import os
import re
import shutil
import subprocess
import threading

import config

ASR_NOTE = ("ASR note: the transcript is machine speech recognition, so infer garbled technical "
            "terms from context, but never invent facts that were not said.")

RATINGS = ["strong", "good", "mixed", "weak", "not enough evidence"]


class AnalyzerError(Exception):
    """Raised with a message that is safe to show the user."""


# --------------------------------------------------------------------------- schemas
def _str():
    return {"type": "string"}


def _arr(items):
    return {"type": "array", "items": items}


def _obj(props, required=None):
    return {"type": "object", "properties": props,
            "required": list(props) if required is None else required,
            "additionalProperties": False}


TOPIC_ITEM = _obj({
    "topic": _str(), "rating": {"type": "string", "enum": RATINGS},
    "asked": _str(), "answered": _str(), "feedback": _str(), "better_answer": _str(),
})
FLAG_ITEM = _obj({"title": _str(), "detail": _str()})
ACTION_ITEM = _obj({"owner": _str(), "task": _str(), "due": _str()})
AREA_ITEM = _obj({
    "id": _str(), "covered": {"type": "boolean"},
    "score": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
    "evidence": _str(), "hint": _str(),
})
# A topic that came up in the call but is not in the rubric yet (the rubric grows during the call).
NEW_AREA_ITEM = _obj({
    "id": _str(), "name": _str(), "weight": {"type": "number", "minimum": 0.5, "maximum": 3},
    "covers": _str(), "off_role": {"type": "boolean"},
})
RUBRIC_SCHEMA = _obj({
    "role": _str(),
    "areas": _arr(_obj({"id": _str(), "name": _str(), "weight": {"type": "number", "minimum": 0.5, "maximum": 3},
                        "covers": _str(), "hint": _str()})),
})


def _section_schema(sec):
    t = sec["type"]
    if t == "list":
        return _arr(_str())
    if t == "text":
        return _str()
    if t == "flags":
        return _arr(FLAG_ITEM)
    if t == "actions":
        return _arr(ACTION_ITEM)
    if t == "topics":
        return _arr(TOPIC_ITEM)
    raise ValueError(f"unknown section type {t}")


FORM_SCHEMA_PART = _obj({
    "behavioral": _arr(_obj({"name": _str(), "proficiency": _str(), "evidence": _str()})),
    "technical": _arr(_obj({
        "competency": _str(), "proficiency": _str(),
        "subcompetencies": _arr(_obj({"name": _str(), "rating": _str(), "evidence": _str()})),
        "comments": _str(),
    })),
    "overall": _obj({"areas_to_probe": _str(), "rating": _str(),
                     "recommendation": _str(), "category": _str()}),
})


def live_schema(profile):
    props = {s["id"]: _section_schema(s) for s in profile["live_sections"]}
    if profile.get("rubric"):
        props["areas"] = _arr(AREA_ITEM)
        props["new_areas"] = _arr(NEW_AREA_ITEM)
    return _obj(props)


def final_schema(profile):
    props = {s["id"]: _section_schema(s) for s in profile["final_sections"]}
    props["summary"] = _str()
    if profile.get("rubric"):
        props["areas"] = _arr(AREA_ITEM)
        props["new_areas"] = _arr(NEW_AREA_ITEM)
        props["weighted_score"] = {"type": ["number", "null"]}
        ids = [d["id"] for d in profile.get("decisions", [])] + ["borderline"]
        props["recommendation"] = _obj({"decision": {"type": "string", "enum": ids}, "reason": _str()})
    for d in profile.get("drafts", []):
        props[d["id"]] = _str()
    if profile.get("form"):
        props["form_answers"] = FORM_SCHEMA_PART
    return _obj(props)


def form_schema():
    return _obj({"form_answers": FORM_SCHEMA_PART})


CORRECTIONS_SCHEMA = _obj({"corrections": _arr(_obj({"id": {"type": "integer"}, "text": _str()}))})
EXHIBIT_SCHEMA = _obj({
    "title": _str(), "content": _str(), "does": _str(), "issues": _arr(_str()),
    "rating": {"type": "string", "enum": RATINGS}, "feedback": _str(), "better": _str(),
    "relevant": {"type": "boolean"},  # watch mode drops frames that are not about the call
})
CHAT_SCHEMA = _obj({
    "reply": _str(), "remember": _str(),
    "action": {"type": "string", "enum": ["none", "fill_form", "stop_fill", "update_answer", "list_pending"]},
    "field": _str(), "value": _str(),
})
PLAN_SCHEMA = _obj({
    "actions": _arr(_obj({"ref": _str(), "value": _str()})),
    "notes": _str(),
})
LINK_SCHEMA = _obj({"ref": {"type": ["string", "null"]}, "reason": _str()})


# --------------------------------------------------------------------------- the CLI call
_EXE = None


def _claude_exe():
    global _EXE
    if _EXE is None:
        _EXE = shutil.which("claude") or shutil.which("claude.exe") or ""
    return _EXE


def _child_env(token=None):
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # always use the subscription login
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    if token:                           # a long-lived token from `claude setup-token`: no /login that expires
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def _desktop_token(provider):
    """Desktop edition: the token the user pasted into the app for this model (None = use the login of the CLI on this PC)."""
    return _AUTH.get() or (getattr(config, "saved_token", lambda p: None)(provider))


_LOGIN_HINT = re.compile(r"(not logged in|please run /login|invalid api key|login|authenticat|expired|401)", re.I)
_TOKEN_MSG = "Claude did not accept the saved token (it may be expired or revoked). Open AI model and paste a new one from `claude setup-token`."
_LOGIN_MSG = "Claude is not signed in or the login expired. Open a terminal, run `claude`, then type /login."


# --------------------------------------------------------------------------- pluggable backend (server edition)
# The desktop edition runs the `claude` CLI directly (below). The server edition installs its own backend so that
# every call goes through its LLM provider, with the calling user's own Claude token (see `auth_scope`).
_BACKEND = None
_AUTH = contextvars.ContextVar("analyzer_auth", default=None)
_PROVIDER = contextvars.ContextVar("analyzer_provider", default=None)
_DESKTOP_PROVIDER = ["claude"]          # the desktop edition: one person, one choice (claude | copilot), kept in desktop settings
PROVIDERS = ("claude", "copilot")


def set_backend(fn):
    """fn(prompt, system, schema, model, images, timeout, auth) -> dict, raising AnalyzerError on failure."""
    global _BACKEND
    _BACKEND = fn


@contextlib.contextmanager
def auth_scope(token, provider=None):
    """Calls made inside this block use `token` (the user's own login for `provider`) when a backend is installed."""
    tok, prov = _AUTH.set(token), _PROVIDER.set(provider)
    try:
        yield
    finally:
        _AUTH.reset(tok)
        _PROVIDER.reset(prov)


def current_auth():
    return _AUTH.get()


def set_provider(name):
    """Desktop edition: which model answers (claude | copilot)."""
    _DESKTOP_PROVIDER[0] = name if name in PROVIDERS else "claude"


def current_provider():
    """The model for this call: the signed-in user's choice on the server, the desktop setting otherwise."""
    return _PROVIDER.get() or _DESKTOP_PROVIDER[0]


def _call(prompt, system, schema, model, images=None, timeout=None):
    """Run one headless Claude call and return the parsed structured result (a dict)."""
    if _BACKEND:
        return _BACKEND(prompt, system, schema, model, images, timeout, _AUTH.get())
    provider = current_provider()
    if provider == "copilot":
        return _call_copilot(prompt, system, schema, images, timeout)
    token = _desktop_token("claude")
    exe = _claude_exe()
    if not exe:
        raise AnalyzerError("The `claude` CLI was not found. Install Claude Code, then run `claude` and /login.")
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-p", "--output-format", "json", "--model", model, "--no-session-persistence",
           "--tools", "", "--system-prompt", system, "--json-schema", json.dumps(schema)]
    stdin = prompt
    if images:
        cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--model", model, "--no-session-persistence", "--tools", "",
               "--system-prompt", system, "--json-schema", json.dumps(schema)]
        content = [{"type": "text", "text": prompt}]
        for png in images:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}})
        stdin = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
    try:
        proc = subprocess.run(
            cmd, input=stdin.encode("utf-8"), capture_output=True, cwd=str(config.SESSIONS_DIR),
            env=_child_env(token), timeout=timeout or config.CLI_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise AnalyzerError("Claude took too long to answer (timeout). Try again.")
    except OSError as exc:
        raise AnalyzerError(f"Could not start the claude CLI: {exc}")
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    result = _parse_cli_output(out, stream=bool(images))
    if result is None:
        text = (err or out).strip()
        if _LOGIN_HINT.search(text):
            raise AnalyzerError(_LOGIN_MSG if not token else _TOKEN_MSG)
        raise AnalyzerError(f"Claude CLI failed (exit {proc.returncode}): {text[:300]}")
    if result.get("is_error"):
        text = str(result.get("result") or "")
        if _LOGIN_HINT.search(text):
            raise AnalyzerError(_LOGIN_MSG if not token else _TOKEN_MSG)
        raise AnalyzerError(f"Claude error: {text[:300]}")
    data = result.get("structured_output")
    if data is None:  # fall back to JSON inside the text result
        data = _json_from_text(str(result.get("result") or ""))
    if data is None:
        raise AnalyzerError("Claude did not return structured data. Try again.")
    return data


def _call_copilot(prompt, system, schema, images, timeout):
    """The desktop edition with GitHub Copilot as the model: the `copilot` CLI with the login of the person at this PC."""
    import copilot
    exe = copilot.find_exe(getattr(config, "COPILOT_CLI_PATH", "copilot"))
    if not exe:
        raise AnalyzerError(copilot.MISSING_MSG)
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return copilot.structured(prompt, system, schema, images, exe=exe, model=getattr(config, "COPILOT_MODEL", "auto"),
                                  timeout=timeout or config.CLI_TIMEOUT, env=copilot.child_env(_desktop_token("copilot")), cwd=config.SESSIONS_DIR)
    except copilot.CopilotError as exc:
        raise AnalyzerError(str(exc)) from exc


def _parse_cli_output(out, stream):
    out = out.strip()
    if not out:
        return None
    if not stream:
        try:
            return json.loads(out)
        except ValueError:
            return None
    last = None
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            last = ev
    return last


def _json_from_text(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------- context text
def fmt_ts(sec):
    sec = int(sec or 0)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def transcript_text(profile, lines, limit_chars=60000, speakers=None):
    rows = []
    for ln in lines:
        if ln.get("marker"):
            rows.append(f"--- {ln['text']} ---")
            continue
        who = profile["you_label"] if ln.get("label") == "you" else profile["other_label"]
        sp = ln.get("speaker")
        if speakers and speakers.get("multi") and ln.get("label") == "other" and sp is not None:
            who += f" ({(speakers.get('names') or {}).get(str(sp)) or 'Speaker ' + str(sp)})"
        rows.append(f"[{fmt_ts(ln.get('ts'))}] {who}: {ln['text']}")
    text = "\n".join(rows)
    return text[-limit_chars:]


def build_context(ctx):
    """ctx: profile, subject, extra, lines, notes, context (list[str]), exhibits, previous."""
    p = ctx["profile"]
    parts = []
    if ctx.get("subject"):
        parts.append(f"{p['subject_label'].upper()}: {ctx['subject']}")
    if p.get("extra") and ctx.get("extra"):
        parts.append(f"{p['extra']['label'].upper()}: {ctx['extra']}")
    if ctx.get("context"):
        parts.append("CONTEXT GIVEN BY THE USER (treat as facts):\n- " + "\n- ".join(ctx["context"]))
    if ctx.get("notes"):
        parts.append("USER'S NOTES:\n" + ctx["notes"])
    ex = [e for e in ctx.get("exhibits", []) if e.get("status") == "done"]
    if ex:
        rows = []
        for i, e in enumerate(ex, 1):
            a = e.get("analysis", {})
            rows.append(f"{i}. {a.get('title', 'Exhibit')} | asked: {e.get('question', '')} | did/said: {e.get('note', '')}\n"
                        f"   content: {a.get('content', '')[:1500]}\n   what it does: {a.get('does', '')}\n"
                        f"   issues: {'; '.join(a.get('issues', []))}\n   rating: {a.get('rating', '')} | {a.get('feedback', '')}")
        parts.append("SCREEN EXHIBITS (hard evidence; each is its own topic):\n" + "\n".join(rows))
    sp = ctx.get("speakers")
    if sp and sp.get("multi"):
        parts.append(f"SEVERAL PEOPLE ARE ON THE {p['other_label'].upper()} SIDE: the transcript tags each of their lines with who spoke, as "
                     f"\"{p['other_label']} (Speaker 2)\" or by name when it is known. Voice recognition can make mistakes; keep each person's "
                     f"answers apart and say so when you are unsure who said something.")
    parts.append("TRANSCRIPT:\n" + transcript_text(p, ctx["lines"], speakers=sp))
    if ctx.get("previous"):
        parts.append("YOUR PREVIOUS ANALYSIS (update it, do not start from scratch):\n"
                     + json.dumps(ctx["previous"], ensure_ascii=False)[:20000])
    return "\n\n".join(parts)


def rubric_text(profile):
    r = profile.get("rubric")
    if not r:
        return ""
    areas = "\n".join(f"- id={a['id']} | {a['name']} (weight {a['weight']}): {a['covers']}" for a in r["areas"])
    return (f"RUBRIC for role '{r['role']}'. Scale: {r['scale']}. Advance threshold: {r['advance_threshold']}.\n"
            f"Return EVERY area below in `areas` (use its id): covered true/false, score 1-5 or null when not "
            f"asked/demonstrated, a one-line evidence, and for uncovered areas a concrete `hint` question fitted "
            f"to the candidate's level and background (empty string when covered).\n{areas}\n"
            f"DYNAMIC TOPICS: this list was built for the role and is not complete. When the conversation substantively "
            f"covers a topic that is NOT in the list (a related skill, or something unrelated to the role), add it to "
            f"`new_areas` (id: new snake_case id; name: 2-4 words; weight 0.5-3 by how much it matters for this role, "
            f"0.5-1 when off-role; covers: one line; off_role: true when it is not part of the role) and ALSO score it in "
            f"`areas` using the same id. A topic counts once the interviewer asked about it and the candidate answered, even in a single "
            f"exchange, so go through every question asked and check it fits a listed topic; if none fits, add it. Do not add "
            f"a topic that was only name-dropped, never duplicate or rename an existing topic, and leave "
            f"`new_areas` empty when nothing new was discussed.")


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40] or "topic"


def merge_new_areas(profile, out):
    """Add topics the AI found during the call to the session's rubric. Returns the number added."""
    r = profile.get("rubric")
    if not r or not isinstance(out, dict):
        return 0
    have_ids = {a["id"] for a in r["areas"]}
    have_names = {a["name"].strip().lower() for a in r["areas"]}
    added = 0
    for n in out.get("new_areas") or []:
        name = (n.get("name") or "").strip()
        aid = _slug(n.get("id") or name)
        if not name or aid in have_ids or name.lower() in have_names:
            continue
        try:
            w = min(3.0, max(0.5, float(n.get("weight") or 1)))
        except (TypeError, ValueError):
            w = 1.0
        r["areas"].append({"id": aid, "name": name, "weight": round(w * 2) / 2, "covers": (n.get("covers") or "").strip(),
                           "hint": "", "dynamic": True, "off_role": bool(n.get("off_role"))})
        have_ids.add(aid)
        have_names.add(name.lower())
        added += 1
    return added


def generate_rubric(profile, role, level="", context=""):
    """Topic list for the role the interviewer is hiring for. Raises AnalyzerError on failure."""
    if config.MOCK_ANALYZER:
        words = [w for w in re.split(r"[^A-Za-z0-9+#.]+", role) if len(w) > 2][:6] or ["General"]
        areas = [{"id": _slug(w), "name": w.title(), "weight": 2 if i < 2 else 1, "covers": f"{w} fundamentals",
                  "hint": f"Walk me through how you have used {w}."} for i, w in enumerate(words)]
        return {"role": role, "areas": areas}
    system = ("You design interview rubrics. Given the role being hired for, list the 8-14 topics a strong interviewer "
              "should cover, most important first. Each topic: id (snake_case), name (2-4 words), weight 0.5-3 "
              "(3 = core must-have, 1 = nice to have), covers (one line of what it includes), hint (one concrete "
              "question that fits the candidate level). Mix technical depth with design/problem-solving and, when "
              "it matters for the role, communication or ownership. No duplicates. Use the job description or "
              "context when given. The topic list is shown live to the interviewer, so keep names short.")
    prompt = (f"ROLE: {role}\nCANDIDATE LEVEL: {level or 'not given'}\n"
              f"CONTEXT (job description / resume):\n{context or '(none)'}\n\n"
              f"Return role (a clean one-line title) and areas.")
    out = _call(prompt, system, RUBRIC_SCHEMA, config.LIVE_MODEL)
    seen, areas = set(), []
    for a in out.get("areas", []):
        name, aid = (a.get("name") or "").strip(), _slug(a.get("id") or a.get("name"))
        if not name or aid in seen:
            continue
        seen.add(aid)
        w = min(3.0, max(0.5, float(a.get("weight") or 1)))
        areas.append({"id": aid, "name": name, "weight": round(w * 2) / 2, "covers": a.get("covers", ""), "hint": a.get("hint", "")})
    if len(areas) < 3:
        raise AnalyzerError("The AI returned too few topics.")
    return {"role": (out.get("role") or role).strip(), "areas": areas[:16]}


def _sections_text(sections):
    return "\n".join(f"- {s['id']} ({s['type']}): {s['title']}" + (f" - {s['detail']}" if s.get("detail") else "")
                     for s in sections)


def weighted_score(profile, areas):
    """Weighted mean of scored areas only. Returns (score|None, scored_count, scored_weight_share)."""
    r = profile.get("rubric")
    if not r:
        return None, 0, 0.0
    weights = {a["id"]: a["weight"] for a in r["areas"]}
    total = sum(weights.values()) or 1
    num = den = 0.0
    n = 0
    for a in areas or []:
        w = weights.get(a.get("id"))
        if w and a.get("score"):
            num += w * a["score"]
            den += w
            n += 1
    if not den:
        return None, 0, 0.0
    return round(num / den, 2), n, den / total


# --------------------------------------------------------------------------- analyses
def live_analysis(ctx):
    p = ctx["profile"]
    if config.MOCK_ANALYZER:
        out = _mock(live_schema(p), ctx)
        _mock_extra(p, out, ctx)
        merge_new_areas(p, out)
        return out
    system = (f"{p['system']}\n\nThis is a LIVE analysis while the call is still running; the conversation is "
              f"incomplete. Keep every field short and useful.\n{ASR_NOTE}\n\nFill these sections:\n"
              f"{_sections_text(p['live_sections'])}\n\n{rubric_text(p)}")
    out = _call(build_context(ctx), system, live_schema(p), config.LIVE_MODEL)
    merge_new_areas(p, out)
    return out


def final_analysis(ctx):
    p = ctx["profile"]
    schema = final_schema(p)
    if config.MOCK_ANALYZER:
        out = _mock(schema, ctx)
        _mock_extra(p, out, ctx)
    else:
        extra = ""
        if p.get("rubric"):
            extra = ("weighted_score = weighted mean of the scored areas only. CONFIDENCE RULE: if the scored "
                     "areas cover under a third of the rubric weight, say so in the summary, use 'borderline' and "
                     "mention how many areas were assessed, and say so if it was mostly not an interview.\n")
        drafts = "\n".join(f"- {d['id']}: {d.get('detail', d['title'])}" for d in p.get("drafts", []))
        form = ""
        if p.get("form"):
            f = p["form"]
            form = (f"\nEVALUATION FORM ({f['title']}). {f['instructions']}\nproficiency options: {f['proficiency_options']}\n"
                    f"behavioral competencies (return all, in this order): {f['behavioral']}\n"
                    f"technical competencies: up to {f['max_technical']} (examples: {f['technical_examples']}), each with "
                    f"proficiency, 1-6 subcompetencies {{name, rating, evidence}} (rating options {f['rating_options']}), "
                    f"and 3-5 sentence `comments` in the interviewer's own voice.\n"
                    f"overall: areas_to_probe, rating (from {f['rating_options']}), recommendation (2-4 sentences), "
                    f"category (one of {f['categories']}).\n")
        system = (f"{p['system']}\n\nThis is the FINAL analysis after the call ended. Be complete and fair.\n"
                  f"{ASR_NOTE}\n{extra}\nSections:\n{_sections_text(p['final_sections'])}\n"
                  f"summary: 3-6 sentences.\nDrafts (one string field each, markdown):\n{drafts}\n{form}\n{rubric_text(p)}")
        out = _call(build_context(ctx), system, schema, config.FINAL_MODEL)
    if p.get("rubric"):
        merge_new_areas(p, out)
        score, _, _ = weighted_score(p, out.get("areas"))
        out["weighted_score"] = score
    return out


def form_answers(ctx, final):
    """Write only the evaluation form answers for an interview that was written up before the form existed."""
    p = ctx["profile"]
    if config.MOCK_ANALYZER:
        return _mock(form_schema(), ctx)["form_answers"]
    ctx = dict(ctx, previous=final)
    f = p["form"]
    system = (f"{p['system']}\n\nWrite ONLY the {f['title']} answers from the transcript, screen exhibits and the "
              f"earlier analysis.\n{f['instructions']}\nproficiency options: {f['proficiency_options']}\n"
              f"behavioral (return all, in order): {f['behavioral']}\nrating options: {f['rating_options']}\n"
              f"technical: up to {f['max_technical']} competencies (examples {f['technical_examples']}), each with "
              f"1-6 subcompetencies and 3-5 sentence comments in the interviewer's voice.\n"
              f"overall.category is one of {f['categories']}.\n{ASR_NOTE}")
    return _call(build_context(ctx), system, form_schema(), config.FINAL_MODEL)["form_answers"]


def correct_lines(profile, lines, context):
    """lines: [{id, label, text}] -> [{id, text}] for lines that were garbled."""
    if config.MOCK_ANALYZER:
        return []
    who = lambda l: profile["you_label"] if l["label"] == "you" else profile["other_label"]
    rows = "\n".join(f"{l['id']}: {who(l)}: {l['text']}" for l in lines)
    system = (f"You fix speech-recognition errors in short transcript lines. Only correct words that are clearly "
              f"garbled technical terms (use the vocabulary and context). Return only lines you changed, with the "
              f"same id. Never add content. {ASR_NOTE}\nVocabulary: {profile.get('vocabulary', '')}")
    prompt = f"Context:\n{context}\n\nLines:\n{rows}"
    return _call(prompt, system, CORRECTIONS_SCHEMA, config.CORRECT_MODEL).get("corrections", [])


def exhibit_analysis(ctx, png, question, note):
    p = ctx["profile"]
    ex = p.get("exhibit", {})
    if config.MOCK_ANALYZER:
        return {"title": "Mock exhibit", "content": "print('hello')", "does": "Prints a greeting.", "issues": [],
                "rating": "good", "feedback": "Mock feedback.", "better": "Mock better approach.", "relevant": True}
    system = (f"{p['system']}\n\nYou are reviewing a screenshot taken during the call. {ex.get('prompt', '')}\n"
              f"Return: title (short), content (verbatim text/code), does (what it does), issues (justified only), "
              f"rating, feedback, better (a better approach), relevant (false when the screen is NOT part of the call: "
              f"desktop, chat app, empty window, a video grid without shared content or unrelated browsing).")
    prompt = (f"{build_context(dict(ctx, exhibits=[], previous=None))}\n\n"
              f"{ex.get('question_label', 'What did you ask?')} {question or '(not given)'}\n"
              f"{ex.get('note_label', 'What did they do or say?')} {note or '(not given)'}\n\nAnalyse the attached screenshot.")
    return _call(prompt, system, EXHIBIT_SCHEMA, config.EXHIBIT_MODEL, images=[png])


def chat_answer(ctx, history, message, form_state=""):
    p = ctx["profile"]
    if config.MOCK_ANALYZER:
        return {"reply": f"(mock) You asked: {message}", "remember": "", "action": "none", "field": "", "value": ""}
    actions = ("action: fill_form (only if the user asks you to fill the evaluation form on the website), stop_fill, "
               "update_answer (field + value: change one answer in the open form window), list_pending (list interviews "
               "still needing feedback), otherwise none. The app carries actions out; never claim you did something.")
    system = (f"{p['system']}\n\nYou are the in-app assistant. Answer using the transcript, notes, exhibits, latest "
              f"analysis and the FORM STATE below. Be brief. If the user states a fact worth remembering (e.g. 'he has 5 "
              f"years of experience'), put it in `remember` (a short statement) so it is added to the context. "
              f"{actions}\n{ASR_NOTE}")
    hist = "\n".join(f"{m['role'].upper()}: {m['text']}" for m in history[-12:])
    prompt = (f"{build_context(ctx)}\n\nFORM STATE:\n{form_state or '(no form answers yet)'}\n\n"
              f"CHAT SO FAR:\n{hist}\n\nUSER: {message}")
    return _call(prompt, system, CHAT_SCHEMA, config.CHAT_MODEL)


PLAN_RULES = (
    "Fill ONLY: the behavioral proficiency radios, Technical Competency 1-3 (competency, proficiency, number of "
    "subcompetencies first, then each subcompetency and its rating, then comments), Areas to Probe, Overall Feedback "
    "Rating, Interviewer's Recommendation and Applicable Category. NEVER touch the photo question, self-declaration, "
    "interviewer, dates or attachments. Copy option text exactly from the field's `options` (closest option; mention "
    "approximations in notes; leave a field out and say so when no option is sensible). Sub-behaviour dropdowns that appear "
    "under a behavioral competency after its level is chosen (for example Logical Reasoning, Problem Solving, Critical "
    "Thinking under Analytical Ability) are rated from that competency's level and evidence line: Intermediate 3, Advanced 4, "
    "Expert 5, Beginner 2, N/A stays N/A; move one step up or down only if the evidence clearly supports it, never above the "
    "evidence; the notes must say they were derived.")


def plan_form_fill(form_text, fields, form):
    """fields: [{ref, label, heading, type, options, value}] -> {actions:[{ref,value}], notes}."""
    if config.MOCK_ANALYZER:
        acts = []
        for f in fields:
            if f.get("value"):
                continue
            if f.get("options"):
                acts.append({"ref": f["ref"], "value": f["options"][min(2, len(f["options"]) - 1)]})
            elif f["type"] in ("textarea", "text"):
                acts.append({"ref": f["ref"], "value": "Mock answer"})
        return {"actions": acts, "notes": "mock plan"}
    system = ("You fill an interview evaluation form on a web page for the interviewer. " + PLAN_RULES +
              " Only return actions for fields in the list, using their `ref`.")
    prompt = f"ANSWERS WRITTEN FROM THE INTERVIEW:\n{form_text}\n\nFORM FIELDS (json):\n{json.dumps(fields, ensure_ascii=False)}"
    return _call(prompt, system, PLAN_SCHEMA, config.FILL_MODEL)


def pick_candidate_link(name, links):
    """links: [{ref, text, href}] (anchors only) -> {ref|None}. Never returns a destructive link."""
    if config.MOCK_ANALYZER:
        low = name.lower().split()
        for l in links:
            if all(t in l["text"].lower() for t in low):
                return {"ref": l["ref"], "reason": "mock match"}
        return {"ref": None, "reason": "mock none"}
    system = ("Pick the one link that opens the candidate's own profile page. The link text contains the candidate's "
              "name tokens (a 'Surname, First' order is fine). Never pick links about withdraw, reject, decline, cancel, "
              "delete, reschedule, sign out, log out or remove. Return null ref when unsure.")
    prompt = f"Candidate: {name}\nLinks:\n{json.dumps(links, ensure_ascii=False)}"
    return _call(prompt, system, LINK_SCHEMA, config.FILL_MODEL)


# --------------------------------------------------------------------------- mock analyzer
def _mock(schema, ctx):
    """Build deterministic fake data from a JSON schema (offline tests and scripts/run-desktop-demo.bat)."""
    n_lines = len([l for l in ctx.get("lines", []) if not l.get("marker")])
    p = ctx["profile"]

    def gen(sch, key=""):
        t = sch.get("type")
        if isinstance(t, list):
            t = [x for x in t if x != "null"][0]
        if "enum" in sch:
            return sch["enum"][min(1, len(sch["enum"]) - 1)] if key != "decision" else sch["enum"][0]
        if t == "object":
            return {k: gen(v, k) for k, v in sch["properties"].items()}
        if t == "array":
            if key == "areas":
                return mock_areas(p, n_lines)
            if key == "new_areas":
                return []
            if key == "followups":
                return ["Mock question: how would you handle failure?"]
            return [gen(sch["items"], key)] if n_lines else []
        if t == "boolean":
            return True
        if t in ("integer", "number"):
            return 3
        return f"Mock {key or 'text'} (based on {n_lines} lines)"
    return gen(schema)


def _mock_extra(profile, out, ctx):
    """Offline stand-in for 'a new topic came up': after 6 lines, add one unrelated topic."""
    n = len([l for l in ctx.get("lines", []) if not l.get("marker")])
    if profile.get("rubric") and n >= 6 and "areas" in out:
        out["new_areas"] = [{"id": "kubernetes", "name": "Kubernetes", "weight": 1, "covers": "pods, deployments", "off_role": True}]
        if not any(a["id"] == "kubernetes" for a in out["areas"]):
            out["areas"].append({"id": "kubernetes", "covered": True, "score": 4, "evidence": "Mock: came up unprompted.", "hint": ""})


def mock_areas(profile, n_lines):
    out = []
    areas = (profile.get("rubric") or {}).get("areas", [])
    covered = min(len(areas), max(1, n_lines // 2)) if n_lines else 0
    for i, a in enumerate(areas):
        done = i < covered
        out.append({"id": a["id"], "covered": done, "score": 3 + (i % 2) if done else None,
                    "evidence": f"Mock evidence for {a['name']}" if done else "",
                    "hint": "" if done else a.get("hint", "")})
    return out
