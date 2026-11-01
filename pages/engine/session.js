// One listening session in the browser: the same state machine and behaviour as the desktop and server editions
// (desktop/backend/session.py, server/backend/app/engine/session.py), with the same method names so a test can compare them:
// live -> stopping -> finalizing -> review -> decided. Everything lives on this device (IndexedDB); the only network traffic
// is the visitor's own analysis calls to the Anthropic API.
import * as A from "./analyzer.js";
import * as R from "./reports.js";

export const settings = {
  ANALYZE_INTERVAL: 60, ANALYZE_MIN_GAP: 30, ANALYZE_NEW_LINES: 4, REQUIRE_CONSENT: true, MIN_FREE_MB: 200,
  corrections: true, demo_delay: 2000, time_scale: 1,           // time_scale < 1 speeds every wait up (tests)
  sample_text: (profile_id) => "",                               // the sample call replayed in demo mode
  asr_status: () => "idle",                                      // "idle" | "loading" | "ready" | "error"
  asr_error: () => "",
  make_speaker_book: null,                                       // () => SpeakerBook (audio.js); null = voices are not told apart
  JSZip: null,                                                   // zip library (loaded by the page when exporting)
  free_mb: async () => Infinity,
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms * settings.time_scale));
const pad = (n) => String(n).padStart(2, "0");
const local_date = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const now_iso = () => { const d = new Date(); return `${local_date(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
export const human_size = (n) => { for (const u of ["B", "KB", "MB", "GB"]) { if (n < 1024 || u === "GB") return u === "B" ? `${Math.round(n)} ${u}` : `${n.toFixed(1)} ${u}`; n /= 1024; } };
export const human_dur = (sec) => { sec = Math.trunc(sec); return sec >= 3600 ? `${Math.floor(sec / 3600)}:${pad(Math.floor((sec % 3600) / 60))}:${pad(sec % 60)}` : `${Math.floor(sec / 60)}:${pad(sec % 60)}`; };
const words = (text) => ((text || "").toLowerCase().match(/[a-z0-9']+/g) || []);
const b64_blob = (b64, type = "image/png") => { const bin = atob(b64); const u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return new Blob([u], { type }); };
const blob_b64 = async (blob) => { const u = new Uint8Array(await blob.arrayBuffer()); let s = ""; for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode(...u.subarray(i, i + 0x8000)); return btoa(s); };
const deep = (x) => JSON.parse(JSON.stringify(x));

/** All sessions on this device (the browser edition is single-user) and the storage behind them. */
export class SessionStore {
  constructor(store, profiles) { this.store = store; this.profiles = profiles; this.sessions = new Map(); }
  async load_all() {
    for (const rec of await this.store.load_all()) {
      try { const s = await Session.load(this, rec, this.profiles); if (s) this.sessions.set(s.sid, s); } catch (e) { console.warn("skipping a saved session", e); }
    }
    await this.cleanup_recordings();
  }
  new_sid(date, base) {
    const n = 1 + [...this.sessions.keys()].filter((k) => k.startsWith(date + "_")).length;
    let sid = `${date}_${pad(n)}_${base}`;
    while (this.sessions.has(sid)) sid += "x";
    return sid;
  }
  live() { return [...this.sessions.values()].find((s) => s.state === "live") || null; }
  day_summary_csv(date) {
    const rows = [...this.sessions.values()].map((s) => ({ date: s.date, time: s.time, name: s.name, type: s.profile.name, decision: s.decision,
      score: (s.final || {}).weighted_score || "", feedback: (s.feedback || {}).state || "" }));
    return R.day_summary(rows, date)[0];
  }
  async cleanup_recordings(days = 30) {                    // whole recordings older than 30 days go away (storage in a browser is limited)
    const cutoff = Date.now() - days * 86400000;
    for (const s of this.sessions.values()) {
      for (const c of [...s.clips]) if (c.state === "saved" && Date.parse(c.ended || c.started || "") < cutoff) await s.delete_clip(c.n, true);
    }
  }
}

export class Session {
  constructor(store, profile, { subject = "", extra = "", context = [], sid = null, role = "", speakers = false } = {}) {
    this.store = store;
    this.profile = deep(profile);                          // the topic list is per session (built for the role, grows during the call)
    this.role = (role || "").trim();
    this.rubric_building = false; this.rubric_note = "";
    this.speakers_on = !!speakers; this.speaker_names = {}; this.speaker_book = null;
    this.subject = subject.trim(); this.extra = extra; this.context = [...context];
    this.state = "live"; this.rev = 1;
    const now = new Date();
    this.date = local_date(now); this.time = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
    this.started = now_iso(); this.started_wall = Date.now() / 1000;
    this.closed = ""; this.consent = null; this.dismissed = false;
    this.sid = sid || store.new_sid(this.date, R.slug(this.subject || this.profile.id));
    this.lines = []; this.vcount = 0; this.notes = "";
    this.exhibits = []; this.chat = []; this.events = []; this.clips = [];
    this.analysis = null; this.final = null; this.drafts = {}; this.form_data = null;
    this.decision = ""; this.result_date = ""; this.feedback = { state: "" };
    this.error = ""; this.analysis_error = "";
    this.active = 0; this.seg_start = Date.now() / 1000;
    this.paused = false; this.listening = true;
    this.watch = { on: false, interval: 30, kept: 0, was_on: false, status: "" };
    this.source = null; this.partials = {};
    this.force = false; this.last_analyze = 0; this.lines_at_last = 0;
    this.analyzing = false; this.chat_busy = false; this.form_busy = false; this.regenerating = false;
    this.stopped = false; this._timer = null; this._save_timer = null; this._chunks = {};
    this.audio_clients = 0; this.levels = { you: 0, other: 0 }; this.sources = []; this.recent_other = [];
    this.blobs = new Map();                                // exhibit images and recordings kept in memory for this tab
    this.urls = new Map();
  }

  get name() { return this.subject || "Untitled"; }
  elapsed() { return this.state === "live" && !this.paused ? this.active + (Date.now() / 1000 - this.seg_start) : this.active; }

  event(text) {
    const d = new Date();
    this.events.push({ ts: `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`, text });
    this.touch();
  }
  touch(save = true) { this.rev += 1; if (save) this.save(); }

  /* ---------------------------------------------------------------- transcript */
  add_line(label, text, ts, marker = false, speaker = null) {
    this.vcount += 1;
    const line = { id: this.lines.length, label, text, ts: Math.round(ts * 10) / 10, marker, v: this.vcount, checked: marker, speaker };
    this.lines.push(line);
    this.touch(false); this.save();
    return line.id;
  }
  override_line(line_id, text, kind) {
    if (!(line_id >= 0 && line_id < this.lines.length) || this.lines[line_id].text === text) return;
    this.vcount += 1;
    const ln = this.lines[line_id];
    ln.text = text; ln.v = this.vcount; ln[kind] = true;
    this.save();
  }
  is_echo(text) {                                          // speaker sound leaking into the microphone
    const mine = new Set(words(text));
    if (mine.size < 3) return false;
    const now = Date.now() / 1000;
    return this.recent_other.some(([t, ws]) => {
      if (!(now - t < 15 && ws.size)) return false;
      let hit = 0; for (const w of mine) if (ws.has(w)) hit++;
      return hit / mine.size >= 0.7;
    });
  }
  on_utterance(label, text, ts, speaker = null) {
    if (this.state !== "live") return null;
    if (label === "other") { this.recent_other.push([Date.now() / 1000, new Set(words(text))]); if (this.recent_other.length > 12) this.recent_other.shift(); }
    else if (this.sources.some((s) => s.role === "other") && this.is_echo(text)) return null;
    return this.add_line(label, text, ts, false, speaker);
  }
  async _speaker_of(role, audio) {
    if (role !== "other" || !this.speakers_on) return null;
    try {
      if (!this.speaker_book) { if (!settings.make_speaker_book) throw new Error("no speaker model in this browser"); this.speaker_book = await settings.make_speaker_book(); }
      return await this.speaker_book.assign(audio);
    } catch (e) {                                          // never lose the words because the speaker model is missing
      this.speakers_on = false; this.event(`Speakers are not told apart: ${e.message}`); return null;
    }
  }
  speaker_info() {
    const ids = new Set(this.lines.filter((ln) => ln.label === "other" && ln.speaker !== null && ln.speaker !== undefined).map((ln) => ln.speaker));
    return { names: { ...this.speaker_names }, multi: ids.size >= 2 };
  }
  set_speaker_name(speaker_id, name) {
    name = (name || "").trim().slice(0, 40);
    if (name) this.speaker_names[String(parseInt(speaker_id))] = name; else delete this.speaker_names[String(parseInt(speaker_id))];
    this.touch();
  }
  on_partial(key, label, text) { if (text === null || text === undefined) delete this.partials[key]; else this.partials[key] = { label, text }; }
  marker(text) { this.add_line("", text, Date.now() / 1000 - this.started_wall, true); }

  /* ---------------------------------------------------------------- start */
  start() {
    this.event("Session started");
    if (this.role && this.profile.rubric) { this.rubric_building = true; this._build_rubric(); }
    if (A.cfg.MOCK_ANALYZER) {                             // demo: replay a sample call like the desktop demo mode
      this.sources = [{ role: "other", name: "Demo (sample transcript)" }]; this.audio_clients = 1;
      this._demo_feed();
    }
    this._timer = setInterval(() => this._loop(), 1000 * settings.time_scale);
    this.touch();
  }
  async _demo_feed() {
    const rows = (settings.sample_text(this.profile.id) || "").split("\n").filter((r) => r.includes(":"));
    for (const row of rows) {
      await sleep(settings.demo_delay);
      if (this.stopped) return;
      while (this.paused || !this.listening) { await sleep(300); if (this.stopped) return; }
      const i = row.indexOf(":"), who = row.slice(0, i).trim().toLowerCase(), text = row.slice(i + 1).trim();
      this.on_utterance(["interviewer", "you", "me"].includes(who) ? "you" : "other", text, Date.now() / 1000 - this.started_wall);
    }
  }
  async _build_rubric() {
    try {
      const res = await A.generate_rubric(this.profile, this.role, this.extra, this.context.join("\n"));
      this.profile.rubric.role = res.role; this.profile.rubric.areas = res.areas;
      this.profile.vocabulary = [res.role, ...res.areas.map((x) => x.name)].join(", ");
      this.event(`Topic list built for ${res.role} (${res.areas.length} topics)`);
    } catch (e) {
      this.rubric_note = `Could not build a topic list for this role (${e.message}); using the default list.`; this.event(this.rubric_note);
    } finally { this.rubric_building = false; this.touch(); }
  }

  /* ---------------------------------------------------------------- live loops */
  ctx(previous = true) {
    return { profile: this.profile, subject: this.subject, extra: this.extra, speakers: this.speaker_info(),
      lines: this.lines.map((l) => ({ ...l })), notes: this.notes, context: [...this.context],
      exhibits: this.exhibits.map((e) => ({ ...e })), previous: previous ? this.analysis : null };
  }
  _loop() {
    if (this.stopped) { clearInterval(this._timer); return; }
    if (this.state !== "live" || this.paused || this.rubric_building) return;
    this._check_disk();
    const n = this.lines.filter((l) => !l.marker).length, fresh = n - this.lines_at_last, gap = Date.now() / 1000 - this.last_analyze;
    const due = (gap >= settings.ANALYZE_INTERVAL && fresh > 0) || (fresh >= settings.ANALYZE_NEW_LINES && gap >= settings.ANALYZE_MIN_GAP);
    if ((this.force || due) && n && !this.analyzing) { this.force = false; this.run_live_analysis(); }
    if (Date.now() / 1000 - (this._last_correct || (this._last_correct = Date.now() / 1000)) > 25 && !A.cfg.MOCK_ANALYZER && settings.corrections) {
      this._last_correct = Date.now() / 1000; this.run_corrections();
    }
  }
  async run_live_analysis() {
    this.analyzing = true;
    try {
      const ctx = this.ctx(), n = ctx.lines.filter((l) => !l.marker).length;
      const res = await A.live_analysis(ctx);
      this.analysis = res; this.analysis_error = ""; this.lines_at_last = n; this.last_analyze = Date.now() / 1000;
    } catch (e) {
      this.analysis_error = e instanceof A.AnalyzerError ? e.message : `Analysis failed: ${e.message}`; this.last_analyze = Date.now() / 1000;
    } finally { this.analyzing = false; this.touch(); }
  }
  async run_corrections() {
    const batch = this.lines.filter((l) => !l.checked && !l.marker).slice(-15);
    if (!batch.length) return;
    const context = [this.subject, ...this.context].join("; ").slice(0, 1500);
    let fixes;
    try { fixes = await A.correct_lines(this.profile, batch.map((l) => ({ id: l.id, label: l.label, text: l.text })), context); } catch (e) { return; }
    for (const fx of fixes) if (Number.isInteger(fx.id)) this.override_line(fx.id, fx.text || "", "fixed");
    for (const l of batch) l.checked = true;
  }
  async _check_disk() {
    if (!this.clips.some((c) => ["recording", "paused"].includes(c.state))) return;
    const free = await settings.free_mb();
    if (free < settings.MIN_FREE_MB) { this.event(`Recording stopped automatically: only ${Math.round(free)} MB of browser storage left`); this.record_stop(); }
  }

  /* ---------------------------------------------------------------- pause / listening */
  set_paused(on) {
    if (this.state !== "live" || on === this.paused) return;
    this.paused = on;
    if (on) { this.active += Date.now() / 1000 - this.seg_start; this.watch.was_on = this.watch.on; this.watch.on = false; }
    else { this.seg_start = Date.now() / 1000; this.watch.on = this.watch.was_on; }
    this.marker(on ? "Session paused" : "Session resumed");
    this._clip_modes();
    this.event(on ? "Session paused" : "Session resumed");
  }
  set_listening(on) {
    if (this.state !== "live" || on === this.listening) return;
    this.listening = on;
    this.marker(on ? "Listening resumed" : "Listening paused");
    this.event(on ? "Listening on" : "Listening off");
  }

  /* ---------------------------------------------------------------- end / finalize */
  end() {
    if (this.state !== "live") return;
    if (this.paused) this.paused = false; else this.active += Date.now() / 1000 - this.seg_start;
    this.state = "stopping"; this.watch.on = false;
    this.touch();
    this._stop_and_finalize();
  }
  async _stop_and_finalize() {
    await sleep(1000);                                     // let the last lines arrive
    for (let i = 0; i < 240 && this.clips.some((c) => c.state === "saving"); i++) await sleep(500);
    if (this.clips.some((c) => ["recording", "paused"].includes(c.state))) this.record_stop();
    this.stopped = true; clearInterval(this._timer);
    this.closed = now_iso();
    this.event("Session ended");
    await this._finalize();
  }
  async _finalize() {
    this.state = "finalizing"; this.error = "";
    this.touch();
    for (let i = 0; i < 180 && (this.rubric_building || this.exhibits.some((e) => e.status === "analyzing")); i++) await sleep(500);
    try {
      const n = this.lines.filter((l) => !l.marker).length;
      const out = n === 0 && !this.exhibits.length ? this._empty_final() : await A.final_analysis(this.ctx(false));
      this._set_final(out);
    } catch (e) { this.error = e instanceof A.AnalyzerError ? e.message : `Final analysis failed: ${e.message}`; }
    this.state = "review";
    this.touch();
  }
  _empty_final() {
    const schema = A.final_schema(this.profile), out = {};
    for (const [k, v] of Object.entries(schema.properties)) out[k] = v.type === "array" ? [] : v.type === "string" ? "" : null;
    out.summary = "No conversation was captured in this session.";
    return out;
  }
  _set_final(out) {
    this.final = out; this.form_data = out.form_answers || null;
    this.drafts = {}; for (const d of this.profile.drafts || []) this.drafts[d.id] = out[d.id] || "";
    this.write_outputs();
  }
  regenerate() {
    if (this.state !== "review" || this.regenerating) return false;
    this.regenerating = true; this._regen();
    return true;
  }
  async _regen() { this.state = "finalizing"; this.touch(false); await this._finalize(); this.regenerating = false; }

  /* ---------------------------------------------------------------- decisions and files */
  decide(decision_id) {
    const dec = (this.profile.decisions || []).find((d) => d.id === decision_id);
    if (!dec || !this.final) return false;
    if (!this.result_date) this.result_date = this.date;
    this.decision = decision_id; this.state = "decided";
    const form = this.profile.form;
    if (this.form_data && form) { const cat = form.category_for[decision_id]; if (cat) { this.form_data.overall = this.form_data.overall || {}; this.form_data.overall.category = cat; } }
    if (form && this.feedback.state === "") this.feedback = { state: "pending" };
    this.event(`Decision: ${dec.short}`);
    this.write_outputs(); this.touch();
    return true;
  }
  finish() {
    if (!this.final || (this.profile.decisions || []).length) return false;
    this.decision = "saved"; this.state = "decided"; this.result_date = this.result_date || this.date;
    this.write_outputs(); this.touch();
    return true;
  }
  _draft_files() {
    const keep = {}, decs = Object.fromEntries((this.profile.decisions || []).map((d) => [d.id, d]));
    const chosen = (decs[this.decision] || {}).draft;
    for (const d of this.profile.drafts || []) if (d.always || d.id === chosen || (!Object.keys(decs).length && this.state === "decided")) keep[d.id] = d;
    return keep;
  }
  /** The files this session would have on disk in the other editions (nothing is written: they are built when needed). */
  virtual_files() {
    const f = new Map(), P = this.profile;
    f.set("session.json", R.to_json(this.meta()));
    f.set("notes.txt", this.notes);
    f.set("context.json", R.to_json(this.context)); f.set("chat.json", R.to_json(this.chat)); f.set("events.json", R.to_json(this.events));
    f.set("clips.json", R.to_json(this.clips.map((c) => { const { got, ...rest } = c; return rest; })));
    f.set("feedback.json", R.to_json(this.feedback)); f.set("exhibits.json", R.to_json(this.exhibits));
    if (this.analysis) f.set("analysis.json", R.to_json(this.analysis));
    if (Object.keys(this.drafts).length) f.set("drafts.json", R.to_json(this.drafts));
    const spk = this.speaker_info(), rows = [];
    for (const ln of this.lines) {
      if (ln.marker) rows.push(`--- ${ln.text} ---`);
      else {
        let who = ln.label === "you" ? P.you_label : P.other_label;
        if (ln.label === "other" && spk.multi && ln.speaker !== null && ln.speaker !== undefined) who += ` (${spk.names[String(ln.speaker)] || "Speaker " + ln.speaker})`;
        rows.push(`[${A.fmt_ts(ln.ts)}] ${who}: ${ln.text}`);
      }
    }
    f.set("transcript.txt", rows.join("\n"));
    if (this.final) {
      f.set("evaluation.json", R.to_json(this.final));
      const keep = this.state === "decided" ? this._draft_files() : Object.fromEntries((P.drafts || []).filter((d) => d.always).map((d) => [d.id, d]));
      for (const d of P.drafts || []) if (keep[d.id]) { let text = this.drafts[d.id] || ""; if (d.id === "topic_feedback" && !text) text = R.topics_md(this.final.topics); f.set(d.filename, text); }
      if (this.form_data) f.set("evaluation-form.md", R.form_md(this.form_data, P.form));
      f.set("report.md", R.report_md(this.meta(), P, this.final, this.exhibits, this.decision, this.clips_public()));
      if (this.state === "decided") f.set("result.json", R.to_json({ decision: this.decision, date: this.result_date, subject: this.subject, score: this.final.weighted_score, profile: P.id }));
    }
    return f;
  }
  write_outputs() { this.save(); }                         // the files are built from the saved state (virtual_files), nothing else to write
  set_feedback(state) {
    if (!["pending", "filled", "submitted"].includes(state) || this.state !== "decided" || !this.profile.form) return false;
    this.feedback = { state, updated: now_iso() };
    this.touch();
    return true;
  }

  /* ---------------------------------------------------------------- persistence */
  meta() {
    return { id: this.sid, profile: this.profile.id, subject: this.subject, extra: this.extra, started: this.started, closed: this.closed, consent: this.consent,
      date: this.date, time: this.time, active: Math.round(this.active * 10) / 10, dismissed: this.dismissed, decision: this.decision, result_date: this.result_date,
      state: this.state, error: this.error, watch_interval: this.watch.interval, role: this.role, rubric: this.profile.rubric || null, rubric_note: this.rubric_note,
      speakers_on: this.speakers_on, speaker_names: this.speaker_names };
  }
  record() {
    return { ...this.meta(), notes: this.notes, context: this.context, chat: this.chat, events: this.events, feedback: this.feedback,
      clips: this.clips.map((c) => { const { got, ...rest } = c; return rest; }), exhibits: this.exhibits, analysis: this.analysis, drafts: this.drafts,
      final: this.final, lines: this.lines, vocabulary: this.profile.vocabulary };
  }
  save() {
    clearTimeout(this._save_timer);
    this._save_timer = setTimeout(() => { this.store.store.put_session(this.sid, this.record()).catch(() => {}); }, settings.time_scale < 1 ? 0 : 400);
  }
  async flush() { clearTimeout(this._save_timer); await this.store.store.put_session(this.sid, this.record()); }
  static async load(store, rec, profiles) {
    const profile = profiles[rec.profile];
    if (!profile) return null;
    const s = new Session(store, profile, { subject: rec.subject || "", extra: rec.extra || "", context: rec.context || [], sid: rec.id, role: rec.role || "" });
    if (rec.rubric && s.profile.rubric) s.profile.rubric = rec.rubric;
    if (rec.vocabulary) s.profile.vocabulary = rec.vocabulary;
    s.rubric_note = rec.rubric_note || "";
    s.speakers_on = !!rec.speakers_on; s.speaker_names = rec.speaker_names || {};
    s.date = rec.date || ""; s.time = rec.time || ""; s.started = rec.started || ""; s.closed = rec.closed || "";
    s.consent = rec.consent || null; s.dismissed = !!rec.dismissed;
    s.active = rec.active || 0; s.decision = rec.decision || ""; s.result_date = rec.result_date || "";
    s.watch.interval = rec.watch_interval || 30;
    s.notes = rec.notes || ""; s.chat = rec.chat || []; s.events = rec.events || []; s.clips = rec.clips || [];
    s.feedback = rec.feedback || { state: "" }; s.exhibits = rec.exhibits || [];
    s.analysis = rec.analysis || null; s.drafts = rec.drafts || {}; s.final = rec.final || null;
    s.form_data = (s.final || {}).form_answers || null;
    s.lines = rec.lines || []; s.vcount = s.lines.reduce((m, l) => Math.max(m, l.v || 0), 0);
    for (const c of s.clips) if (["recording", "paused", "saving"].includes(c.state)) { c.state = c.audio_file || c.video_file ? "saved" : "error"; c.error = c.error || ""; }
    for (const e of s.exhibits) if (e.status === "analyzing") { e.status = "error"; e.error = "Interrupted. Press Retry."; }
    for (const e of s.exhibits) { const b = await store.store.get_blob(`${s.sid}/${e.file}`); if (b) s.blobs.set(e.file, b); }
    for (const c of s.clips) for (const k of ["audio_file", "video_file"]) if (c[k]) { const b = await store.store.get_blob(`${s.sid}/rec/${c[k]}`); if (b) s.blobs.set("rec/" + c[k], b); }
    s.state = rec.state === "decided" && s.final ? "decided" : "review";
    s.stopped = true; s.paused = false;
    if (s.state === "review" && !s.final) s.error = s.error || "No summary yet. Press Retry.";
    return s;
  }

  /* ---------------------------------------------------------------- notes / context */
  set_notes(text) { this.notes = text; this.touch(); }
  add_context(text) { text = text.trim(); if (text && !this.context.includes(text)) { this.context.push(text); this.touch(); } }
  remove_context(idx) { if (idx >= 0 && idx < this.context.length) { this.context.splice(idx, 1); this.touch(); } }

  /* ---------------------------------------------------------------- exhibits */
  add_exhibit(png_b64, question = "", note = "", auto = false) {
    const n = this.exhibits.length + 1;
    const ex = { n, status: "analyzing", question, note, auto, ts: Math.round((Date.now() / 1000 - this.started_wall) * 10) / 10, file: `exhibits/${pad(n)}.png`, analysis: {}, error: "" };
    this.exhibits.push(ex);
    if (auto) this.watch.kept += 1;
    const blob = b64_blob(png_b64);
    this.blobs.set(ex.file, blob); this.store.store.put_blob(`${this.sid}/${ex.file}`, blob).catch(() => {});
    this.touch();
    this._analyze_exhibit(ex, png_b64);
    return ex;
  }
  async _analyze_exhibit(ex, png_b64 = null) {
    try {
      const b64 = png_b64 || (await blob_b64(this.blobs.get(ex.file)));
      const res = await A.exhibit_analysis(this.ctx(false), b64, ex.question, ex.note);
      if (ex.auto && res.relevant === false) {            // silently dropped: "not relevant"
        this.exhibits = this.exhibits.filter((x) => x !== ex); this.watch.kept = Math.max(0, this.watch.kept - 1); await this._renumber_exhibits();
      } else { ex.analysis = res; ex.status = "done"; ex.error = ""; }
    } catch (e) { ex.status = "error"; ex.error = e.message; }
    this.touch();
  }
  async _renumber_exhibits() {                             // keep numbering contiguous after a drop
    const moved = new Map();
    this.exhibits.forEach((e, i) => { const nf = `exhibits/${pad(i + 1)}.png`; if (e.file !== nf) { moved.set(nf, this.blobs.get(e.file)); e.file = nf; } e.n = i + 1; });
    for (const [file, blob] of moved) { if (blob) { this.blobs.set(file, blob); await this.store.store.put_blob(`${this.sid}/${file}`, blob); } }
    const extra = `exhibits/${pad(this.exhibits.length + 1)}.png`;
    this.blobs.delete(extra); await this.store.store.del_blob(`${this.sid}/${extra}`);
  }
  exhibit_retry(n) {
    const ex = this.exhibits.find((e) => e.n === n);
    if (ex && ex.status === "error") { ex.status = "analyzing"; ex.error = ""; this.touch(); this._analyze_exhibit(ex); return true; }
    return false;
  }
  exhibit_delete(n) {
    const ex = this.exhibits.find((e) => e.n === n);
    if (!ex) return false;
    this.exhibits = this.exhibits.filter((e) => e !== ex);
    this.blobs.delete(ex.file);
    this._renumber_exhibits();
    this.touch();
    return true;
  }

  /* ---------------------------------------------------------------- screen watching (the page grabs the frames) */
  set_watch(on, source = null) {
    if (source !== null) this.source = source;
    this.watch.on = !!on && this.state === "live" && !this.paused; this.watch.status = "";
    this.event(on ? "Screen watching on" : "Screen watching off");
  }

  /* ---------------------------------------------------------------- recording (the page records, the files stay in this browser) */
  _clip(n) { return this.clips.find((c) => c.n === n); }
  async record_start(audio, video, consent = false) {
    if (this.state !== "live") return { error: "Recording only works inside a live session." };
    if (this.clips.some((c) => ["recording", "paused", "saving"].includes(c.state))) return { error: "Already recording." };
    if (!(audio || video)) return { error: "Choose Audio and/or Screen video in Options first." };
    if (settings.REQUIRE_CONSENT && !this.consent && !consent) return { need_consent: true };
    if (consent && !this.consent) this.consent = { confirmed: true, time: now_iso() };
    const free = await settings.free_mb();
    if (free < settings.MIN_FREE_MB) return { error: `Only ${Math.round(free)} MB of browser storage is free (limit ${settings.MIN_FREE_MB} MB).` };
    const n = Math.max(0, ...this.clips.map((c) => c.n)) + 1, base = `${this.sid}_rec${pad(n)}`;
    this.clips.push({ n, name: `Recording ${n}`, state: "recording", audio: !!audio, video: !!video, started: now_iso(), start_ts: Math.round((Date.now() / 1000 - this.started_wall) * 10) / 10,
      duration: 0, size: 0, audio_file: audio ? `${base}.audio.webm` : "", video_file: video ? `${base}.video.webm` : "",
      source: video ? (this.source || {}).label || "Shared screen" : "", error: "", got: [] });
    this._clip_modes();
    this.event(`Recording ${n} started (${audio ? "audio" : ""}${audio && video ? " + " : ""}${video ? "video" : ""})`);
    return { ok: true, n };
  }
  _clip_modes() {                                          // pause/resume the active clip together with the session
    for (const c of this.clips) if (["recording", "paused"].includes(c.state)) c.state = this.paused ? "paused" : "recording";
    this.touch(false);
  }
  record_stop() {
    const clip = this.clips.find((c) => ["recording", "paused"].includes(c.state));
    if (!clip) return false;
    clip.state = "saving"; clip.duration = Math.round((Date.now() / 1000 - this.started_wall - clip.start_ts) * 10) / 10;
    this.touch(false);
    this._upload_watchdog(clip);
    return true;
  }
  async _upload_watchdog(clip) {                           // the page hands over its recordings right after stopping; give up after 2 minutes
    for (let i = 0; i < 240 && clip.state === "saving"; i++) await sleep(500);
    if (clip.state === "saving") { clip.error = "The recording was not saved in time."; await this._finish_clip(clip); }
  }
  clip_upload_path(n, kind) { const c = this._clip(n); return c && ["saving", "recording", "paused"].includes(c.state) && ["audio", "video"].includes(kind) && c[kind + "_file"] ? c[kind + "_file"] : null; }
  async clip_uploaded(n, kind, blob, duration = null) {   // the page hands over one recorded file
    const name = this.clip_upload_path(n, kind), c = this._clip(n);
    if (!name) return false;
    this.blobs.set("rec/" + name, blob); await this.store.store.put_blob(`${this.sid}/rec/${name}`, blob);
    c.got = [...new Set([...(c.got || []), kind])].sort();
    if (duration) c.duration = Math.round(duration * 10) / 10;
    if (["audio", "video"].filter((k) => c[k]).every((k) => c.got.includes(k))) await this._finish_clip(c);
    return true;
  }
  async _finish_clip(clip) {
    const have = ["audio_file", "video_file"].filter((k) => clip[k] && this.blobs.has("rec/" + clip[k]));
    clip.state = have.length ? "saved" : "error";
    if (clip.state === "error" && !clip.error) clip.error = "No recording data was received.";
    clip.ended = now_iso();
    clip.size = have.reduce((t, k) => t + this.blobs.get("rec/" + clip[k]).size, 0);
    this.event(`Recording ${clip.n} saved (${human_size(clip.size)})`);
    this.touch();
  }
  _url(key) {
    if (!this.blobs.has(key)) return "";
    if (!this.urls.has(key)) this.urls.set(key, URL.createObjectURL(this.blobs.get(key)));
    return this.urls.get(key);
  }
  clips_public() {
    return this.clips.map((c) => {
      const { got, ...d } = c;
      d.duration_text = human_dur(c.duration || 0); d.size_text = human_size(c.size || 0); d.path = "";
      d.audio_url = c.audio && c.state === "saved" && c.audio_file ? this._url("rec/" + c.audio_file) : "";
      d.video_url = c.video && c.state === "saved" && c.video_file ? this._url("rec/" + c.video_file) : "";
      if (["recording", "paused"].includes(c.state)) d.duration_text = human_dur(Date.now() / 1000 - this.started_wall - c.start_ts);
      return d;
    });
  }
  clip_file(n, kind) { const c = this._clip(n); if (!c || c.state !== "saved") return null; const name = kind === "audio" ? c.audio_file : c.video_file; return name ? this.blobs.get("rec/" + name) || null : null; }
  async delete_clip(n = null, quiet = false) {
    const targets = this.clips.filter((c) => (n === null || c.n === n) && !["recording", "paused", "saving"].includes(c.state));
    for (const c of targets) {
      for (const k of ["audio_file", "video_file"]) if (c[k]) { this.blobs.delete("rec/" + c[k]); this.urls.delete("rec/" + c[k]); await this.store.store.del_blob(`${this.sid}/rec/${c[k]}`); }
      this.clips = this.clips.filter((x) => x !== c);
    }
    if (!quiet) this.event("Recordings deleted");
    return targets.length;
  }

  /* ---------------------------------------------------------------- chat */
  form_state_text() { return this.form_data ? R.form_md(this.form_data, this.profile.form) : ""; }
  chat_add(role, text, extra = {}) {
    const d = new Date();
    const msg = { id: this.chat.length + 1, role, text, ts: `${pad(d.getHours())}:${pad(d.getMinutes())}`, unread: role === "assistant", ...extra };
    this.chat.push(msg); this.touch();
    return msg;
  }
  chat_context(text) { this.chat_add("user", `Add context: ${text}`); this.add_context(text); this.chat_add("assistant", "Saved. I will use it in every later analysis.", { unread: false }); }
  chat_ask(text) { this.chat_add("user", text); this.chat_busy = true; this._chat_job(text); }
  async _chat_job(text) {
    const hist = this.chat.slice(0, -1).filter((m) => m.kind === undefined || m.kind === null).slice(-12).map((m) => ({ role: m.role, text: m.text }));
    let res;
    try { const ctx = this.ctx(); ctx.previous = this.final || this.analysis; res = await A.chat_answer(ctx, hist, text, this.form_state_text()); }
    catch (e) { this.chat_busy = false; this.chat_add("assistant", `Sorry, that failed: ${e.message}`, { error: true }); return; }
    this.chat_busy = false;
    if ((res.remember || "").trim()) this.add_context(res.remember);
    this.chat_add("assistant", res.reply || "");
    this._chat_action(res);
  }
  _chat_action(res) {
    const action = res.action || "none";
    if (["fill_form", "update_answer"].includes(action) && this.profile.form) {
      this.chat_add("assistant", "In the browser edition the portal is filled with the helper bookmark: open the evaluation form below, copy the answers " +
        "(or all fields) and use the Fill helper on the portal page. Nothing is submitted for you.");
    } else if (action === "stop_fill") this.chat_add("assistant", "There is no fill running in the browser edition.");
    else if (action === "list_pending") {
      const items = [...this.store.sessions.values()].filter((s) => s.state === "decided" && s.profile.form && s.feedback.state !== "submitted").map((s) => ({ sid: s.sid, name: s.name }));
      this.chat_add("assistant", items.length ? "Interviews still needing feedback:" : "Nothing is waiting for feedback.", { kind: "pending_list", items });
    }
  }
  chat_dismiss(msg_id = null) {
    for (const m of this.chat) { if (msg_id === null || msg_id === undefined) m.unread = false; else if (m.id === msg_id) { m.status = "dismissed"; m.unread = false; } }
    this.touch();
  }

  /* ---------------------------------------------------------------- evaluation form */
  write_form_answers() {
    if (!this.profile.form || !this.final) return false;
    this.form_busy = true; this.touch(false); this._form_job();
    return true;
  }
  async _form_job() {
    try {
      const ans = await A.form_answers(this.ctx(false), this.final);
      this.form_data = ans; this.final.form_answers = ans;
      const cat = this.profile.form.category_for[this.decision];
      if (cat) { ans.overall = ans.overall || {}; ans.overall.category = cat; }
      this.write_outputs();
    } catch (e) { this.event(`Form answers failed: ${e.message}`); }
    this.form_busy = false; this.touch();
  }

  /* ---------------------------------------------------------------- public views */
  list_item() {
    let rec = "";
    if (this.clips.some((c) => c.state === "recording")) rec = "rec"; else if (this.clips.some((c) => c.state === "paused")) rec = "paused";
    return { id: this.sid, name: this.name, type: this.profile.id, type_name: this.profile.name, state: this.state, date: this.date, time: this.time, rec, paused: this.paused,
      listening: this.listening, dec: this.decision, fb: this.profile.form && this.state === "decided" ? this.feedback.state || "" : "",
      err: !!this.error, needs_summary: this.state === "review" && !this.final, dismissed: this.dismissed, has_form: !!this.profile.form };
  }
  exhibits_public() { return this.exhibits.map((e) => ({ ...e, url: this._url(e.file) })); }
  live_block(since, first) {
    const lines = this.lines.filter((l) => l.v > since), parts = Object.values(this.partials), live = this.state === "live";
    const err = settings.asr_status() === "error" ? settings.asr_error() : "";
    const cap_info = { levels: live ? { ...this.levels } : { you: 0, other: 0 }, sources: [...this.sources], error: err, errors: [],
      models: A.cfg.MOCK_ANALYZER ? "demo" : settings.asr_status() === "loading" ? "loading" : settings.asr_status(), connected: this.audio_clients > 0 };
    const active = this.clips.find((c) => ["recording", "paused"].includes(c.state));
    const rec_dur = active ? Math.round((Date.now() / 1000 - this.started_wall - active.start_ts) * 10) / 10 : 0;
    return { rec_dur, lines, partials: parts, max_v: this.vcount, elapsed: Math.round(this.elapsed() * 10) / 10, state: this.state, paused: this.paused, listening: this.listening,
      capture: cap_info, watch: { ...this.watch }, source: this.source, analyzing: this.analyzing, analysis_error: this.analysis_error, chat_busy: this.chat_busy,
      form_busy: this.form_busy, fill: null, rec_active: !!active, rec_audio_only_paused: !this.listening && !!active };
  }
  detail_block() {
    const form = this.profile.form;
    return { id: this.sid, name: this.name, subject: this.subject, extra: this.extra, profile: this.profile.id, state: this.state, date: this.date, time: this.time,
      started: this.started, analysis: this.analysis, final: this.final, drafts: this.drafts, decision: this.decision, error: this.error, context: this.context,
      exhibits: this.exhibits_public(), clips: this.clips_public(), chat: this.chat, feedback: this.feedback, form_data: this.form_data,
      form_fields: R.form_fields(this.form_data, form), form_text: R.form_md(this.form_data, form), consent: this.consent,
      files: [...this.virtual_files().keys(), ...(this.exhibits.length ? ["exhibits"] : [])].sort(),
      draft_files: Object.fromEntries((this.profile.drafts || []).map((d) => [d.id, d.filename])),
      score: this._score_info(), rubric: this.rubric_public(), speakers: { on: this.speakers_on, names: { ...this.speaker_names } } };
  }
  rubric_public() {
    const r = this.profile.rubric;
    if (!r) return null;
    return { role: r.role || "", areas: r.areas.map((a) => ({ id: a.id, name: a.name, weight: a.weight, hint: a.hint, dynamic: a.dynamic, off_role: a.off_role })),
      building: this.rubric_building, note: this.rubric_note, custom: !!this.role };
  }
  _score_info() {
    if (!this.profile.rubric || !this.final) return null;
    const [score, n, share] = A.weighted_score(this.profile, this.final.areas);
    return { score, scored: n, total: this.profile.rubric.areas.length, share: Math.round(share * 100) / 100 };
  }
  async zip_bytes(with_recordings = false) {
    const Z = settings.JSZip;
    if (!Z) throw new Error("The zip library is not loaded.");
    const zip = new Z();
    for (const [name, text] of this.virtual_files()) zip.file(`${this.sid}/${name}`, text);
    for (const e of this.exhibits) { const b = this.blobs.get(e.file); if (b) zip.file(`${this.sid}/${e.file}`, b); }
    if (with_recordings) for (const c of this.clips) for (const k of ["audio_file", "video_file"]) { const b = c[k] && this.blobs.get("rec/" + c[k]); if (b) zip.file(`${this.sid}/recordings/${c[k]}`, b); }
    return zip.generateAsync({ type: "blob" });
  }
  async discard(delete_files) {
    if (this.state === "live") { this.stopped = true; clearInterval(this._timer); if (this.clips.some((c) => ["recording", "paused"].includes(c.state))) this.record_stop(); }
    this.dismissed = true;
    if (delete_files) { clearTimeout(this._save_timer); this.stopped = true; clearInterval(this._timer); this.store.sessions.delete(this.sid); await this.store.store.del_session(this.sid); }
    else this.touch();
  }
}
