"""Markdown/CSV builders: report.md, the evaluation form text, topic-wise feedback and the day summary."""
import csv
import io
import json
import re
from pathlib import Path

import analyzer


def slug(text, default="session"):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:40] or default)


def _bullets(items):
    return "\n".join(f"- {i}" for i in items) if items else "_none_"


def topics_md(topics):
    if not topics:
        return "_No technical topics were discussed._"
    out = []
    for i, t in enumerate(topics, 1):
        out.append(f"### {i}. {t.get('topic', '')}  ({t.get('rating', '')})\n"
                   f"- **Asked:** {t.get('asked', '')}\n- **Answered:** {t.get('answered', '')}\n"
                   f"- **Feedback:** {t.get('feedback', '')}\n- **A stronger answer:** {t.get('better_answer', '')}")
    return "\n\n".join(out)


def section_md(sec, value):
    title = f"## {sec['title']}\n"
    t = sec["type"]
    if t == "topics":
        return title + topics_md(value)
    if t == "text":
        return title + (value or "_none_")
    if t == "list":
        return title + _bullets(value)
    if t == "flags":
        return title + _bullets([f"**{f.get('title', '')}** - {f.get('detail', '')}" for f in value or []])
    if t == "actions":
        return title + _bullets([f"{a.get('task', '')} (owner: {a.get('owner') or '-'}, due: {a.get('due') or '-'})"
                                 for a in value or []])
    return title


def form_fields(form_data, form):
    """The form answers as an ordered list of {label, value} so every field can have a Copy button."""
    if not form_data:
        return []
    out = []
    for b in form_data.get("behavioral", []):
        out.append({"label": f"{b['name']} - proficiency", "value": b.get("proficiency", "")})
        out.append({"label": f"{b['name']} - evidence", "value": b.get("evidence", "")})
    for i, t in enumerate(form_data.get("technical", []), 1):
        out.append({"label": f"Technical competency {i}", "value": t.get("competency", "")})
        out.append({"label": f"Technical competency {i} - proficiency", "value": t.get("proficiency", "")})
        for s in t.get("subcompetencies", []):
            out.append({"label": f"  {s['name']} - rating", "value": s.get("rating", "")})
            out.append({"label": f"  {s['name']} - evidence", "value": s.get("evidence", "")})
        out.append({"label": f"Technical competency {i} - comments", "value": t.get("comments", "")})
    o = form_data.get("overall", {})
    out.append({"label": "Areas to probe", "value": o.get("areas_to_probe", "")})
    out.append({"label": "Overall feedback rating", "value": o.get("rating", "")})
    out.append({"label": "Interviewer's recommendation", "value": o.get("recommendation", "")})
    out.append({"label": "Applicable category", "value": o.get("category", "")})
    return out


def form_md(form_data, form):
    if not form_data:
        return ""
    title = (form or {}).get("title", "Evaluation form")
    lines = [f"# {title}", ""]
    lines.append("## Behavioral competencies")
    for b in form_data.get("behavioral", []):
        lines.append(f"- **{b['name']}**: {b.get('proficiency', '')} - {b.get('evidence', '')}")
    for i, t in enumerate(form_data.get("technical", []), 1):
        lines += ["", f"## Technical competency {i}: {t.get('competency', '')} ({t.get('proficiency', '')})"]
        for s in t.get("subcompetencies", []):
            lines.append(f"- {s['name']}: {s.get('rating', '')} - {s.get('evidence', '')}")
        lines += ["", f"**Comments:** {t.get('comments', '')}"]
    o = form_data.get("overall", {})
    lines += ["", "## Overall", f"- **Areas to probe:** {o.get('areas_to_probe', '')}",
              f"- **Overall feedback rating:** {o.get('rating', '')}",
              f"- **Interviewer's recommendation:** {o.get('recommendation', '')}",
              f"- **Applicable category:** {o.get('category', '')}"]
    return "\n".join(lines) + "\n"


def report_md(meta, profile, final, exhibits, decision, recordings=None):
    """meta: {subject, extra, started, closed}; final: the final analysis dict."""
    p = profile
    out = [f"# {p['name']}: {meta.get('subject') or 'Untitled'}", ""]
    out.append(f"- **Date:** {meta.get('started', '')}")
    if meta.get("extra"):
        out.append(f"- **{p['extra']['label']}:** {meta['extra']}")
    if decision:
        label = next((d["short"] for d in p.get("decisions", []) if d["id"] == decision), decision)
        out.append(f"- **Decision:** {label}")
    if p.get("rubric") and final.get("weighted_score") is not None:
        out.append(f"- **Weighted score:** {final['weighted_score']:.2f} / 5")
    rec = final.get("recommendation")
    if rec:
        out.append(f"- **AI recommendation:** {rec.get('decision')} - {rec.get('reason', '')}")
    out += ["", "## Summary", final.get("summary", ""), ""]
    if p.get("rubric") and final.get("areas"):
        names = {a["id"]: a for a in p["rubric"]["areas"]}
        out += ["## Scores", "", "| Area | Weight | Score | Evidence |", "|---|---|---|---|"]
        for a in final["areas"]:
            info = names.get(a["id"], {"name": a["id"], "weight": ""})
            out.append(f"| {info['name']} | {info['weight']} | {a.get('score') or '-'} | {a.get('evidence', '')} |")
        out.append("")
    done = [e for e in exhibits if e.get("status") == "done"]
    if done:
        out.append("## Screen exhibits")
        for i, e in enumerate(done, 1):
            a = e["analysis"]
            out += [f"### Exhibit {i}: {a.get('title', '')} ({a.get('rating', '')})",
                    f"- Asked: {e.get('question', '')}", f"- Did/said: {e.get('note', '')}",
                    f"- What it does: {a.get('does', '')}",
                    f"- Issues: {'; '.join(a.get('issues', [])) or 'none'}",
                    f"- Feedback: {a.get('feedback', '')}", f"- Better approach: {a.get('better', '')}", ""]
    for sec in p["final_sections"]:
        out += [section_md(sec, final.get(sec["id"])), ""]
    if final.get("form_answers"):
        out += ["## " + p["form"]["title"], "", form_md(final["form_answers"], p["form"]), ""]
    if recordings:
        out.append("## Recordings")
        out += [f"- {r['name']}: {r.get('duration_text', '')} ({r.get('size_text', '')})" for r in recordings]
    return "\n".join(out).rstrip() + "\n"


def day_summary(sessions, date):
    """sessions: list of dicts {time, name, type, decision, score, feedback}. Returns (csv_text, md_text)."""
    rows = [s for s in sessions if s["date"] == date]
    rows.sort(key=lambda r: r["time"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "name", "type", "decision", "score", "feedback"])
    for r in rows:
        w.writerow([r["time"], r["name"], r["type"], r["decision"], r["score"], r["feedback"]])
    md = [f"# Day summary {date}", "", "| Time | Name | Type | Decision | Score | Feedback |", "|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['time']} | {r['name']} | {r['type']} | {r['decision']} | {r['score']} | {r['feedback']} |")
    return buf.getvalue(), "\n".join(md) + "\n"


def to_json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)
