// Web edition hooks for the shared UI (shared/ui/app.js). The desktop edition captures audio, windows and recordings
// with local devices; here the BROWSER does it and talks to the server: microphone + shared tab audio over a
// WebSocket, screen sharing for screenshots / watching, and MediaRecorder for recordings (sent to the server in pieces).
// Sign-in uses the HttpOnly session cookie, so <img>, <video> and the WebSocket authenticate without extra code.
(() => {
"use strict";
const P = { name: "web", web: true, noFolders: true, user: null, cfg: {}, audio: null, screen: null, rec: null, asr: null };
window.PLATFORM = P;

// Audio is started and stopped with Start transcribing / Stop transcribing, so the desktop's Transcribe audio / Listening switches would only confuse.
P.noListenSwitch = true;
P.speakers = true;                         // the start screen offers "tell the speakers apart"
P.fine = "Audio is sent to this server and transcribed there; it is not stored. Only the text, your notes and screenshots go to the AI you chose (Claude or GitHub Copilot). Recording starts only when you press Record.";
P.emptyTranscript = "Press Start transcribing at the top, allow the microphone, and lines appear here as people speak.";
P.sourcesNote = "Your side is this browser's microphone; the other side is the sound of the Teams/Zoom tab you picked when you pressed Start transcribing (only its sound is used). Missing? Use “＋ Add the other person”.";

const jget = async (url, init) => { const r = await fetch(url, init); let j = {}; try { j = await r.json(); } catch (e) { /* no body */ } return { ok: r.ok, status: r.status, j }; };
const dirty = id => { const el = document.getElementById(id); if (el) el.dataset.sig = ""; };
const LSget = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } };
const LSset = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } };

/* ------------------------------------------------------------------ styles for the parts only this edition has */
const css = document.createElement("style");
css.textContent = `
.authwrap{position:fixed;inset:0;z-index:200;background:var(--bg);display:flex;align-items:center;justify-content:center;padding:16px;overflow:auto}
.authbox{width:min(400px,100%);display:flex;flex-direction:column;gap:8px}
.authbox h1{font-size:34px;margin:0}.authbox b{color:var(--accent)}
.authbox label{display:flex;flex-direction:column;gap:2px;font-size:13px;color:var(--mute)}
.authtabs{display:flex;gap:4px;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:3px}
.authtabs button{flex:1;border-color:transparent;background:transparent}.authtabs button.on{background:var(--card);border-color:var(--line);font-weight:600}
.steps{padding-left:20px;margin:6px 0 10px}.steps li{margin:3px 0}
#side .acct{padding:6px 14px;font-size:12px}
.bm{display:inline-block;padding:4px 10px;border:1px dashed var(--accent);border-radius:8px;text-decoration:none;cursor:grab}
`;
document.head.appendChild(css);

/* ------------------------------------------------------------------ sign in / sign up */
function showAuth() {
  if (document.querySelector(".authwrap")) return;
  const cfg = P.cfg || {}, open = cfg.registration_open !== false;
  let mode = "login";
  const wrap = document.createElement("div");
  wrap.className = "authwrap";
  document.body.appendChild(wrap);
  const draw = (err = "") => {
    wrap.innerHTML = `<form class="authbox card" id="authForm"><h1 class="serif">Debrief <b>AI</b></h1>
      <div class="authtabs" role="tablist"><button type="button" data-m="login" class="${mode === "login" ? "on" : ""}">Sign in</button>
        <button type="button" data-m="register" class="${mode === "register" ? "on" : ""}" ${open ? "" : "disabled"}>Sign up</button></div>
      <p class="mute small" style="margin:2px 0">${mode === "login" ? "Welcome back." : cfg.first_user ? "The first account becomes the administrator." : "Create your account, then connect your own AI account (Claude or GitHub Copilot)."}${open ? "" : " Sign-up is closed on this server; ask the administrator for an account."}</p>
      <label>Email<input type="email" id="auEmail" required autocomplete="username"></label>
      <label>Password<input type="password" id="auPw" required minlength="8" autocomplete="${mode === "login" ? "current-password" : "new-password"}"></label>
      ${mode === "register" ? '<label>Confirm password<input type="password" id="auPw2" required minlength="8" autocomplete="new-password"></label>' : ""}
      ${err ? `<div class="errbox" role="alert">${esc(err)}</div>` : ""}
      <button class="primary" id="auGo" style="padding:10px">${mode === "login" ? "Sign in" : "Create account"}</button></form>`;
    for (const b of wrap.querySelectorAll(".authtabs button")) b.onclick = () => { mode = b.dataset.m; draw(); };
    wrap.querySelector("#authForm").onsubmit = async e => {
      e.preventDefault();
      const email = wrap.querySelector("#auEmail").value.trim(), pw = wrap.querySelector("#auPw").value;
      if (mode === "register" && pw !== wrap.querySelector("#auPw2").value) return draw("The two passwords do not match.");
      wrap.querySelector("#auGo").disabled = true;
      const r = await jget("/api/auth/" + mode, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password: pw }) });
      if (!r.ok) return draw(typeof r.j.detail === "string" ? r.j.detail : "That did not work (" + r.status + ")");
      LSset("dbf.afterSignup", mode === "register");
      location.reload();
    };
    const em = wrap.querySelector("#auEmail"); if (em) em.focus();
  };
  draw();
}
P.onUnauthorized = () => showAuth();

