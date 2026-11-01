// Browser port of shared/core/reports.py: report.md, the evaluation form text, topic-wise feedback and the day summary.
// pages/tests/golden.test.mjs checks the output against the Python code.

export function slug(text, dflt = "session") {
  const s = (text || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return s.slice(0, 40) || dflt;
}
const _bullets = (items) => (items && items.length ? items.map((i) => `- ${i}`).join("\n") : "_none_");

export function topics_md(topics) {
  if (!topics || !topics.length) return "_No technical topics were discussed._";
  return topics.map((t, i) =>
    `### ${i + 1}. ${t.topic ?? ""}  (${t.rating ?? ""})\n- **Asked:** ${t.asked ?? ""}\n- **Answered:** ${t.answered ?? ""}\n` +
    `- **Feedback:** ${t.feedback ?? ""}\n- **A stronger answer:** ${t.better_answer ?? ""}`).join("\n\n");
}

export function section_md(sec, value) {
  const title = `## ${sec.title}\n`, t = sec.type;
  if (t === "topics") return title + topics_md(value);
  if (t === "text") return title + (value || "_none_");
  if (t === "list") return title + _bullets(value);
  if (t === "flags") return title + _bullets((value || []).map((f) => `**${f.title ?? ""}** - ${f.detail ?? ""}`));
  if (t === "actions") return title + _bullets((value || []).map((a) => `${a.task ?? ""} (owner: ${a.owner || "-"}, due: ${a.due || "-"})`));
  return title;
}

export function form_fields(form_data, form) {
  if (!form_data) return [];
  const out = [];
  for (const b of form_data.behavioral || []) {
    out.push({ label: `${b.name} - proficiency`, value: b.proficiency ?? "" });
    out.push({ label: `${b.name} - evidence`, value: b.evidence ?? "" });
  }
  (form_data.technical || []).forEach((t, k) => {
    const i = k + 1;
    out.push({ label: `Technical competency ${i}`, value: t.competency ?? "" });
    out.push({ label: `Technical competency ${i} - proficiency`, value: t.proficiency ?? "" });
    for (const s of t.subcompetencies || []) {
      out.push({ label: `  ${s.name} - rating`, value: s.rating ?? "" });
      out.push({ label: `  ${s.name} - evidence`, value: s.evidence ?? "" });
    }
    out.push({ label: `Technical competency ${i} - comments`, value: t.comments ?? "" });
  });
  const o = form_data.overall || {};
  out.push({ label: "Areas to probe", value: o.areas_to_probe ?? "" });
  out.push({ label: "Overall feedback rating", value: o.rating ?? "" });
  out.push({ label: "Interviewer's recommendation", value: o.recommendation ?? "" });
  out.push({ label: "Applicable category", value: o.category ?? "" });
  return out;
}

export function form_md(form_data, form) {
  if (!form_data) return "";
  const title = (form || {}).title || "Evaluation form";
  const lines = [`# ${title}`, "", "## Behavioral competencies"];
  for (const b of form_data.behavioral || []) lines.push(`- **${b.name}**: ${b.proficiency ?? ""} - ${b.evidence ?? ""}`);
  (form_data.technical || []).forEach((t, k) => {
    lines.push("", `## Technical competency ${k + 1}: ${t.competency ?? ""} (${t.proficiency ?? ""})`);
    for (const s of t.subcompetencies || []) lines.push(`- ${s.name}: ${s.rating ?? ""} - ${s.evidence ?? ""}`);
    lines.push("", `**Comments:** ${t.comments ?? ""}`);
  });
  const o = form_data.overall || {};
  lines.push("", "## Overall", `- **Areas to probe:** ${o.areas_to_probe ?? ""}`, `- **Overall feedback rating:** ${o.rating ?? ""}`,
    `- **Interviewer's recommendation:** ${o.recommendation ?? ""}`, `- **Applicable category:** ${o.category ?? ""}`);
  return lines.join("\n") + "\n";
}

export function report_md(meta, profile, final, exhibits, decision, recordings = null) {
  const p = profile;
  const out = [`# ${p.name}: ${meta.subject || "Untitled"}`, ""];
  out.push(`- **Date:** ${meta.started || ""}`);
  if (meta.extra) out.push(`- **${p.extra.label}:** ${meta.extra}`);
  if (decision) {
    const d = (p.decisions || []).find((x) => x.id === decision);
    out.push(`- **Decision:** ${d ? d.short : decision}`);
  }
  if (p.rubric && final.weighted_score !== null && final.weighted_score !== undefined) out.push(`- **Weighted score:** ${Number(final.weighted_score).toFixed(2)} / 5`);
  const rec = final.recommendation;
  if (rec) out.push(`- **AI recommendation:** ${rec.decision} - ${rec.reason || ""}`);
  out.push("", "## Summary", final.summary || "", "");
  if (p.rubric && final.areas && final.areas.length) {
    const names = Object.fromEntries(p.rubric.areas.map((a) => [a.id, a]));
    out.push("## Scores", "", "| Area | Weight | Score | Evidence |", "|---|---|---|---|");
    for (const a of final.areas) {
      const info = names[a.id] || { name: a.id, weight: "" };
      out.push(`| ${info.name} | ${info.weight} | ${a.score || "-"} | ${a.evidence || ""} |`);
    }
    out.push("");
  }
  const done = exhibits.filter((e) => e.status === "done");
  if (done.length) {
    out.push("## Screen exhibits");
    done.forEach((e, i) => {
      const a = e.analysis;
      out.push(`### Exhibit ${i + 1}: ${a.title ?? ""} (${a.rating ?? ""})`, `- Asked: ${e.question ?? ""}`, `- Did/said: ${e.note ?? ""}`,
        `- What it does: ${a.does ?? ""}`, `- Issues: ${(a.issues || []).join("; ") || "none"}`,
        `- Feedback: ${a.feedback ?? ""}`, `- Better approach: ${a.better ?? ""}`, "");
    });
  }
  for (const sec of p.final_sections) out.push(section_md(sec, final[sec.id]), "");
  if (final.form_answers) out.push("## " + p.form.title, "", form_md(final.form_answers, p.form), "");
  if (recordings && recordings.length) {
    out.push("## Recordings");
    for (const r of recordings) out.push(`- ${r.name}: ${r.duration_text || ""} (${r.size_text || ""})`);
  }
  return out.join("\n").replace(/\s+$/, "") + "\n";
}

const csvCell = (v) => { const s = String(v ?? ""); return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
export function day_summary(sessions, date) {
  const rows = sessions.filter((s) => s.date === date).sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  const csv = [["time", "name", "type", "decision", "score", "feedback"]].concat(rows.map((r) => [r.time, r.name, r.type, r.decision, r.score, r.feedback]))
    .map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n";
  const md = [`# Day summary ${date}`, "", "| Time | Name | Type | Decision | Score | Feedback |", "|---|---|---|---|---|---|"]
    .concat(rows.map((r) => `| ${r.time} | ${r.name} | ${r.type} | ${r.decision} | ${r.score} | ${r.feedback} |`));
  return [csv, md.join("\n") + "\n"];
}

export const to_json = (obj) => JSON.stringify(obj, null, 2);
