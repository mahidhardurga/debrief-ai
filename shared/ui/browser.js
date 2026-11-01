// Browser features shared by the two browser editions (the server edition and the GitHub Pages edition): sharing a screen or
// window, screenshots, watching the screen, recording with MediaRecorder, and the portal fill helper. Each edition passes hooks
// for the parts that differ (where recordings go, where the mixed call audio comes from).
//   hooks.audioOn()      -> true while the call's audio is being transcribed
//   hooks.needOther()    -> optional: true while the other side of the call is not transcribed (the picker then asks for its sound too)
//   hooks.takeOther(screen) -> optional: hand the share's sound to the transcriber
//   hooks.mixStream()    -> MediaStream with the transcribed audio (recordings contain it), or null
//   hooks.sink(kind, n, sid) -> { write(blob), done(durationSeconds) -> Promise<{error?}> }   where a recorded file goes
(() => {
"use strict";
window.installBrowserFeatures = function (P, hooks) {
const dirty = id => { const el = document.getElementById(id); if (el) el.dataset.sig = ""; };
const LSget = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } };
const LSset = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } };
P.screen = P.screen || null; P.rec = P.rec || null;

function niceLabel(l) {
  if (!l) return "Shared screen";
  if (/^screen:/i.test(l)) return "Screen";
  if (/^web-contents/i.test(l)) return "Browser tab";
  if (/^window:/i.test(l)) return "Window";
  return l.slice(0, 60);
}

/* ------------------------------------------------------------------ screen sharing, screenshots, watching */
function setScreen(stream) {
  stopScreen();
  const v = document.createElement("video");
  v.muted = true; v.playsInline = true; v.srcObject = stream; v.play().catch(() => {});
  const t = stream.getVideoTracks()[0];
  P.screen = { stream, video: v, label: niceLabel(t && t.label) };
  t.onended = () => {
    if (P.screen && P.screen.stream === stream) { P.screen = null; toast("Screen sharing stopped"); dirty("bar"); }
  };
  dirty("bar");
  return P.screen;
}
/* One browser share serves everything: the sound of the call (transcribing), the screen the AI watches, screenshots and recordings.
   Its audio tracks, once handed to the transcriber (screen.audioTaken), keep running when only the picture is replaced or stopped. */
function stopScreen() {
  if (!P.screen) return;
  P.screen.stream.getTracks().forEach(t => { if (!(P.screen.audioTaken && t.kind === "audio")) t.stop(); });
  P.screen = null;
}
async function pickScreen() {
  const wantSound = !!(hooks.needOther && hooks.needOther());           // the other side is not being transcribed yet: ask for its sound in the same share
  try {
    const s = setScreen(await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 5 }, audio: wantSound }));
    if (wantSound && hooks.takeOther) hooks.takeOther(s);
    return s;
  } catch (e) { toast("Sharing cancelled"); return null; }
}
/** The share to use: the one already running, or ask for one (with sound when the transcriber needs it). */
P.shareDisplay = async wantSound => {
  const s = P.screen;
  if (s && (!wantSound || s.stream.getAudioTracks().some(t => t.readyState === "live"))) return s;
  const n = setScreen(await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 5 }, audio: !!wantSound }));
  return n;
};
function grab(maxW) {
  const v = P.screen && P.screen.video; if (!v || !v.videoWidth) return null;
  const k = Math.min(1, maxW / v.videoWidth), c = document.createElement("canvas");
  c.width = Math.round(v.videoWidth * k); c.height = Math.round(v.videoHeight * k);
  c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
  return c;
}
P.preview = pv => {
  if (!pv) return;
  if (P.screen && Date.now() - (P._pvT || 0) > 3000) { const c = grab(160); if (c) { P._pvT = Date.now(); P._pv = c.toDataURL("image/jpeg", 0.6); } }
  if (P.screen && P._pv) { pv.src = P._pv; pv.style.display = "block"; } else pv.style.display = "none";
};


