// GitHub Pages edition hooks for the shared UI (shared/ui/app.js). There is no server: the session engine (engine/*.js) runs in this
// page and answers the UI's /api calls, Whisper and the speaker model run in a worker on the visitor's machine, and analysis goes
// straight to the Anthropic API with the visitor's own key. Sessions are kept in this browser (IndexedDB).
(() => {
"use strict";
const P = { name: "pages", web: true, noFolders: true, noListenSwitch: true, speakers: true, audio: null, screen: null, rec: null, engine: null };
window.PLATFORM = P;
P.fine = "Everything runs in this browser: audio is transcribed here and never uploaded, and your sessions stay on this device. Only transcript text, your notes and screenshots go to Anthropic, with your own API key. Recording starts only when you press Record.";
P.emptyTranscript = "Press Start transcribing at the top, allow the microphone, and lines appear here as people speak.";
P.sourcesNote = "Your side is this browser's microphone. The other side is the sound of the Teams/Zoom tab you picked when you pressed Start transcribing (only its sound is used). Missing? Use “＋ Add the other person”.";

const dirty = id => { const el = document.getElementById(id); if (el) el.dataset.sig = ""; };
const get = k => { try { return sessionStorage.getItem(k) ?? localStorage.getItem(k) ?? ""; } catch (e) { return ""; } };
const put = (k, v, remember) => { try { sessionStorage.removeItem(k); localStorage.removeItem(k); (remember ? localStorage : sessionStorage).setItem(k, v); } catch (e) { /* private mode */ } };
const LSget = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } };
const LSset = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } };
let M = null;                                   // the engine modules, loaded in beforeBoot
const MODELS = { live: ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"], final: ["claude-opus-5-5", "claude-sonnet-5-5"] };
const SPEECH = [["Xenova/whisper-base.en", "base.en (about 150 MB, better)"], ["Xenova/whisper-tiny.en", "tiny.en (about 40 MB, faster)"]];

function applySettings() {                      // no API key: the demo mode (a sample call and fake analysis) so the page is usable at once
  const key = get("dbf.apiKey").trim();
  M.A.cfg.MOCK_ANALYZER = !key;
  M.A.cfg.apiKey = () => get("dbf.apiKey").trim();
  const live = get("dbf.liveModel") || MODELS.live[0], fin = get("dbf.finalModel") || MODELS.final[0];
  Object.assign(M.A.cfg, { LIVE_MODEL: live, CORRECT_MODEL: live, EXHIBIT_MODEL: live, CHAT_MODEL: live, FILL_MODEL: live, FINAL_MODEL: fin });
  dirty("top");
}

/* ------------------------------------------------------------------ boot: the engine, storage, the in-page /api */
P.beforeBoot = async () => {
  const [A, S, Api, Store, Audio, Reports] = await Promise.all([import("./engine/analyzer.js"), import("./engine/session.js"), import("./engine/api.js"),
    import("./engine/storage.js"), import("./engine/audio.js"), import("./engine/reports.js")]);
  M = { A, S, Api, Store, Audio, Reports };
  const profiles = {};
  for (const id of ["interview", "meeting"]) profiles[id] = await (await fetch(`profiles/${id}.json`)).json();
  const samples = {};
  for (const [id, f] of [["interview", "sample_transcript"], ["meeting", "sample_meeting"]]) samples[id] = await (await fetch(`samples/${f}.txt`)).text();
  const store = await Store.open_store();
  M.sessions = new S.SessionStore(store, profiles);
  Object.assign(S.settings, { sample_text: id => samples[id] || samples.meeting, asr_status: () => (P.asr ? P.asr.status : "idle"), asr_error: () => (P.asr ? P.asr.error : ""),
    make_speaker_book: () => new Audio.SpeakerBook(a => P.asr.embed(a), { threshold: LSget("dbf.speakerThreshold", 0.86) }),
    free_mb: async () => { try { const e = await navigator.storage.estimate(); return (e.quota - e.usage) / 1048576; } catch (x) { return Infinity; } } });
  M.api = new Api.Api(M.sessions, profiles);
  Api.install_fetch(M.api);
  applySettings();
  await M.sessions.load_all();
  window.__debrief = M;                         // for debugging and the automated checks
  if (!get("dbf.apiKey")) setTimeout(() => toast("Demo mode: a sample call is replayed with fake analysis. Add your Anthropic API key in Settings to analyse real calls.", 9000), 800);
  return true;
};

/* ------------------------------------------------------------------ settings */
P.openSettings = () => {
  const opt = (list, cur) => list.map(x => { const v = Array.isArray(x) ? x[0] : x, l = Array.isArray(x) ? x[1] : x; return `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(l)}</option>`; }).join("");
  modal(`<h3 class="serif">Settings</h3>
    <p class="small mute">Everything runs in this browser. Your key is kept in this tab only unless you tick “remember”, and it is sent only to Anthropic.</p>
    <label class="small">Anthropic API key<input id="setKey" type="password" placeholder="sk-ant-… (empty: demo mode)" value="${esc(get("dbf.apiKey"))}" autocomplete="off" spellcheck="false"></label>
    <label class="small" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="setRem" style="width:auto" ${localStorage.getItem("dbf.apiKey") ? "checked" : ""}> Remember on this device</label>
    <div class="row"><label class="small" style="flex:1">Live model<select id="setLive">${opt(MODELS.live, get("dbf.liveModel") || MODELS.live[0])}</select></label>
      <label class="small" style="flex:1">Final model<select id="setFinal">${opt(MODELS.final, get("dbf.finalModel") || MODELS.final[0])}</select></label></div>
    <label class="small">Speech model (downloaded once, then cached)<select id="setAsr">${opt(SPEECH, get("dbf.asrModel") || SPEECH[0][0])}</select></label>
    <p class="small mute" id="setGpu">${navigator.gpu ? "WebGPU is available: fast transcription." : "No WebGPU in this browser: transcription uses the slower fallback. Chrome or Edge are recommended."}</p>
    <div class="btnrow" style="justify-content:flex-end"><button id="setNo">Cancel</button><button class="primary" id="setOk">Save</button></div>`);
  $("setNo").onclick = closeModal;
  $("setOk").onclick = () => {
    put("dbf.apiKey", $("setKey").value.trim(), $("setRem").checked);
    put("dbf.liveModel", $("setLive").value, true); put("dbf.finalModel", $("setFinal").value, true);
    const asr = $("setAsr").value; if (asr !== get("dbf.asrModel")) { put("dbf.asrModel", asr, true); P.asr = null; }
    applySettings(); closeModal(); toast($("setKey").value.trim() ? "Saved. New sessions use your API key." : "Saved. Demo mode (no API key).");
  };
};
P.topExtra = () => `<button class="small ${get("dbf.apiKey") ? "" : "bad"}" data-act="settings" title="API key and models">${get("dbf.apiKey") ? "⚙ Settings" : "⚙ Demo mode: add API key"}</button>`;
P.topSig = () => (get("dbf.apiKey") ? "key" : "demo");
P.todayExtra = () => '<br><a href="#" data-act="daySummary">Download today\'s summary (CSV)</a>';

/* ------------------------------------------------------------------ downloads that the server editions serve as files */
function saveBlob(blob, name) { const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000); }
async function ensureZip() {
  if (M.S.settings.JSZip) return;
  await new Promise((res, rej) => { const s = document.createElement("script"); s.src = "https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js"; s.onload = res; s.onerror = () => rej(new Error("Could not load the zip library")); document.head.appendChild(s); });
  M.S.settings.JSZip = window.JSZip;
}
document.addEventListener("click", async e => {              // the "Export zip" links point at /api/export/<id>: build the zip here instead
  const a = e.target.closest("a[href^='/api/export/']"); if (!a) return;
  e.preventDefault(); e.stopPropagation();
  const url = new URL(a.getAttribute("href"), location.origin), sid = decodeURIComponent(url.pathname.split("/").pop()), s = M.sessions.sessions.get(sid);
  if (!s) return;
  try { await ensureZip(); saveBlob(await s.zip_bytes(url.searchParams.get("rec") === "1"), `${sid}${url.searchParams.get("rec") === "1" ? "-with-recordings" : ""}.zip`); }
  catch (x) { toast(x.message); }
}, true);

