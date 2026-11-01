// Browser port of shared/core/analyzer.py: the same schemas, prompts, scoring, dynamic topic lists, chat, form answers and
// mock analyzer. pages/tests/golden.test.mjs checks that every prompt and schema built here equals what the Python code builds.
// Calls go straight from this page to the Anthropic API with the visitor's own API key (nothing else is sent anywhere).

export const cfg = {
  MOCK_ANALYZER: false,                      // true: deterministic fake data (the demo mode, no API key needed)
  LIVE_MODEL: "claude-sonnet-5-5", FINAL_MODEL: "claude-opus-5-5", CORRECT_MODEL: "claude-sonnet-5-5",
  EXHIBIT_MODEL: "claude-sonnet-5-5", CHAT_MODEL: "claude-sonnet-5-5", FILL_MODEL: "claude-sonnet-5-5",
  apiKey: () => "",                          // replaced by the page: returns the visitor's key
};

export const ASR_NOTE = "ASR note: the transcript is machine speech recognition, so infer garbled technical " +
  "terms from context, but never invent facts that were not said.";
export const RATINGS = ["strong", "good", "mixed", "weak", "not enough evidence"];

export class AnalyzerError extends Error {}

/* ------------------------------------------------------------------ Python-compatible text (the prompts must match byte for byte) */
export function pyRepr(x) {
  if (x === null || x === undefined) return "None";
  if (x === true) return "True";
  if (x === false) return "False";
  if (typeof x === "number") return String(x);
  if (typeof x === "string") {
    const q = x.includes("'") && !x.includes('"') ? '"' : "'";
    return q + x.replace(/\\/g, "\\\\").replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t").split(q).join("\\" + q) + q;
  }
  if (Array.isArray(x)) return "[" + x.map(pyRepr).join(", ") + "]";
  return "{" + Object.entries(x).map(([k, v]) => pyRepr(k) + ": " + pyRepr(v)).join(", ") + "}";
}
export function pyJson(x) {              // json.dumps(x, ensure_ascii=False): ", " and ": " separators
  if (x === null || x === undefined) return "null";
  if (typeof x === "string") return JSON.stringify(x);
  if (typeof x === "number" || typeof x === "boolean") return String(x);
  if (Array.isArray(x)) return "[" + x.map(pyJson).join(", ") + "]";
  return "{" + Object.entries(x).map(([k, v]) => JSON.stringify(k) + ": " + pyJson(v)).join(", ") + "}";
}
const num = (x) => (Number.isInteger(x) ? String(x) : String(x));
const pyFloat = (x) => (Number.isInteger(x) ? x.toFixed(1) : String(x));          // 3 -> "3.0" like Python prints floats

/* ------------------------------------------------------------------ schemas */
const _str = () => ({ type: "string" });
const _arr = (items) => ({ type: "array", items });
const _obj = (props, required) => ({ type: "object", properties: props, required: required ?? Object.keys(props), additionalProperties: false });

