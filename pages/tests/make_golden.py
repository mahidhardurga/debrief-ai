"""Builds pages/tests/golden.json: what the Python analysis and report code (shared/core) produces for fixed inputs.
The browser edition's JavaScript port must produce exactly the same prompts, schemas and reports (golden.test.mjs).

    python pages/tests/make_golden.py            rewrite golden.json
    python pages/tests/make_golden.py --check    fail when golden.json is out of date (used by the server tests / CI)
"""
import asyncio
import copy
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared" / "core"))
CFG = types.SimpleNamespace(MOCK_ANALYZER=False, LIVE_MODEL="L", FINAL_MODEL="F", CORRECT_MODEL="C", EXHIBIT_MODEL="E", CHAT_MODEL="H",
                            FILL_MODEL="X", CLI_TIMEOUT=1, SESSIONS_DIR=Path("."))
sys.modules["config"] = CFG
import analyzer  # noqa: E402
import reports  # noqa: E402

PROFILES = {}
for _p in sorted((ROOT / "shared" / "profiles").glob("*.json")):
    _j = json.loads(_p.read_text(encoding="utf-8"))
    PROFILES[_j["id"]] = _j
OUT = Path(__file__).with_name("golden.json")

LINES = [
    {"id": 0, "label": "you", "text": "Tell me how a HashMap works.", "ts": 3.2, "marker": False, "speaker": None},
    {"id": 1, "label": "other", "text": "It uses buckets and trees when they get long.", "ts": 9.0, "marker": False, "speaker": 1},
    {"id": 2, "label": "", "text": "Session paused", "ts": 20, "marker": True, "speaker": None},
    {"id": 3, "label": "other", "text": "Sorry, I am back. Résumé with unicode ✓.", "ts": 61.9, "marker": False, "speaker": 2},
    {"id": 4, "label": "you", "text": "And transactions?", "ts": 3725.0, "marker": False, "speaker": None},
]
EXHIBITS = [
    {"n": 1, "status": "done", "question": "Reverse a string", "note": "used a loop", "analysis": {
        "title": "Reverse", "content": "for c in s: r = c + r", "does": "reverses a string", "issues": ["no tests", "slow"],
        "rating": "good", "feedback": "fine", "better": "use slicing", "relevant": True}},
    {"n": 2, "status": "analyzing", "question": "x", "note": "y", "analysis": {}},
]
PREVIOUS = {"summary": "so far", "areas": [{"id": "core_java", "covered": True, "score": 3, "evidence": "ok", "hint": ""}]}


def ctx(profile, **kw):
    base = {"profile": copy.deepcopy(PROFILES[profile]), "subject": "Asha", "extra": "Mid (3-6 yrs)", "lines": LINES, "notes": "nervous at first",
            "context": ["5 yrs Java", "led a team"], "exhibits": EXHIBITS, "previous": None, "speakers": None}
    base.update(kw)
    return base


def area_ids(profile):
    return [a["id"] for a in PROFILES[profile]["rubric"]["areas"]]


FORM_ANSWERS = {"behavioral": [{"name": "Communication", "proficiency": "Advanced", "evidence": "clear"}],
                "technical": [{"competency": "Java", "proficiency": "Advanced", "subcompetencies": [{"name": "Collections", "rating": "4 - Exceeded expectations", "evidence": "strong"}], "comments": "Good."}],
                "overall": {"areas_to_probe": "kafka", "rating": "3 - Met expectations", "recommendation": "Proceed", "category": "Proceed to next round of an interview"}}


def final_canned(profile):
    p = PROFILES[profile]
    out = {"summary": "Solid."}
    if p.get("rubric"):
        a = area_ids(profile)
        out["areas"] = [{"id": a[0], "covered": True, "score": 4, "evidence": "e", "hint": ""}, {"id": a[1], "covered": True, "score": 2, "evidence": "e2", "hint": ""},
                        {"id": "nope", "covered": True, "score": 5, "evidence": "x", "hint": ""}]
        out["new_areas"] = []
        out["recommendation"] = {"decision": "advance", "reason": "good"}
    if p.get("form"):
        out["form_answers"] = FORM_ANSWERS
    for d in PROFILES[profile].get("drafts", []):
        out[d["id"]] = f"draft {d['id']}"
    for s in PROFILES[profile]["final_sections"]:
        out[s["id"]] = {"topics": [{"topic": "HashMap", "rating": "good", "asked": "a", "answered": "b", "feedback": "c", "better_answer": "d"}],
                        "text": "t", "list": ["x", "y"], "flags": [{"title": "T", "detail": "D"}], "actions": [{"owner": "me", "task": "do", "due": ""}]}[s["type"]]
    return out