/* ------------------------------------------------------------------ audio: microphone + the sound of a shared tab, transcribed in this browser */
async function ensureAsr() {
  if (P.asr && P.asr.status !== "error") return P.asr;
  P.asr = new M.Audio.AsrWorker("whisper-worker.js");
  P.asr.on_change = () => { P.asrPct = P.asr.progress; };
  P.asr.load(get("dbf.asrModel") || SPEECH[0][0]).catch(() => {});
  return P.asr;
}
function explainStart() {
  if (LSget("dbf.skipIntro", false)) return Promise.resolve(true);
  return new Promise(res => {
    modal(`<h3 class="serif">Start transcribing</h3>
      <p class="small">Both people are transcribed, so the browser asks for two permissions, one after the other:</p>
      <ol class="steps small"><li><b>Your microphone</b>: press Allow.</li>
        <li><b>The sound of the call</b> (the candidate): in the share window open the <b>Tab</b> list, pick the Teams or Zoom tab (for a desktop app choose <b>Entire Screen</b>), keep <b>Share with tab audio</b> on and press <b>Share</b>.</li></ol>
      <p class="small mute">Only the sound is used: the video is dropped at once, nothing is recorded or shown to anyone. The speech model downloads the first time (a minute or two) and is cached after that.</p>
      <label class="small" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="osSkip" style="width:auto"> Don't show this again</label>
      <div class="btnrow" style="justify-content:flex-end;margin-top:8px"><button id="osNo">Cancel</button><button class="primary" id="osYes">Continue</button></div>`);
    $("osNo").onclick = () => { closeModal(); res(false); };
    $("osYes").onclick = () => { if ($("osSkip").checked) LSset("dbf.skipIntro", true); closeModal(); res(true); };
  });
}
async function startAudio() {
  const s = M.sessions.sessions.get(ST.sid);
  await ensureAsr();
  P.engine = new M.Audio.AudioEngine(P.asr);
  if (s.speakers_on) P.asr.warm();
  P.audio = { sid: ST.sid, get names() { return P.engine.names; }, get sources() { return s.sources; }, get mix() { return P.engine.mix; } };
  try { await P.engine.start(s); } catch (e) { toast("Microphone not available: " + e.message, 6000); }
  await addOtherSide();
  if (!P.engine.streams.length) { stopAudio(); throw new Error("Nothing could be captured. Allow the microphone (or share the call's tab) and try again."); }
  dirty("bar");
}
async function addOtherSide() {
  if (!P.engine || P.engine.has_other()) return;
  try { await P.engine.add_other(); dirty("bar"); }
  catch (e) { toast("The call's sound was not shared, so only your microphone is transcribed. Use “＋ Add the other person” to try again. (" + e.message + ")", 9000); }
}
function stopAudio() { if (P.engine) P.engine.stop(); P.engine = null; P.audio = null; dirty("bar"); }