const loadScript = src => new Promise((res, rej) => { const s = document.createElement("script"); s.src = src; s.onload = res; s.onerror = () => rej(new Error("Could not load " + src)); document.head.appendChild(s); });

P.beforeBoot = async () => {
  P.cfg = (await jget("/api/config")).j || {};
  const me = await jget("/api/auth/me");
  if (!me.ok) { showAuth(); return false; }
  P.user = me.j;
  await loadScript("aimodel.js");
  installAiModel(P);                                                                    // choose Claude or GitHub Copilot (shared with the desktop)
  await P.refreshAi();
  setInterval(P.refreshAi, 20000);
  if (P.aiNeedsLogin()) setTimeout(P.openAiModel, 500);                                 // new users connect their model first
  return true;
};

/* ------------------------------------------------------------------ account and AI model */
P.openAccount = () => {
  const u = P.user || { email: "" };
  modal(`<h3 class="serif">Account</h3><div class="small mute">${esc(u.email)}${u.role === "admin" ? " · administrator" : ""}</div>
    <p class="small">Calls are analysed with <b>${P.ai ? esc(P.ai.name) : "your AI"}</b>, using your own login, so the usage counts against your plan and nobody else's.</p>
    <div class="btnrow"><button class="primary" id="acAi">Change AI model</button></div>
    <div class="btnrow" style="margin-top:14px;justify-content:space-between"><button id="acOut">Sign out</button><button id="acClose">Close</button></div>`);
  $("acClose").onclick = closeModal;
  $("acOut").onclick = P.act.signout;
  $("acAi").onclick = () => { closeModal(); P.openAiModel(); };
};
P.beforeStart = async () => {
  if (P.aiNeedsLogin()) { toast("Connect your " + P.ai.name + " account first"); P.openAiModel(); return false; }
  return true;
};
P.topExtra = () => `${P.aiChip ? P.aiChip() : ""}<button class="small ghost" data-act="account" title="Account and sign out">${esc(P.user ? P.user.email : "")}</button>`;
P.topSig = () => JSON.stringify([P.aiSig && P.aiSig(), P.user && P.user.email]);
P.todayExtra = () => '<br><a href="/api/day_summary" download>Download today\'s summary (CSV)</a>';

/* ------------------------------------------------------------------ audio: microphone + shared tab/system audio -> WebSocket */
const WORKLET = "class Tap extends AudioWorkletProcessor{process(i){const c=i[0][0];if(c)this.port.postMessage(c.slice(0));return true}}registerProcessor('tap',Tap);";

