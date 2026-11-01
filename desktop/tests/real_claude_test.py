"""One real call per schema through the Claude CLI (uses your subscription; ~5 small calls)."""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT.parent / "shared" / "core"))
import config  # noqa: E402
import analyzer  # noqa: E402
import windows_capture as wc  # noqa: E402
import numpy as np  # noqa: E402

assert not config.MOCK_ANALYZER
prof = config.load_profiles()["interview"]
lines = []
for i, row in enumerate((ROOT.parent / "shared" / "samples" / "sample_transcript.txt").read_text(encoding="utf-8").splitlines()):
    who, text = row.split(":", 1)
    lines.append({"label": "you" if who == "Interviewer" else "other", "text": text.strip(), "ts": i * 20, "marker": False})
ctx = {"profile": prof, "subject": "Test Candidate", "extra": "Mid (3-6 yrs)", "lines": lines, "notes": "", "context": [], "exhibits": [], "previous": None}


def timed(name, fn):
    t = time.time()
    out = fn()
    print(f"[{name}] ok in {time.time() - t:.0f}s")
    return out


live = timed("live", lambda: analyzer.live_analysis(ctx))
print("  areas:", len(live["areas"]), "covered:", sum(a["covered"] for a in live["areas"]), "| topics:", len(live["topics"]), "| followups:", live["followups"][:1])
final = timed("final(opus)", lambda: analyzer.final_analysis(ctx))
print("  score:", final["weighted_score"], "| rec:", final["recommendation"]["decision"], "| topics:", len(final["topics"]),
      "| drafts:", [k for k in ("topic_feedback", "advance_handoff", "reject_feedback") if final.get(k)], "| form_data behavioral:", len(final["form_answers"]["behavioral"]))
assert final["form_answers"]["overall"]["category"], "category missing"
form_data = timed("form_answers", lambda: analyzer.form_answers(ctx, final))
print("  behavioral:", [b["proficiency"] for b in form_data["behavioral"]][:4])
import reports  # noqa: E402
text = reports.form_md(form_data, prof["form"])
fields = [{"ref": "F1", "label": "Analytical Ability", "heading": "Behavioral competencies", "type": "radio", "options": prof["form"]["proficiency_options"], "value": ""},
          {"ref": "F2", "label": "Areas to Probe", "heading": "Overall", "type": "textarea", "options": [], "value": ""},
          {"ref": "F3", "label": "Does the photo match?", "heading": "Photo", "type": "radio", "options": ["Yes", "No"], "value": ""},
          {"ref": "F4", "label": "Overall Feedback Rating", "heading": "Overall", "type": "select", "options": prof["form"]["rating_options"], "value": ""}]
plan = timed("plan", lambda: analyzer.plan_form_fill(text, fields, prof["form"]))
print("  actions:", [(a["ref"], a["value"][:30]) for a in plan["actions"]])
assert all(a["ref"] != "F3" for a in plan["actions"]), "photo question must not be filled"
links = [{"ref": "L1", "text": "Withdraw Ravi Kumar", "href": "x"}, {"ref": "L2", "text": "Kumar, Ravi", "href": "y"}, {"ref": "L3", "text": "Verma, Asha", "href": "z"}]
pick = timed("link", lambda: analyzer.pick_candidate_link("Ravi Kumar", links))
print("  picked:", pick)
assert pick["ref"] == "L2"
png = wc.encode_png(wc.resize(wc.bgra_to_rgb(wc.fake_frame()), 640))
ex = timed("exhibit(image)", lambda: analyzer.exhibit_analysis(dict(ctx, exhibits=[]), png, "Reverse a string", "typed a loop"))
print("  exhibit:", ex["title"], "| relevant:", ex["relevant"])
print("REAL CLAUDE OK")