export const TOPIC_ITEM = _obj({ topic: _str(), rating: { type: "string", enum: RATINGS }, asked: _str(), answered: _str(), feedback: _str(), better_answer: _str() });
export const FLAG_ITEM = _obj({ title: _str(), detail: _str() });
export const ACTION_ITEM = _obj({ owner: _str(), task: _str(), due: _str() });
export const AREA_ITEM = _obj({ id: _str(), covered: { type: "boolean" }, score: { type: ["integer", "null"], minimum: 1, maximum: 5 }, evidence: _str(), hint: _str() });
export const NEW_AREA_ITEM = _obj({ id: _str(), name: _str(), weight: { type: "number", minimum: 0.5, maximum: 3 }, covers: _str(), off_role: { type: "boolean" } });
export const RUBRIC_SCHEMA = _obj({
  role: _str(),
  areas: _arr(_obj({ id: _str(), name: _str(), weight: { type: "number", minimum: 0.5, maximum: 3 }, covers: _str(), hint: _str() })),
});
function _section_schema(sec) {
  const t = sec.type;
  if (t === "list") return _arr(_str());
  if (t === "text") return _str();
  if (t === "flags") return _arr(FLAG_ITEM);
  if (t === "actions") return _arr(ACTION_ITEM);
  if (t === "topics") return _arr(TOPIC_ITEM);
  throw new Error(`unknown section type ${t}`);
}
export const FORM_SCHEMA_PART = _obj({
  behavioral: _arr(_obj({ name: _str(), proficiency: _str(), evidence: _str() })),
  technical: _arr(_obj({ competency: _str(), proficiency: _str(), subcompetencies: _arr(_obj({ name: _str(), rating: _str(), evidence: _str() })), comments: _str() })),
  overall: _obj({ areas_to_probe: _str(), rating: _str(), recommendation: _str(), category: _str() }),
});
export function live_schema(profile) {
  const props = {};
  for (const s of profile.live_sections) props[s.id] = _section_schema(s);
  if (profile.rubric) { props.areas = _arr(AREA_ITEM); props.new_areas = _arr(NEW_AREA_ITEM); }
  return _obj(props);
}
export function final_schema(profile) {
  const props = {};
  for (const s of profile.final_sections) props[s.id] = _section_schema(s);
  props.summary = _str();
  if (profile.rubric) {
    props.areas = _arr(AREA_ITEM); props.new_areas = _arr(NEW_AREA_ITEM); props.weighted_score = { type: ["number", "null"] };
    const ids = (profile.decisions || []).map((d) => d.id).concat(["borderline"]);
    props.recommendation = _obj({ decision: { type: "string", enum: ids }, reason: _str() });
  }
  for (const d of profile.drafts || []) props[d.id] = _str();
  if (profile.form) props.form_answers = FORM_SCHEMA_PART;
  return _obj(props);
}
export const form_schema = () => _obj({ form_answers: FORM_SCHEMA_PART });
export const CORRECTIONS_SCHEMA = _obj({ corrections: _arr(_obj({ id: { type: "integer" }, text: _str() })) });
export const EXHIBIT_SCHEMA = _obj({
  title: _str(), content: _str(), does: _str(), issues: _arr(_str()), rating: { type: "string", enum: RATINGS },
  feedback: _str(), better: _str(), relevant: { type: "boolean" },
});
export const CHAT_SCHEMA = _obj({
  reply: _str(), remember: _str(),
  action: { type: "string", enum: ["none", "fill_form", "stop_fill", "update_answer", "list_pending"] },
  field: _str(), value: _str(),
});

/* ------------------------------------------------------------------ the model call */
let _backend = null;
export function set_backend(fn) { _backend = fn; }       // fn(prompt, system, schema, model, images) -> object (tests and demo use it)

async function _call(prompt, system, schema, model, images = null) {
  if (_backend) return _backend(prompt, system, schema, model, images);
  const key = cfg.apiKey();
  if (!key) throw new AnalyzerError("Add your Anthropic API key (Settings, top right) to analyse calls, or use the demo mode.");
  const content = [{ type: "text", text: prompt }].concat((images || []).map((b64) => ({ type: "image", source: { type: "base64", media_type: "image/png", data: b64 } })));
  let r;
  try {
    r = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01", "anthropic-dangerous-direct-browser-access": "true" },
      body: JSON.stringify({ model, max_tokens: 8000, system, messages: [{ role: "user", content }],
        tools: [{ name: "report", description: "Return the structured result", input_schema: schema }], tool_choice: { type: "tool", name: "report" } }),
    });
  } catch (e) { throw new AnalyzerError("Could not reach the Anthropic API: " + e.message); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new AnalyzerError(((j.error && j.error.message) || `Request failed (${r.status})`) + (r.status === 401 ? " Check your API key." : ""));
  const t = (j.content || []).find((c) => c.type === "tool_use");
  if (!t) throw new AnalyzerError("Claude did not return structured data. Try again.");
  return t.input;
}