class Recorder:
    def __init__(self, canned):
        self.canned, self.calls = list(canned), []

    def __call__(self, prompt, system, schema, model, images, timeout, auth):
        self.calls.append({"prompt": prompt, "system": system, "schema": schema, "images": len(images or []), "model": model})
        return copy.deepcopy(self.canned.pop(0))


def run_async(name, fn, args, canned, mock=False):
    """Runs an analyzer function with the model call replaced; stores the prompts it built and what it returned."""
    CFG.MOCK_ANALYZER = mock
    rec = Recorder(canned)
    analyzer.set_backend(rec)
    case_args = copy.deepcopy(args)
    result = fn(*copy.deepcopy(case_args))
    after = copy.deepcopy(case_args)
    result_after = None
    CFG.MOCK_ANALYZER = False
    analyzer.set_backend(None)
    return {"name": name, "fn": fn.__name__, "args": case_args, "canned": canned, "mock": mock,
            "expected": {"calls": rec.calls, "result": result, "args_after": result_after or after}}


def run_with_mutation(name, fn, ctx_obj, canned, extra_args=(), mock=False):
    CFG.MOCK_ANALYZER = mock
    rec = Recorder(canned)
    analyzer.set_backend(rec)
    args = [copy.deepcopy(ctx_obj), *copy.deepcopy(list(extra_args))]
    result = fn(*args)
    CFG.MOCK_ANALYZER = False
    analyzer.set_backend(None)
    return {"name": name, "fn": fn.__name__, "args": [ctx_obj, *extra_args], "canned": canned, "mock": mock,
            "expected": {"calls": rec.calls, "result": result, "profile_after": args[0]["profile"]}}


def pure(name, fn, *args):
    r = fn(*copy.deepcopy(list(args)))
    return {"name": name, "fn": fn.__name__, "args": list(args), "pure": True, "expected": {"result": r}}


