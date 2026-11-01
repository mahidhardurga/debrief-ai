// The browser edition's session engine and /api, run in Node with the demo (mock) analyzer and an in-memory store:
// the same lifecycle the server tests walk through (start, live analysis, topics added live, notes, chat, screenshots, recording,
// end, decision, files, reload from storage), so the three editions behave alike.
import assert from "node:assert/strict";
import fs from "node:fs";
import * as A from "../engine/analyzer.js";
import { Api } from "../engine/api.js";
import { MemoryStore } from "../engine/storage.js";
import { Session, SessionStore, settings } from "../engine/session.js";

const root = new URL("../../shared/", import.meta.url);
const profiles = {};
for (const f of fs.readdirSync(new URL("profiles/", root))) { const p = JSON.parse(fs.readFileSync(new URL("profiles/" + f, root), "utf-8")); profiles[p.id] = p; }
const sample = (id) => fs.readFileSync(new URL(id === "interview" ? "samples/sample_transcript.txt" : "samples/sample_meeting.txt", root), "utf-8");

Object.assign(settings, { time_scale: 0.01, demo_delay: 2000, ANALYZE_MIN_GAP: 0, sample_text: sample, asr_status: () => "idle" });
A.cfg.MOCK_ANALYZER = true;
const wait = async (fn, ms = 8000) => { const t0 = Date.now(); for (;;) { const v = await fn(); if (v) return v; if (Date.now() - t0 > ms) throw new Error("timeout waiting: " + fn); await new Promise((r) => setTimeout(r, 15)); } };
const PNG = Buffer.concat([Buffer.from("89504e470d0a1a0a", "hex"), Buffer.alloc(64, 48)]).toString("base64");

const mem = new MemoryStore();
const sessions = new SessionStore(mem, profiles);
const api = new Api(sessions, profiles);
const call = async (m, p, body = {}, q = {}) => { const [status, json] = await api.handle(m, p, q, body); return { status, ...json }; };
const state = async (sid, extra = {}) => (await api.handle("GET", "/api/state", { sid, rev: -1, ...extra }, {}))[1];

// config and profiles
assert.equal((await call("GET", "/api/config")).platform, "pages");
const profs = (await api.handle("GET", "/api/profiles"))[1];
assert.deepEqual(profs.map((p) => p.id).sort(), ["interview", "meeting"]);
assert.ok(profs[0].has_rubric !== undefined && !profs.some((p) => "system" in p), "prompts are not exposed");

// lifecycle with a role-based topic list
const sid = (await call("POST", "/api/start", { profile: "interview", subject: "Asha", role: "Golang backend engineer", context: "5 yrs Go", speakers: true })).sid;
assert.ok(sid);
assert.equal((await call("POST", "/api/start", { profile: "meeting" })).status, 409, "one live session at a time");
let st = await wait(async () => { const s = await state(sid, { first: "1" }); return s.detail.rubric && !s.detail.rubric.building && s.live.lines.length >= 7 && s.detail.analysis ? s : null; });
assert.equal(st.detail.rubric.role, "Golang backend engineer");
await call("POST", "/api/analyze", { sid });                                       // "Analyze now" once there are enough lines
st = await wait(async () => { const s2 = await state(sid, { first: "1" }); return s2.detail.rubric.areas.some((a) => a.dynamic && a.off_role) ? s2 : null; });
assert.equal(st.live.capture.models, "demo");

// notes, context, pause, listening, chat, speaker names
await call("POST", "/api/notes", { sid, text: "nervous" });
await call("POST", "/api/context_add", { sid, text: "he is a lead" });
assert.deepEqual((await state(sid)).detail.context, ["5 yrs Go", "he is a lead"]);
await call("POST", "/api/context_remove", { sid, index: 0 });
assert.deepEqual((await state(sid)).detail.context, ["he is a lead"]);
await call("POST", "/api/pause", { sid, on: true }); assert.equal((await state(sid)).live.paused, true);
await call("POST", "/api/pause", { sid, on: false });
await call("POST", "/api/listen", { sid, on: false }); assert.equal((await state(sid)).live.listening, false);
await call("POST", "/api/listen", { sid, on: true });
await call("POST", "/api/chat", { sid, text: "How is the candidate doing?", mode: "ask" });
await call("POST", "/api/chat", { sid, text: "He used Kafka", mode: "context" });
await wait(async () => { const d = (await state(sid)).detail; return d.chat.length >= 4 && d.context.includes("He used Kafka"); });
await call("POST", "/api/speaker_name", { sid, id: 2, name: "Priya" });
assert.deepEqual((await state(sid)).detail.speakers, { on: true, names: { 2: "Priya" } });