const W = { prev: null, lastKept: null, changeSince: null, stableSince: null, next: 0 };
function signature(c) {
  const s = document.createElement("canvas"); s.width = 64; s.height = 36;
  const g = s.getContext("2d"); g.drawImage(c, 0, 0, 64, 36);
  const d = g.getImageData(0, 0, 64, 36).data, out = new Float32Array(64 * 36);
  for (let i = 0; i < out.length; i++) out[i] = (d[i * 4] + d[i * 4 + 1] + d[i * 4 + 2]) / 3;
  return out;
}
function diff(a, b) { if (!a || !b) return 1; let n = 0; for (let i = 0; i < a.length; i++) if (Math.abs(a[i] - b[i]) > 12) n++; return n / a.length; }
async function watchTick() {          // same algorithm as the desktop edition: keep a frame once the screen changed and then settled
  const L = ST.live;
  if (!(L && L.watch.on && L.state === "live" && P.screen)) { W.prev = W.lastKept = W.changeSince = W.stableSince = null; return; }
  if (Date.now() < W.next) return;
  W.next = Date.now() + L.watch.interval * 1000;
  const c = grab(1600); if (!c) return;
  const sg = signature(c), now = Date.now() / 1000, cfg = (ST.cfg && ST.cfg.watch) || { change: 0.02, stable: 2, force: 90 };
  const changed = !W.prev || diff(sg, W.prev) > cfg.change; W.prev = sg;
  if (changed) { W.changeSince = W.changeSince || now; W.stableSince = null; } else { W.stableSince = W.stableSince || now; W.changeSince = null; }
  const everyInterval = cfg.mode === "interval";                      // a frame every interval when something new is on screen
  const stable = everyInterval || (!changed && (L.watch.interval >= cfg.stable || now - W.stableSince >= cfg.stable));
  const forced = W.changeSince !== null && now - W.changeSince >= cfg.force;
  const differs = !W.lastKept || diff(sg, W.lastKept) > cfg.change;
  if ((stable || forced) && differs) {
    W.lastKept = sg; if (forced) W.changeSince = now;
    await api("/api/exhibit", { sid: ST.sid, image: c.toDataURL("image/png"), question: "(screen watch)", note: "", auto: true });
    ST.rev = -1;
  }
}
setInterval(() => { watchTick().catch(() => {}); }, 500);