def build():
    cases = []
    iv, mt = "interview", "meeting"
    multi = {"multi": True, "names": {"2": "Priya"}}
    cases += [
        pure("rubric text", analyzer.rubric_text, PROFILES[iv]), pure("rubric text (no rubric)", analyzer.rubric_text, PROFILES[mt]),
        pure("transcript text", analyzer.transcript_text, PROFILES[iv], LINES), pure("transcript text with speakers", analyzer.transcript_text, PROFILES[iv], LINES, 60000, multi),
        pure("fmt_ts", analyzer.fmt_ts, 3725.9), pure("build_context minimal", analyzer.build_context, ctx(mt, exhibits=[], notes="", context=[], subject="", extra="")),
        pure("build_context full", analyzer.build_context, ctx(iv, previous=PREVIOUS, speakers=multi)),
        pure("weighted_score", analyzer.weighted_score, PROFILES[iv], final_canned(iv)["areas"]),
        pure("live schema", analyzer.live_schema, PROFILES[iv]), pure("final schema", analyzer.final_schema, PROFILES[iv]), pure("final schema (meeting)", analyzer.final_schema, PROFILES[mt]),
        pure("form schema", analyzer.form_schema),
    ]
    a = area_ids(iv)
    live_canned = {"areas": [{"id": a[0], "covered": True, "score": 4, "evidence": "e", "hint": ""}],
                   "new_areas": [{"id": "Kafka Basics", "name": "Kafka basics", "weight": 1.2, "covers": "  streams ", "off_role": True},
                                 {"id": "x", "name": "Core Java Depth", "weight": 2, "covers": "dup", "off_role": False},
                                 {"id": "", "name": "", "weight": 1, "covers": "", "off_role": False}], "topics": [], "followups": ["q"], "flags": [], "impression": "i"}
    cases.append(run_with_mutation("live analysis (interview)", analyzer.live_analysis, ctx(iv, previous=PREVIOUS, speakers=multi), [live_canned]))
    cases.append(run_with_mutation("live analysis (meeting)", analyzer.live_analysis, ctx(mt), [{k: v for k, v in {s["id"]: ([] if s["type"] != "text" else "t") for s in PROFILES[mt]["live_sections"]}.items()}]))
    cases.append(run_with_mutation("final analysis (interview)", analyzer.final_analysis, ctx(iv), [final_canned(iv)]))
    cases.append(run_with_mutation("final analysis (meeting)", analyzer.final_analysis, ctx(mt), [final_canned(mt)]))
    cases.append(run_with_mutation("form answers", analyzer.form_answers, ctx(iv), [{"form_answers": FORM_ANSWERS}], extra_args=[PREVIOUS]))
    cases.append(run_with_mutation("exhibit analysis", analyzer.exhibit_analysis, ctx(iv), [{"title": "t", "relevant": True}], extra_args=["QUJD", "Reverse a string", "used a loop"]))
    cases.append(run_with_mutation("chat answer", analyzer.chat_answer, ctx(iv, previous=PREVIOUS), [{"reply": "r", "remember": "", "action": "none", "field": "", "value": ""}],
                                   extra_args=[[{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}], "How is he doing?", "form text"]))
    cases.append({**run_async("corrections", analyzer.correct_lines, [PROFILES[iv], [{"id": 1, "label": "other", "text": "spring boot oh auth"}], "Asha; Java"], [{"corrections": [{"id": 1, "text": "Spring Boot OAuth"}]}])})
    cases.append(run_async("generate rubric", analyzer.generate_rubric, [PROFILES[iv], "Golang backend engineer", "Mid (3-6 yrs)", "Kafka, gRPC"],
                           [{"role": " Go Backend Engineer ", "areas": [{"id": "Go Basics", "name": "Go basics", "weight": 3.4, "covers": "c", "hint": "h"}, {"id": "go_basics", "name": "dup", "weight": 1, "covers": "", "hint": ""},
                                                                      {"id": "k", "name": "Kafka", "weight": 0.2, "covers": "", "hint": ""}, {"id": "g", "name": "gRPC", "weight": 2, "covers": "", "hint": ""}]}]))
    # the mock analyzer (demo mode)
    for n in (0, 3, 8):
        lines = [dict(LINES[1], id=i, ts=float(i)) for i in range(n)]
        cases.append(run_with_mutation(f"mock live ({n} lines)", analyzer.live_analysis, ctx(iv, lines=lines, exhibits=[]), [], mock=True))
        cases.append(run_with_mutation(f"mock final ({n} lines)", analyzer.final_analysis, ctx(iv, lines=lines, exhibits=[]), [], mock=True))
    cases.append(run_with_mutation("mock chat", analyzer.chat_answer, ctx(iv), [], extra_args=[[], "hello", ""], mock=True))
    cases.append(run_async("mock rubric", analyzer.generate_rubric, [PROFILES[iv], "Senior Java developer (Spring Boot, AWS)", "", ""], [], mock=True))
    # reports
    final = final_canned(iv)
    final["weighted_score"] = 3.4
    meta = {"subject": "Asha", "extra": "Mid (3-6 yrs)", "started": "2026-11-01T10:00:00"}
    ex_done = [e for e in EXHIBITS]
    clips = [{"name": "Recording 1", "duration_text": "1:02", "size_text": "1.5 MB"}]
    cases += [
        pure("slug", reports.slug, "  Hello, World! Ünï 123 "), pure("slug empty", reports.slug, "", "x"), pure("topics_md", reports.topics_md, final["topics"]),
        pure("topics_md empty", reports.topics_md, []),
        pure("form_fields", reports.form_fields, FORM_ANSWERS, PROFILES[iv]["form"]), pure("form_md", reports.form_md, FORM_ANSWERS, PROFILES[iv]["form"]),
        pure("report_md (interview)", reports.report_md, meta, PROFILES[iv], final, ex_done, "advance", clips),
        pure("report_md (meeting)", reports.report_md, {"subject": "Sync", "extra": "", "started": "2026-11-01T11:00:00"}, PROFILES[mt], final_canned(mt), [], "", None),
        pure("day_summary", reports.day_summary, [{"date": "2026-11-01", "time": "10:00", "name": "A, \"B\"", "type": "Interview", "decision": "advance", "score": 3.4, "feedback": "pending"},
                                                 {"date": "2026-11-01", "time": "09:00", "name": "C", "type": "Meeting notes", "decision": "", "score": "", "feedback": ""},
                                                 {"date": "2026-10-31", "time": "08:00", "name": "old", "type": "x", "decision": "", "score": "", "feedback": ""}], "2026-11-01"),
        pure("to_json", reports.to_json, {"a": [1, 2, {"b": None}], "é": "ü", "e": [], "f": {}}),
    ]
    for sec in PROFILES[iv]["final_sections"] + PROFILES[mt]["final_sections"]:
        cases.append(pure(f"section_md {sec['type']}", reports.section_md, sec, final_canned(iv if sec in PROFILES[iv]["final_sections"] else mt)[sec["id"]]))
    return {"version": 1, "cases": cases}


if __name__ == "__main__":
    text = json.dumps(build(), ensure_ascii=False, indent=1, sort_keys=False, default=list) + "\n"
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print("pages/tests/golden.json is out of date: run  python pages/tests/make_golden.py  and commit it")
            sys.exit(1)
        print("golden.json is up to date")
    else:
        OUT.write_text(text, encoding="utf-8")
        print("wrote", OUT, len(json.loads(text)["cases"]), "cases")