// screenshots
assert.equal((await call("POST", "/api/exhibit", { sid, image: "data:image/png;base64,aGVsbG8=" })).status, 415);
assert.equal((await call("POST", "/api/exhibit", { sid, image: "data:image/png;base64," + PNG, question: "Reverse a string" })).ok, true);
const ex = await wait(async () => (await state(sid)).detail.exhibits.find((e) => e.status === "done"));
assert.ok(ex.analysis.title && ex.url.startsWith("blob:"), "the image is served from this tab");
assert.equal((await call("POST", "/api/exhibit", { sid, image: PNG, auto: true })).dropped, true, "frames after watching stopped are dropped");
await call("POST", "/api/watch", { sid, on: true, source: { label: "Browser tab" } });
assert.equal((await call("POST", "/api/exhibit", { sid, image: PNG, auto: true })).ok, true);
await wait(async () => (await state(sid)).detail.exhibits.length === 2 && (await state(sid)).detail.exhibits.every((e) => e.status === "done"));
assert.equal((await state(sid)).live.watch.kept, 1);
await call("POST", "/api/exhibit_delete", { sid, n: 2 });
assert.equal((await state(sid)).detail.exhibits.length, 1);

// recording: consent, the page hands over its files, then they are played back and deleted
assert.deepEqual(await call("POST", "/api/record", { sid, action: "start", audio: true, video: true }), { status: 200, need_consent: true });
const rec = await call("POST", "/api/record", { sid, action: "start", audio: true, video: true, consent: true });
assert.equal(rec.status, 200); assert.equal((await call("POST", "/api/record", { sid, action: "start", audio: true })).status, 400, "already recording");
assert.equal((await call("POST", "/api/record", { sid, action: "stop" })).ok, true);
const s = sessions.sessions.get(sid);
await s.clip_uploaded(rec.n, "audio", new Blob(["AUDIO"]), 9.5); await s.clip_uploaded(rec.n, "video", new Blob(["VIDEOVIDEO"]), 9.5);
const clip = (await state(sid)).detail.clips[0];
assert.equal(clip.state, "saved"); assert.equal(clip.size, 15); assert.ok(clip.audio_url.startsWith("blob:") && clip.video_url.startsWith("blob:"));
assert.equal(await (s.clip_file(rec.n, "audio")).text(), "AUDIO");
assert.equal((await state(sid)).detail.consent.confirmed, true);

// end -> final analysis -> decision -> files
assert.equal((await call("POST", "/api/end", { sid })).ok, true);
let d = await wait(async () => { const x = (await state(sid)).detail; return x.state === "review" && x.final ? x : null; });
assert.ok(d.score.score && d.final.summary.startsWith("Mock"));
assert.equal((await call("POST", "/api/decision", { sid, decision: "maybe" })).status, 400);
assert.equal((await call("POST", "/api/decision", { sid, decision: "advance" })).ok, true);
d = (await state(sid)).detail;
assert.equal(d.state, "decided"); assert.equal(d.feedback.state, "pending");
for (const f of ["report.md", "evaluation.json", "result.json", "evaluation-form.md", "next-round-handoff.md", "transcript.txt"]) assert.ok(d.files.includes(f), f + " is missing");
assert.ok(!d.files.includes("reject-feedback.md"), "only the chosen decision's draft is kept");
assert.ok(d.form_fields.length > 5 && d.form_text.startsWith("# "));
assert.equal((await call("POST", "/api/feedback", { sid, state: "submitted" })).ok, true);
assert.ok(s.virtual_files().get("transcript.txt").includes("(Priya)") === false, "one other speaker so far: no tags");
assert.ok(sessions.day_summary_csv(s.date).includes("Asha"));

// everything survives a reload from storage
await s.flush();
const again = new SessionStore(mem, profiles); await again.load_all();
const back = again.sessions.get(sid);
assert.ok(back && back.state === "decided" && back.decision === "advance" && back.speaker_names["2"] === "Priya");
assert.equal(back.exhibits.length, 1); assert.ok(back.exhibits_public()[0].url.startsWith("blob:"), "screenshots come back from storage");
assert.equal(back.clips[0].state, "saved"); assert.ok(back.clip_file(1, "video"), "recordings come back from storage");
assert.deepEqual(back.profile.rubric.areas.map((a) => a.id), s.profile.rubric.areas.map((a) => a.id), "the topic list including added topics is saved");
assert.equal(back.final.weighted_score, s.final.weighted_score);

// discard deletes everything
assert.equal((await call("POST", "/api/discard", { sid, delete: true })).ok, true);
assert.equal((await call("POST", "/api/end", { sid })).status, 404);
assert.equal((await mem.blob_keys(sid)).length, 0, "its images and recordings are deleted too");

// a meeting without a rubric or form, and an old session without speakers
const m = (await call("POST", "/api/start", { profile: "meeting", subject: "Sync" })).sid;
await wait(async () => (await state(m, { first: "1" })).live.lines.length >= 3);
await call("POST", "/api/end", { sid: m });
const md = await wait(async () => { const x = (await state(m)).detail; return x.final ? x : null; });
assert.equal(md.score, null); assert.ok(md.files.includes("report.md"));
console.log("ENGINE OK");
process.exit(0);