/* ------------------------------------------------------------------ recording (MediaRecorder; the edition decides where the files go) */
const pickMime = list => list.find(m => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
function newRecorder(kind, stream, n, sid) {
  const mime = kind === "audio" ? pickMime(["audio/webm;codecs=opus", "audio/webm"]) : pickMime(["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"]);
  const rec = new MediaRecorder(stream, kind === "audio" ? { mimeType: mime, audioBitsPerSecond: 48000 } : { mimeType: mime, videoBitsPerSecond: 900000, audioBitsPerSecond: 48000 });
  const sink = hooks.sink(kind, n, sid), o = { kind, rec, sink };
  rec.ondataavailable = e => { if (e.data && e.data.size) sink.write(e.data); };
  rec.start(8000);
  return o;
}
async function startRecording() {
  const opts = ST.recOpts, L = ST.live;
  if (opts.audio && !hooks.audioOn()) return toast("Press Start transcribing first: the recording contains what the microphone and the shared tab hear.", 6000);
  if (opts.video && !P.screen && !(await pickScreen())) return;
  const body = { sid: ST.sid, action: "start", audio: opts.audio, video: opts.video, source: P.screen ? { label: P.screen.label } : L.source };
  let r = await api("/api/record", body);
  if (r.need_consent) { if (!(await consentDialog())) return; r = await api("/api/record", { ...body, consent: true }); }
  if (r.error) return toast(r.error);
  const sid = ST.sid, n = r.n, recs = [], mix = hooks.mixStream();
  if (opts.audio) recs.push(newRecorder("audio", mix, n, sid));
  if (opts.video) recs.push(newRecorder("video", new MediaStream([...P.screen.stream.getVideoTracks(), ...(mix ? mix.getAudioTracks() : [])]), n, sid));
  P.rec = { sid, n, recs, t0: Date.now() };
  toast("Recording started"); dirty("bar"); poll();
}
async function finishRecording() {          // stop the recorders, make sure every piece is stored, tell the session each file is complete
  const R = P.rec; if (!R) return;
  P.rec = null; dirty("bar");
  const secs = (Date.now() - R.t0) / 1000;
  await Promise.all(R.recs.map(o => new Promise(res => { o.rec.onstop = res; try { o.rec.state !== "inactive" ? o.rec.stop() : res(); } catch (e) { res(); } })));
  for (const o of R.recs) {
    const r = await o.sink.done(secs);
    if (r && r.error) toast("Recording " + o.kind + " not saved: " + r.error, 6000);
  }
  ST.rev = -1; poll();
}
window.addEventListener("beforeunload", e => { if (P.rec || hooks.audioOn()) { e.preventDefault(); e.returnValue = ""; } });

/* keep the screen and the recording in step with the session (the edition calls this after every render) */
P.syncBrowser = (L, d, live) => {
  if (P.rec) {                                      // the session ended, or the recording was stopped from elsewhere
    const running = d && (d.clips || []).some(c => c.state === "recording" || c.state === "paused");
    if (!live || ST.sid !== P.rec.sid || (!running && Date.now() - P.rec.t0 > 3000)) finishRecording();
  }
  if (P.rec) for (const o of P.rec.recs) {            // pausing the session pauses the recording
    if (L.paused && o.rec.state === "recording") o.rec.pause();
    else if (!L.paused && o.rec.state === "paused") o.rec.resume();
  }
  if (!live && P.screen && (!ST.sid || !d || d.state !== "live")) stopScreen();
};

/* ------------------------------------------------------------------ actions */
P.act = Object.assign(P.act || {}, {
  async picker() {
    const s = await pickScreen();
    if (s) { await api("/api/watch", { sid: ST.sid, on: ST.live.watch.on, source: { label: s.label } }); poll(); }
  },
  async capture() {
    if (!P.screen && !(await pickScreen())) return;
    await wait(300);
    const c = grab(1600); if (!c) return toast("The shared screen is not ready yet, try again in a second");
    exhibitDialog(c.toDataURL("image/png"), null);
  },
  async watch() {
    const L = ST.live;
    if (L.watch.on) { await api("/api/watch", { sid: ST.sid, on: false }); return poll(); }
    if (!LSget("dbf.watchOk", false)) {
      if (!await confirmBox("Let the AI watch your screen?", "Every few seconds this page looks at the screen you share. A screenshot is sent to the AI whenever something new is on screen, and every one is kept with the session. It stops when the call ends or is paused.", "Start watching")) return;
      LSset("dbf.watchOk", true);
    }
    if (!P.screen && !(await pickScreen())) return;
    W.next = 0;
    await api("/api/watch", { sid: ST.sid, on: true, source: { label: P.screen.label } }); poll();
  },
  async record() {
    const rec = (cur().clips || []).find(c => c.state === "recording" || c.state === "paused");
    if (rec) { await api("/api/record", { sid: ST.sid, action: "stop" }); await finishRecording(); return poll(); }
    await startRecording();
  },
  async copyFill() {
    const d = cur();
    copyText(JSON.stringify({ app: "debrief-ai", name: d.name, fields: d.form_fields.map(f => ({ label: f.label, value: f.value })) }));
  },
  async fillGo() {
    const url = ($("portal") || {}).value || ST.portal;
    ST.portal = url; LSset("dbf.portal", url);
    if (/^https?:\/\//.test(url)) window.open(url, "_blank", "noopener");
    else toast("Enter the portal address first (https://…)");
  },
  permYes() { toast("Use the Fill helper under the evaluation form."); },
});

/* ------------------------------------------------------------------ portal fill helper (bookmarklet; never submits) */
function HELPER() {
  (async () => {
    let txt = "";
    try { txt = await navigator.clipboard.readText(); } catch (e) { txt = prompt("Paste the fill data (Debrief AI: “Copy fill data for the helper”)") || ""; }
    let data; try { data = JSON.parse(txt); } catch (e) { data = null; }
    if (!data || data.app !== "debrief-ai") { alert("The clipboard has no Debrief AI fill data. In Debrief AI press “Copy fill data for the helper”, then click this bookmark again."); return; }
    const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    const toks = s => new Set(norm(s).split(" ").filter(w => w.length > 2));
    const score = (a, b) => { const A = toks(a), B = toks(b); if (!A.size || !B.size) return 0; let n = 0; A.forEach(x => { if (B.has(x)) n++; }); return n / Math.min(A.size, B.size); };
    const txtOf = s => (s || "").replace(/\s+/g, " ").trim();
    const vis = el => { const r = el.getBoundingClientRect(), st = getComputedStyle(el); return r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none"; };
    const isW = n => n.matches && n.matches("input,select,textarea,button") || (n.querySelector && n.querySelector("input,select,textarea"));
    const heads = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6,legend,[role=heading]")];
    const headingFor = el => { let best = ""; for (const h of heads) { if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) best = txtOf(h.innerText); else break; } return best; };
    const caption = el => {
      if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return txtOf(l.innerText); }
      const al = el.getAttribute("aria-label"); if (al) return txtOf(al);
      const w = el.closest("label"); if (w) return txtOf(w.innerText);
      let n = el; for (let i = 0; i < 6 && n; i++) { let p = n.previousElementSibling; while (p) { if (!isW(p)) { const t = txtOf(p.innerText); if (t && t.length < 300) return t; } p = p.previousElementSibling; } n = n.parentElement; }
      return "";
    };
    const set = (el, v) => {
      const proto = el.tagName === "SELECT" ? HTMLSelectElement.prototype : el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);
      el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true }));
    };
    const best = (cap, head) => { let b = null, bs = 0.5; for (const f of data.fields) { const s = Math.max(score(cap, f.label), score(head + " " + cap, f.label)); if (s > bs) { bs = s; b = f; } } return b; };
    const done = [], skipped = [], used = new Set();
    for (const el of document.querySelectorAll("select,textarea,input[type=text],input:not([type])")) {
      if (el.disabled || el.readOnly || (el.tagName !== "SELECT" && !vis(el)) || el.type === "search" || el.type === "hidden") continue;
      const cap = caption(el), f = best(cap, headingFor(el)); if (!f || used.has(f)) { if (cap) skipped.push(cap); continue; }
      if (el.tagName === "SELECT") {
        const o = [...el.options].find(x => norm(x.text) === norm(f.value)) || [...el.options].find(x => norm(x.text) && norm(f.value).includes(norm(x.text)));
        if (!o) { skipped.push(cap); continue; }
        set(el, o.value);
      } else set(el, f.value);
      used.add(f); done.push(cap || f.label);
    }
    const groups = {}; for (const el of document.querySelectorAll("input[type=radio]")) (groups[el.name || el.id] = groups[el.name || el.id] || []).push(el);
    for (const els of Object.values(groups)) {
      const cap = caption(els[0].closest("fieldset") || els[0]) || headingFor(els[0]), f = best(cap, "");
      const pick = f && els.find(r => norm(txtOf((r.closest("label") || {}).innerText || r.value)) === norm(f.value));
      if (pick) { pick.click(); used.add(f); done.push(cap); } else if (cap) skipped.push(cap);
    }
    const box = document.createElement("div");
    box.style.cssText = "position:fixed;z-index:2147483647;right:12px;bottom:12px;max-width:340px;max-height:60vh;overflow:auto;background:#fff;color:#111;border:2px solid #d97757;border-radius:10px;padding:10px 12px;font:13px/1.4 system-ui;box-shadow:0 6px 24px rgba(0,0,0,.35)";
    box.innerHTML = "<b>Debrief AI filled " + done.length + " field(s).</b><br>Nothing was submitted. Review every answer, then press Submit yourself." +
      (skipped.length ? "<details><summary>" + skipped.length + " field(s) not matched</summary>" + skipped.slice(0, 40).map(s => "• " + s.replace(/</g, "&lt;")).join("<br>") + "</details>" : "") +
      "<div style='margin-top:6px'><button id='dbfx'>Close</button></div>";
    document.body.appendChild(box); box.querySelector("#dbfx").onclick = () => box.remove();
  })();
}
P.fillHtml = (d, p) => {
  const bm = "javascript:(" + encodeURIComponent(HELPER.toString()) + ")()";
  return `<h4>Fill the real form (browser helper)</h4>
    <div class="small mute">A web page cannot reach into your portal window, so a bookmark does the filling in your own browser. It never submits.</div>
    <ol class="steps small"><li>Drag <a class="bm" href="${bm}" onclick="return false" title="Drag me to the bookmarks bar">Debrief fill</a> to your bookmarks bar (once).</li>
      <li><button class="small" data-act="copyFill">Copy fill data for the helper</button> then open the portal form: <input id="portal" style="flex:1;min-width:200px;width:auto" value="${esc(ST.portal || "")}" placeholder="Portal address (https://…)"> <button class="small" data-act="fillGo">Open</button></li>
      <li>On the portal form click the <b>Debrief fill</b> bookmark. It fills the matching fields and lists what it could not match; review everything and press Submit yourself.</li></ol>
    <div class="small mute">The helper matches fields by their labels, so check each answer. You can always copy any answer above and paste it by hand.</div>`;
};
};
})();