P.barExtra = (d, L) => {
  if (d.state !== "live") return "";
  let h;
  if (L.capture.models === "demo") h = '<div class="grp2"><span class="chip ok">Demo audio</span></div>';
  else if (P.audio && P.audio.sid === d.id) {
    const other = P.engine && P.engine.has_other();
    h = `<div class="grp2"><span class="chip ok">🎙 Transcribing · ${esc(P.engine.names.join(" + "))}</span>${other ? "" : '<button class="small" data-act="audioOther" title="Pick the Teams/Zoom tab to transcribe the other person too">＋ Add the other person</button>'}<button class="small" data-act="audioStop">Stop transcribing</button></div>`;
  } else h = '<div class="grp2"><button class="primary" data-act="audioStart">🎙 Start transcribing</button></div>';
  if (L.watch.on && !P.screen) h += '<div class="warnbox small" style="flex-basis:100%">Screen sharing stopped. Choose what to share again to continue watching.</div>';
  return h;
};
P.barSig = () => JSON.stringify([!!P.audio, P.engine && P.engine.streams.length, !!P.screen, P.screen && P.screen.label, !!P.rec, ST.live && ST.live.capture.connected, ST.live && ST.live.watch.on]);
P.liveBanner = (d, L) => {
  let h = "";
  if (d.state === "live" && L.capture.models !== "demo" && P.asr && P.asr.status === "loading") h += `<div class="warnbox"><span class="spin"></span> Downloading and loading the speech model in your browser${P.asr.progress ? ` (${Math.round(P.asr.progress)}%)` : ""}. Lines appear once it is ready; it is cached for next time.</div>`;
  if (d.state === "live" && L.capture.models !== "demo" && !P.audio && !L.capture.connected && !ST.lines.size)
    h += '<div class="card" style="border-color:var(--accent)"><b>Press “Start transcribing” to capture the call.</b><div class="small mute">It transcribes you (microphone) and the other person (the sound of the Teams/Zoom tab you pick in the browser\'s share window). Sharing a screen is separate and optional (“Choose what to share”).</div></div>';
  return h;
};
P.afterRender = () => {
  const L = ST.live, d = typeof cur === "function" ? cur() : null, live = d && L && L.state === "live";
  if (P.audio && (!live || ST.sid !== P.audio.sid)) stopAudio();
  if (P.syncBrowser) P.syncBrowser(L, d, live);
};

P.act = {
  settings() { P.openSettings(); },
  async audioStart() { if (!(await explainStart())) return; try { await startAudio(); poll(); } catch (e) { toast(e.message, 7000); } },
  audioStop() { stopAudio(); poll(); },
  audioOther() { addOtherSide(); },
  async daySummary(ds, el, ev) { ev.preventDefault(); const date = dateKey(new Date()); saveBlob(new Blob([M.sessions.day_summary_csv(date)], { type: "text/csv" }), `day-summary-${date}.csv`); },
};

/* ------------------------------------------------------------------ screen, watching, recording and the fill helper (shared with the server edition) */
installBrowserFeatures(P, {
  audioOn: () => !!P.audio,
  mixStream: () => (P.engine && P.engine.mix ? P.engine.mix.stream : null),
  sink(kind, n, sid) {                          // a recording is collected here and stored in this browser when it stops
    const chunks = [];
    return {
      write(blob) { chunks.push(blob); },
      async done(duration) {
        const s = M.sessions.sessions.get(sid);
        if (!s || !chunks.length) return { error: "nothing was recorded" };
        try { await s.clip_uploaded(n, kind, new Blob(chunks, { type: kind === "audio" ? "audio/webm" : "video/webm" }), duration); return {}; }
        catch (e) { return { error: e.message }; }
      },
    };
  },
});
})();