/* ------------------------------------------------------------------ context text */
export function fmt_ts(sec) {
  sec = Math.trunc(sec || 0);
  return String(Math.floor(sec / 60)).padStart(2, "0") + ":" + String(sec % 60).padStart(2, "0");
}
export function transcript_text(profile, lines, limit_chars = 60000, speakers = null) {
  const rows = [];
  for (const ln of lines) {
    if (ln.marker) { rows.push(`--- ${ln.text} ---`); continue; }
    let who = ln.label === "you" ? profile.you_label : profile.other_label;
    const sp = ln.speaker;
    if (speakers && speakers.multi && ln.label === "other" && sp !== null && sp !== undefined) {
      who += ` (${(speakers.names || {})[String(sp)] || "Speaker " + sp})`;
    }
    rows.push(`[${fmt_ts(ln.ts)}] ${who}: ${ln.text}`);
  }
  const text = rows.join("\n");
  return text.slice(-limit_chars);
}
export function build_context(ctx) {
  const p = ctx.profile, parts = [];
  if (ctx.subject) parts.push(`${p.subject_label.toUpperCase()}: ${ctx.subject}`);
  if (p.extra && ctx.extra) parts.push(`${p.extra.label.toUpperCase()}: ${ctx.extra}`);
  if (ctx.context && ctx.context.length) parts.push("CONTEXT GIVEN BY THE USER (treat as facts):\n- " + ctx.context.join("\n- "));
  if (ctx.notes) parts.push("USER'S NOTES:\n" + ctx.notes);
  const ex = (ctx.exhibits || []).filter((e) => e.status === "done");
  if (ex.length) {
    const rows = ex.map((e, i) => {
      const a = e.analysis || {};
      return `${i + 1}. ${a.title ?? "Exhibit"} | asked: ${e.question ?? ""} | did/said: ${e.note ?? ""}\n` +
        `   content: ${(a.content ?? "").slice(0, 1500)}\n   what it does: ${a.does ?? ""}\n` +
        `   issues: ${(a.issues || []).join("; ")}\n   rating: ${a.rating ?? ""} | ${a.feedback ?? ""}`;
    });
    parts.push("SCREEN EXHIBITS (hard evidence; each is its own topic):\n" + rows.join("\n"));
  }
  const sp = ctx.speakers;
  if (sp && sp.multi) {
    parts.push(`SEVERAL PEOPLE ARE ON THE ${p.other_label.toUpperCase()} SIDE: the transcript tags each of their lines with who spoke, as ` +
      `"${p.other_label} (Speaker 2)" or by name when it is known. Voice recognition can make mistakes; keep each person's ` +
      "answers apart and say so when you are unsure who said something.");
  }
  parts.push("TRANSCRIPT:\n" + transcript_text(p, ctx.lines, 60000, sp));
  if (ctx.previous) parts.push("YOUR PREVIOUS ANALYSIS (update it, do not start from scratch):\n" + pyJson(ctx.previous).slice(0, 20000));
  return parts.join("\n\n");
}

export function rubric_text(profile) {
  const r = profile.rubric;
  if (!r) return "";
  const areas = r.areas.map((a) => `- id=${a.id} | ${a.name} (weight ${num(a.weight)}): ${a.covers}`).join("\n");
  return `RUBRIC for role '${r.role}'. Scale: ${r.scale}. Advance threshold: ${pyFloat(r.advance_threshold)}.\n` +
    "Return EVERY area below in `areas` (use its id): covered true/false, score 1-5 or null when not " +
    "asked/demonstrated, a one-line evidence, and for uncovered areas a concrete `hint` question fitted " +
    `to the candidate's level and background (empty string when covered).\n${areas}\n` +
    "DYNAMIC TOPICS: this list was built for the role and is not complete. When the conversation substantively " +
    "covers a topic that is NOT in the list (a related skill, or something unrelated to the role), add it to " +
    "`new_areas` (id: new snake_case id; name: 2-4 words; weight 0.5-3 by how much it matters for this role, " +
    "0.5-1 when off-role; covers: one line; off_role: true when it is not part of the role) and ALSO score it in " +
    "`areas` using the same id. A topic counts once the interviewer asked about it and the candidate answered, even in a single " +
    "exchange, so go through every question asked and check it fits a listed topic; if none fits, add it. Do not add " +
    "a topic that was only name-dropped, never duplicate or rename an existing topic, and leave " +
    "`new_areas` empty when nothing new was discussed.";
}

const _slug = (text) => ((text || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40)) || "topic";
export { _slug as slug_topic };