function pcm16(f) {
  const out = new Int16Array(f.length);
  for (let i = 0; i < f.length; i++) { const s = Math.max(-1, Math.min(1, f[i])); out[i] = s < 0 ? s * 0x8000 : s * 0x7fff; }
  return out;
}
async function startAudio() {
  const sid = ST.sid, proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/sessions/${encodeURIComponent(sid)}/audio`);
  ws.binaryType = "arraybuffer";
  await new Promise((res, rej) => {
    ws.onopen = res;
    ws.onerror = () => rej(new Error("Could not open the audio connection"));
    ws.onclose = e => rej(new Error(e.code === 4401 ? "Please sign in again" : e.code === 4409 ? "This session is not live" : e.reason || "Audio connection closed (" + e.code + ")"));
  });
  const ctx = new AudioContext({ sampleRate: 16000 });
  await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: "text/javascript" })));
  const mix = ctx.createMediaStreamDestination();             // what the recordings contain
  const streams = [], names = [], sources = [];
  const attach = (stream, role, name) => {
    streams.push(stream); names.push(name); sources.push({ role: role ? "other" : "you", name });
    const node = new AudioWorkletNode(ctx, "tap");
    let pending = [], n = 0;
    node.port.onmessage = e => {
      const p = pcm16(e.data); pending.push(p); n += p.length;
      if (n >= 4000 && ws.readyState === WebSocket.OPEN) {      // ~0.25 s per frame
        const frame = new Uint8Array(1 + n * 2); frame[0] = role;
        let o = 1; for (const q of pending) { frame.set(new Uint8Array(q.buffer), o); o += q.byteLength; }
        ws.send(frame); pending = []; n = 0;
      }
    };
    const src = ctx.createMediaStreamSource(stream);
    src.connect(node); src.connect(mix);
  };
  let micOk = true;
  try { attach(await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }), 0, "Microphone (browser)"); }
  catch (e) { micOk = false; toast("Microphone not available: " + e.message, 6000); }
  ws.send(JSON.stringify({ sources }));
  ws.onmessage = e => {
    try {
      const m = JSON.parse(e.data);
      if (m.type === "status") { P.asr = m; }
      else if (m.type === "error") toast(m.message, 7000);
    } catch (x) { /* ignore */ }
  };
  ws.onclose = () => { if (P.audio && P.audio.ws === ws) { P.audio = null; toast("The transcription connection closed. Press Start transcribing to reconnect.", 6000); dirty("bar"); } };
  P.audio = { ws, ctx, streams, names, mix, sid, sources, attach };
  dirty("bar");
  await addOtherSide(true);                                   // Start transcribing always listens to both sides
  if (P.audio && !P.audio.streams.length) { stopAudio(); throw new Error("Nothing could be captured. Allow the microphone (or share the call's tab) and try again."); }
}

/* Start transcribing listens to both people: the microphone (you) and the sound of the Teams/Zoom tab or of the system (the
   other person). A browser can only get that sound through its share picker, so a short explanation comes first. Only the
   sound goes to the transcriber; the picture is kept so the same share can be used to watch the screen. */
function explainStart() {
  if (LSget("dbf.skipIntro", false)) return Promise.resolve(true);
  return new Promise(res => {
    modal(`<h3 class="serif">Start transcribing</h3>
      <p class="small">Both people are transcribed, so the browser asks for two permissions, one after the other:</p>
      <ol class="steps small"><li><b>Your microphone</b>: press Allow.</li>
        <li><b>The sound of the call</b> (the candidate): in the share window open the <b>Tab</b> list, pick the Teams or Zoom tab (for a desktop app choose <b>Entire Screen</b>), keep <b>Share with tab audio</b> on and press <b>Share</b>.</li></ol>
      <p class="small mute">This one share is all the browser needs: its sound is transcribed, and the picture is used only if you later press Watch screen, Capture now or Record. Nothing is recorded or shown to anyone otherwise.</p>
      <label class="small" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="osSkip" style="width:auto"> Don't show this again</label>
      <div class="btnrow" style="justify-content:flex-end;margin-top:8px"><button id="osNo">Cancel</button><button class="primary" id="osYes">Continue</button></div>`);
    $("osNo").onclick = () => { closeModal(); res(false); };
    $("osYes").onclick = () => { if ($("osSkip").checked) LSset("dbf.skipIntro", true); closeModal(); res(true); };
  });
}
function takeOtherSound(screen) {                              // hand the share's sound to the transcriber (the picture stays for watching)
  const a = P.audio, tracks = screen.stream.getAudioTracks();
  if (!a || a.sources.some(s => s.role === "other")) return false;
  if (!tracks.length) { toast("No sound was shared. In the picker choose a tab (or Entire Screen) and tick “Share audio”.", 7000); return false; }
  screen.audioTaken = true;
  a.attach(new MediaStream(tracks), 1, "Shared tab / system audio");
  if (a.ws.readyState === WebSocket.OPEN) a.ws.send(JSON.stringify({ sources: a.sources }));
  dirty("bar");
  return true;
}
async function addOtherSide(auto) {
  const a = P.audio; if (!a) return;
  if (a.sources.some(s => s.role === "other")) return toast("The other side is already being captured");
  try { takeOtherSound(await P.shareDisplay(true)); }                // one share for the sound and, later, for watching the screen
  catch (e) { toast("The call's sound was not shared, so only your microphone is transcribed. Use “＋ Add the other person” to try again.", 8000); }
}
function stopAudio() {
  const a = P.audio; if (!a) return;
  P.audio = null;
  try { if (a.ws.readyState === WebSocket.OPEN) a.ws.send("flush"); } catch (e) { /* closed */ }
  setTimeout(() => { try { a.ws.close(); } catch (e) { /* closed */ } }, 1500);        // let the last utterance come back
  a.streams.forEach(s => s.getTracks().forEach(t => t.stop()));
  setTimeout(() => { a.ctx.close().catch(() => {}); }, 1800);
  dirty("bar");
}

/* ---- the audio controls at the start of the bar, and the hint above the transcript ---- */
P.barExtra = (d, L) => {
  if (d.state !== "live") return "";
  const demo = L.capture.models === "demo";
  let h;
  if (demo) h = '<div class="grp2"><span class="chip ok">Demo audio</span></div>';
  else if (P.audio && P.audio.sid === d.id) {
    const other = P.audio.sources.some(s => s.role === "other");
    h = `<div class="grp2"><span class="chip ok">🎙 Transcribing · ${esc(P.audio.names.join(" + "))}</span>${other ? "" : '<button class="small" data-act="audioOther" title="The call\'s sound is not being captured yet: pick the Teams/Zoom tab to transcribe the other person too">＋ Add the other person</button>'}<button class="small" data-act="audioStop">Stop transcribing</button></div>`;
  } else {
    h = '<div class="grp2"><button class="primary" data-act="audioStart">🎙 Start transcribing</button></div>';
  }
  if (L.watch.on && !P.screen) h += '<div class="warnbox small" style="flex-basis:100%">Screen sharing stopped. Choose what to share again to continue watching.</div>';
  return h;
};
P.barSig = () => JSON.stringify([!!P.audio, P.audio && P.audio.sources.length, !!P.screen, P.screen && P.screen.label, !!P.rec, ST.live && ST.live.capture.connected, ST.live && ST.live.watch.on]);
P.liveBanner = (d, L) => {
  if (d.state !== "live" || L.capture.models === "demo" || P.audio || L.capture.connected || ST.lines.size) return "";
  return '<div class="card" style="border-color:var(--accent)"><b>Press “Start transcribing” to capture the call.</b>' +
    '<div class="small mute">It transcribes you (microphone) and the other person (the sound of the Teams/Zoom tab you pick in the browser\'s share window). Sharing a screen is separate and optional (“Choose what to share”).</div></div>';
};

/* ------------------------------------------------------------------ keep the browser pieces in step with the session */
P.afterRender = () => {
  const L = ST.live, d = typeof cur === "function" ? cur() : null;
  const live = d && L && L.state === "live";
  if (P.audio && (!live || ST.sid !== P.audio.sid)) stopAudio();
  if (P.syncBrowser) P.syncBrowser(L, d, live);
};

/* ------------------------------------------------------------------ actions that replace the desktop device ones */
P.act = {
  account() { P.openAccount(); },
  async signout() { await fetch("/api/auth/logout", { method: "POST" }); location.reload(); },
  async audioStart() { if (!(await explainStart())) return; try { await startAudio(); poll(); } catch (e) { toast(e.message, 6000); } },
  audioStop() { stopAudio(); poll(); },
  audioOther() { addOtherSide(); },
};

/* ------------------------------------------------------------------ screen, watching, recording and the fill helper are shared with the Pages edition */
installBrowserFeatures(P, {
  audioOn: () => !!P.audio,
  needOther: () => !!P.audio && !P.audio.sources.some(s => s.role === "other"),
  takeOther: takeOtherSound,
  mixStream: () => (P.audio ? P.audio.mix.stream : null),
  sink(kind, n, sid) {                              // a recording goes to the server in small pieces while it records
    let seq = 0, chain = Promise.resolve(), failed = "";
    return {
      write(blob) {
        const mine = seq++;
        chain = chain.then(async () => {
          for (let attempt = 0; attempt < 4; attempt++) {                  // retry a flaky connection a few times
            try {
              const r = await fetch(`/api/record_chunk?sid=${encodeURIComponent(sid)}&n=${n}&kind=${kind}&seq=${mine}`, { method: "POST", body: blob });
              if (r.ok) return;
              if (r.status >= 400 && r.status < 500) { failed = (await r.json().catch(() => ({}))).detail || "upload refused"; return; }
            } catch (x) { /* retry */ }
            await wait(1000 * (attempt + 1));
          }
          failed = "upload failed";
        });
      },
      async done(duration) {
        await chain;
        if (failed) return { error: failed };
        await api("/api/record_done", { sid, n, kind, duration });
        return {};
      },
    };
  },
});

})();
