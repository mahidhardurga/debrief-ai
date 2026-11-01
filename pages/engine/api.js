// The browser edition's "/api" : the same routes and JSON shapes as the desktop and server editions (so the shared UI,
// shared/ui/app.js, works unchanged), answered inside the page instead of by a server. install_fetch() makes the page's own
// fetch("/api/...") calls land here; everything else (the Anthropic API, model downloads) goes to the network as usual.
import * as A from "./analyzer.js";
import { Session, settings } from "./session.js";

const ok = (extra = {}) => ({ ok: true, ...extra });
const fail = (error, status = 400) => [status, { error }];

export class Api {
  constructor(sessions, profiles, { watch = { change: 0.02, stable: 2, force: 90 }, speakers_default = true } = {}) {
    this.sessions = sessions; this.profiles = profiles; this.watch = watch; this.speakers_default = speakers_default;
    this.routes = {
      "GET /api/config": () => this.config(), "GET /api/profiles": () => this.list_profiles(), "GET /api/state": (q) => this.state(q),
      "POST /api/start": (q, d) => this.start(d), "POST /api/end": (q, d) => this.on(d, (s) => s.end()), "POST /api/pause": (q, d) => this.on(d, (s) => s.set_paused(!!d.on)),
      "POST /api/listen": (q, d) => this.on(d, (s) => s.set_listening(!!d.on)), "POST /api/analyze": (q, d) => this.on(d, (s) => { s.force = true; }),
      "POST /api/notes": (q, d) => this.on(d, (s) => s.set_notes(String(d.text || ""))),
      "POST /api/finish": (q, d) => this.on(d, (s) => ({ ok: s.finish() })), "POST /api/regenerate": (q, d) => this.on(d, (s) => ({ ok: s.regenerate() })),
      "POST /api/retry": (q, d) => this.retry(d), "POST /api/discard": (q, d) => this.on(d, (s) => s.discard(!!d.delete)),
      "POST /api/decision": (q, d) => this.on(d, (s) => (s.decide(d.decision || "") ? undefined : { error: "Could not save the decision" })),
      "POST /api/feedback": (q, d) => this.on(d, (s) => ({ ok: s.set_feedback(d.state || "") })),
      "POST /api/context_add": (q, d) => this.on(d, (s) => s.add_context(String(d.text || ""))), "POST /api/context_remove": (q, d) => this.on(d, (s) => s.remove_context(parseInt(d.index ?? -1))),
      "POST /api/chat": (q, d) => this.chat(d), "POST /api/chat_dismiss": (q, d) => this.on(d, (s) => s.chat_dismiss(d.id ?? null)),
      "POST /api/speaker_name": (q, d) => this.on(d, (s) => s.set_speaker_name(parseInt(d.id || 0), d.name || "")),
      "POST /api/watch": (q, d) => this.on(d, (s) => s.set_watch(!!d.on, d.source ? { label: String(d.source.label || "Shared screen").slice(0, 60) } : null)),
      "POST /api/watch_interval": (q, d) => this.on(d, (s) => { s.watch.interval = Math.max(5, Math.min(3600, parseInt(d.seconds || 30))); s.touch(); }),
      "POST /api/exhibit": (q, d) => this.exhibit(d), "POST /api/exhibit_delete": (q, d) => this.on(d, (s) => ({ ok: s.exhibit_delete(parseInt(d.n || 0)) })),
      "POST /api/exhibit_retry": (q, d) => this.on(d, (s) => ({ ok: s.exhibit_retry(parseInt(d.n || 0)) })), "POST /api/record": (q, d) => this.record(d),
      "POST /api/recording_delete": (q, d) => this.recording_delete(d), "POST /api/form": (q, d) => this.on(d, (s) => ({ ok: s.write_form_answers() })),
      "POST /api/fill_form": () => fail("The browser edition cannot open the portal for you. Use the Fill helper under the evaluation form."), "POST /api/fill_cancel": () => [200, ok()],
    };
  }
  sess(d) { const s = this.sessions.sessions.get(d.sid || ""); if (!s) throw Object.assign(new Error("Unknown session"), { status: 404 }); return s; }
  async on(d, fn) {                                        // run fn on the session: {ok:true} unless it returns something (or an error)
    const s = this.sess(d), r = await fn(s);
    return r && r.error ? fail(r.error) : [200, r || ok()];
  }
  config() {
    return [200, { mock: A.cfg.MOCK_ANALYZER, platform: "pages", recordings_dir: "", recordings_cloud_warning: false, record_audio: true, video_record: true,
      require_consent: settings.REQUIRE_CONSENT, retention_days: 30, form_url: "", form_mode: "", analyze_interval: settings.ANALYZE_INTERVAL, watch: this.watch, asr_partials: true }];
  }
  list_profiles() {
    return [200, Object.values(this.profiles).map((p) => {
      const o = {}; for (const k of ["id", "name", "description", "you_label", "other_label", "subject_label", "extra", "context_hint", "decisions", "live_sections", "final_sections", "drafts", "exhibit"]) o[k] = p[k] ?? null;
      o.has_rubric = !!p.rubric; o.has_form = !!p.form;
      o.rubric = p.rubric ? { areas: p.rubric.areas.map((a) => ({ id: a.id, name: a.name, weight: a.weight })), advance_threshold: p.rubric.advance_threshold } : null;
      o.form = p.form ? { title: p.form.title, categories: p.form.categories } : null;
      return o;
    })];
  }
  state(q) {
    const live = this.sessions.live();
    const out = { sessions: [...this.sessions.sessions.values()].sort((a, b) => (a.started < b.started ? 1 : -1)).map((s) => s.list_item()), live_sid: live ? live.sid : null,
      capture_models: A.cfg.MOCK_ANALYZER ? "demo" : settings.asr_status() };
    const s = this.sessions.sessions.get(q.sid || ""), rev = parseInt(q.rev ?? -1), first = q.first === "1";
    if (s) {
      out.live = s.live_block(parseInt(q.since || 0), first); out.rev = s.rev;
      if (s.rev !== rev || first) { out.detail = s.detail_block(); if (first) out.detail.notes = s.notes; }
      out.events = s.events.slice(-60);
    }
    return [200, out];
  }
  async start(d) {
    const profile = this.profiles[d.profile];
    if (!profile) return fail("Unknown profile");
    const cur = this.sessions.live();
    if (cur && !d.end_current) return [409, { error: "live", live_sid: cur.sid, live_name: cur.name }];
    if (cur) cur.end();
    const context = String(d.context || "").split("\n").filter((c) => c.trim());
    const s = new Session(this.sessions, profile, { subject: d.subject || "", extra: d.extra || (profile.extra || {}).default || "", context,
      role: String(d.role || "").slice(0, 200), speakers: d.speakers ?? this.speakers_default });
    this.sessions.sessions.set(s.sid, s);
    s.start();
    return [200, ok({ sid: s.sid })];
  }
  async retry(d) { const s = this.sess(d); return s.state === "review" ? [200, { ok: s.regenerate() }] : fail("Nothing to retry"); }
  async chat(d) {
    const s = this.sess(d), text = String(d.text || "").trim().slice(0, 4000);
    if (!text) return fail("Empty message");
    if (d.mode === "context") s.chat_context(text); else s.chat_ask(text);
    return [200, ok()];
  }
  async exhibit(d) {
    const s = this.sess(d), b64 = String(d.image || "").split(",").pop();
    if (!b64.startsWith("iVBOR")) return fail("Only PNG screenshots are accepted", 415);
    if (d.auto && !(s.watch.on && s.state === "live")) return [200, { ok: false, dropped: true }];
    s.add_exhibit(b64, d.question || "", d.note || "", !!d.auto);
    return [200, ok()];
  }
  async record(d) {
    const s = this.sess(d);
    if (d.action === "stop") return [200, { ok: s.record_stop() }];
    if (d.source) s.source = { label: String(d.source.label || "Shared screen").slice(0, 60) };
    const res = await s.record_start(!!d.audio, !!d.video, !!d.consent);
    return res.error ? [400, res] : [200, res];
  }
  async recording_delete(d) { const s = this.sess(d), removed = await s.delete_clip(d.all ? null : parseInt(d.n || 0)); s.touch(); return [200, ok({ removed })]; }

  async handle(method, path, query = {}, body = {}) {
    const route = this.routes[`${method} ${path}`];
    if (!route) return [404, { error: `Unknown route ${path}` }];
    try { return await route(query, body); }
    catch (e) { return [e.status || 500, { error: e.message || String(e) }]; }
  }
}

/** Route this page's own fetch("/api/...") calls to the in-page API. */
export function install_fetch(api, win = window) {
  const real = win.fetch.bind(win);
  win.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    if (!url.startsWith("/api/")) return real(input, init);
    const [path, qs = ""] = url.split("?");
    const [status, obj] = await api.handle(((init && init.method) || "GET").toUpperCase(), path, Object.fromEntries(new URLSearchParams(qs)), init && init.body ? JSON.parse(init.body) : {});
    return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
  };
}