export function merge_new_areas(profile, out) {
  const r = profile.rubric;
  if (!r || !out || typeof out !== "object") return 0;
  const haveIds = new Set(r.areas.map((a) => a.id)), haveNames = new Set(r.areas.map((a) => a.name.trim().toLowerCase()));
  let added = 0;
  for (const n of out.new_areas || []) {
    const name = (n.name || "").trim(), aid = _slug(n.id || name);
    if (!name || haveIds.has(aid) || haveNames.has(name.toLowerCase())) continue;
    let w = parseFloat(n.weight || 1); if (Number.isNaN(w)) w = 1;
    w = Math.min(3, Math.max(0.5, w));
    r.areas.push({ id: aid, name, weight: Math.round(w * 2) / 2, covers: (n.covers || "").trim(), hint: "", dynamic: true, off_role: !!n.off_role });
    haveIds.add(aid); haveNames.add(name.toLowerCase()); added++;
  }
  return added;
}

export async function generate_rubric(profile, role, level = "", context = "") {
  if (cfg.MOCK_ANALYZER) {
    const words = role.split(/[^A-Za-z0-9+#.]+/).filter((w) => w.length > 2).slice(0, 6);
    const ws = words.length ? words : ["General"];
    const title = (w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
    return { role, areas: ws.map((w, i) => ({ id: _slug(w), name: title(w), weight: i < 2 ? 2 : 1, covers: `${w} fundamentals`, hint: `Walk me through how you have used ${w}.` })) };
  }
  const system = "You design interview rubrics. Given the role being hired for, list the 8-14 topics a strong interviewer " +
    "should cover, most important first. Each topic: id (snake_case), name (2-4 words), weight 0.5-3 " +
    "(3 = core must-have, 1 = nice to have), covers (one line of what it includes), hint (one concrete " +
    "question that fits the candidate level). Mix technical depth with design/problem-solving and, when " +
    "it matters for the role, communication or ownership. No duplicates. Use the job description or " +
    "context when given. The topic list is shown live to the interviewer, so keep names short.";
  const prompt = `ROLE: ${role}\nCANDIDATE LEVEL: ${level || "not given"}\nCONTEXT (job description / resume):\n${context || "(none)"}\n\n` +
    "Return role (a clean one-line title) and areas.";
  const out = await _call(prompt, system, RUBRIC_SCHEMA, cfg.LIVE_MODEL);
  const seen = new Set(), areas = [];
  for (const a of out.areas || []) {
    const name = (a.name || "").trim(), aid = _slug(a.id || a.name);
    if (!name || seen.has(aid)) continue;
    seen.add(aid);
    const w = Math.min(3, Math.max(0.5, parseFloat(a.weight || 1)));
    areas.push({ id: aid, name, weight: Math.round(w * 2) / 2, covers: a.covers || "", hint: a.hint || "" });
  }
  if (areas.length < 3) throw new AnalyzerError("The AI returned too few topics.");
  return { role: (out.role || role).trim(), areas: areas.slice(0, 16) };
}

const _sections_text = (sections) => sections.map((s) => `- ${s.id} (${s.type}): ${s.title}` + (s.detail ? ` - ${s.detail}` : "")).join("\n");

export function weighted_score(profile, areas) {
  const r = profile.rubric;
  if (!r) return [null, 0, 0.0];
  const weights = {}; for (const a of r.areas) weights[a.id] = a.weight;
  const total = Object.values(weights).reduce((x, y) => x + y, 0) || 1;
  let numr = 0, den = 0, n = 0;
  for (const a of areas || []) {
    const w = weights[a.id];
    if (w && a.score) { numr += w * a.score; den += w; n += 1; }
  }
  if (!den) return [null, 0, 0.0];
  return [Math.round((numr / den) * 100) / 100, n, den / total];
}

/* ------------------------------------------------------------------ analyses */
export async function live_analysis(ctx) {
  const p = ctx.profile;
  if (cfg.MOCK_ANALYZER) { const out = _mock(live_schema(p), ctx); _mock_extra(p, out, ctx); merge_new_areas(p, out); return out; }
  const system = `${p.system}\n\nThis is a LIVE analysis while the call is still running; the conversation is ` +
    `incomplete. Keep every field short and useful.\n${ASR_NOTE}\n\nFill these sections:\n` +
    `${_sections_text(p.live_sections)}\n\n${rubric_text(p)}`;
  const out = await _call(build_context(ctx), system, live_schema(p), cfg.LIVE_MODEL);
  merge_new_areas(p, out);
  return out;
}

export async function final_analysis(ctx) {
  const p = ctx.profile, schema = final_schema(p);
  let out;
  if (cfg.MOCK_ANALYZER) { out = _mock(schema, ctx); _mock_extra(p, out, ctx); }
  else {
    let extra = "";
    if (p.rubric) {
      extra = "weighted_score = weighted mean of the scored areas only. CONFIDENCE RULE: if the scored " +
        "areas cover under a third of the rubric weight, say so in the summary, use 'borderline' and " +
        "mention how many areas were assessed, and say so if it was mostly not an interview.\n";
    }
    const drafts = (p.drafts || []).map((d) => `- ${d.id}: ${d.detail ?? d.title}`).join("\n");
    let form = "";
    if (p.form) {
      const f = p.form;
      form = `\nEVALUATION FORM (${f.title}). ${f.instructions}\nproficiency options: ${pyRepr(f.proficiency_options)}\n` +
        `behavioral competencies (return all, in this order): ${pyRepr(f.behavioral)}\n` +
        `technical competencies: up to ${f.max_technical} (examples: ${pyRepr(f.technical_examples)}), each with ` +
        `proficiency, 1-6 subcompetencies {name, rating, evidence} (rating options ${pyRepr(f.rating_options)}), ` +
        "and 3-5 sentence `comments` in the interviewer's own voice.\n" +
        `overall: areas_to_probe, rating (from ${pyRepr(f.rating_options)}), recommendation (2-4 sentences), ` +
        `category (one of ${pyRepr(f.categories)}).\n`;
    }
    const system = `${p.system}\n\nThis is the FINAL analysis after the call ended. Be complete and fair.\n` +
      `${ASR_NOTE}\n${extra}\nSections:\n${_sections_text(p.final_sections)}\n` +
      `summary: 3-6 sentences.\nDrafts (one string field each, markdown):\n${drafts}\n${form}\n${rubric_text(p)}`;
    out = await _call(build_context(ctx), system, schema, cfg.FINAL_MODEL);
  }
  if (p.rubric) { merge_new_areas(p, out); out.weighted_score = weighted_score(p, out.areas)[0]; }
  return out;
}

export async function form_answers(ctx, final) {
  const p = ctx.profile;
  if (cfg.MOCK_ANALYZER) return _mock(form_schema(), ctx).form_answers;
  ctx = { ...ctx, previous: final };
  const f = p.form;
  const system = `${p.system}\n\nWrite ONLY the ${f.title} answers from the transcript, screen exhibits and the ` +
    `earlier analysis.\n${f.instructions}\nproficiency options: ${pyRepr(f.proficiency_options)}\n` +
    `behavioral (return all, in order): ${pyRepr(f.behavioral)}\nrating options: ${pyRepr(f.rating_options)}\n` +
    `technical: up to ${f.max_technical} competencies (examples ${pyRepr(f.technical_examples)}), each with ` +
    "1-6 subcompetencies and 3-5 sentence comments in the interviewer's voice.\n" +
    `overall.category is one of ${pyRepr(f.categories)}.\n${ASR_NOTE}`;
  return (await _call(build_context(ctx), system, form_schema(), cfg.FINAL_MODEL)).form_answers;
}

export async function correct_lines(profile, lines, context) {
  if (cfg.MOCK_ANALYZER) return [];
  const who = (l) => (l.label === "you" ? profile.you_label : profile.other_label);
  const rows = lines.map((l) => `${l.id}: ${who(l)}: ${l.text}`).join("\n");
  const system = "You fix speech-recognition errors in short transcript lines. Only correct words that are clearly " +
    "garbled technical terms (use the vocabulary and context). Return only lines you changed, with the " +
    `same id. Never add content. ${ASR_NOTE}\nVocabulary: ${profile.vocabulary || ""}`;
  const prompt = `Context:\n${context}\n\nLines:\n${rows}`;
  return ((await _call(prompt, system, CORRECTIONS_SCHEMA, cfg.CORRECT_MODEL)).corrections) || [];
}

export async function exhibit_analysis(ctx, png_b64, question, note) {
  const p = ctx.profile, ex = p.exhibit || {};
  if (cfg.MOCK_ANALYZER) {
    return { title: "Mock exhibit", content: "print('hello')", does: "Prints a greeting.", issues: [], rating: "good",
      feedback: "Mock feedback.", better: "Mock better approach.", relevant: true };
  }
  const system = `${p.system}\n\nYou are reviewing a screenshot taken during the call. ${ex.prompt || ""}\n` +
    "Return: title (short), content (verbatim text/code), does (what it does), issues (justified only), " +
    "rating, feedback, better (a better approach), relevant (false when the screen is NOT part of the call: " +
    "desktop, chat app, empty window, a video grid without shared content or unrelated browsing).";
  const prompt = `${build_context({ ...ctx, exhibits: [], previous: null })}\n\n` +
    `${ex.question_label || "What did you ask?"} ${question || "(not given)"}\n` +
    `${ex.note_label || "What did they do or say?"} ${note || "(not given)"}\n\nAnalyse the attached screenshot.`;
  return _call(prompt, system, EXHIBIT_SCHEMA, cfg.EXHIBIT_MODEL, [png_b64]);
}

export async function chat_answer(ctx, history, message, form_state = "") {
  const p = ctx.profile;
  if (cfg.MOCK_ANALYZER) return { reply: `(mock) You asked: ${message}`, remember: "", action: "none", field: "", value: "" };
  const actions = "action: fill_form (only if the user asks you to fill the evaluation form on the website), stop_fill, " +
    "update_answer (field + value: change one answer in the open form window), list_pending (list interviews " +
    "still needing feedback), otherwise none. The app carries actions out; never claim you did something.";
  const system = `${p.system}\n\nYou are the in-app assistant. Answer using the transcript, notes, exhibits, latest ` +
    "analysis and the FORM STATE below. Be brief. If the user states a fact worth remembering (e.g. 'he has 5 " +
    "years of experience'), put it in `remember` (a short statement) so it is added to the context. " +
    `${actions}\n${ASR_NOTE}`;
  const hist = history.slice(-12).map((m) => `${m.role.toUpperCase()}: ${m.text}`).join("\n");
  const prompt = `${build_context(ctx)}\n\nFORM STATE:\n${form_state || "(no form answers yet)"}\n\nCHAT SO FAR:\n${hist}\n\nUSER: ${message}`;
  return _call(prompt, system, CHAT_SCHEMA, cfg.CHAT_MODEL);
}

/* ------------------------------------------------------------------ mock analyzer (demo mode) */
function _mock(schema, ctx) {
  const nLines = (ctx.lines || []).filter((l) => !l.marker).length, p = ctx.profile;
  const gen = (sch, key = "") => {
    let t = sch.type;
    if (Array.isArray(t)) t = t.filter((x) => x !== "null")[0];
    if (sch.enum) return key !== "decision" ? sch.enum[Math.min(1, sch.enum.length - 1)] : sch.enum[0];
    if (t === "object") return Object.fromEntries(Object.entries(sch.properties).map(([k, v]) => [k, gen(v, k)]));
    if (t === "array") {
      if (key === "areas") return mock_areas(p, nLines);
      if (key === "new_areas") return [];
      if (key === "followups") return ["Mock question: how would you handle failure?"];
      return nLines ? [gen(sch.items, key)] : [];
    }
    if (t === "boolean") return true;
    if (t === "integer" || t === "number") return 3;
    return `Mock ${key || "text"} (based on ${nLines} lines)`;
  };
  return gen(schema);
}
function _mock_extra(profile, out, ctx) {
  const n = (ctx.lines || []).filter((l) => !l.marker).length;
  if (profile.rubric && n >= 6 && out.areas) {
    out.new_areas = [{ id: "kubernetes", name: "Kubernetes", weight: 1, covers: "pods, deployments", off_role: true }];
    if (!out.areas.some((a) => a.id === "kubernetes")) out.areas.push({ id: "kubernetes", covered: true, score: 4, evidence: "Mock: came up unprompted.", hint: "" });
  }
}
export function mock_areas(profile, nLines) {
  const areas = (profile.rubric || {}).areas || [], out = [];
  const covered = nLines ? Math.min(areas.length, Math.max(1, Math.floor(nLines / 2))) : 0;
  areas.forEach((a, i) => {
    const done = i < covered;
    out.push({ id: a.id, covered: done, score: done ? 3 + (i % 2) : null, evidence: done ? `Mock evidence for ${a.name}` : "", hint: done ? "" : a.hint || "" });
  });
  return out;
}
