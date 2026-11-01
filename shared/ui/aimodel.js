// "AI model": choose whether Claude or GitHub Copilot analyses the calls. Shared by the desktop and the server edition.
//   GET  /api/ai          -> { choice, name, providers: { claude: {required, connected, found?}, copilot: {...} } }
//   POST /api/ai {choice} -> the same
//   PUT/DELETE /api/claude and /api/copilot save or remove a pasted token (the server requires it; the desktop uses it instead of the CLI's login)
//   desktop only: POST /api/ai_test checks that the CLI, login or token works on this PC
(() => {
"use strict";
window.installAiModel = function (P) {
const NAMES = { claude: "Claude", copilot: "GitHub Copilot" };
const dirty = id => { const el = document.getElementById(id); if (el) el.dataset.sig = ""; };
const send = async (method, url, body) => {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  let j = {}; try { j = await r.json(); } catch (e) { /* 204 */ }
  return { ok: r.ok, status: r.status, j };
};
const detail = r => (typeof r.j.detail === "string" ? r.j.detail : r.j.error || "Request failed (" + r.status + ")");
P.ai = null;

P.refreshAi = async () => {
  const r = await send("GET", "/api/ai");
  if (r.ok) { P.ai = r.j; dirty("top"); }
  return P.ai;
};
const active = () => { const a = P.ai; return a && a.providers ? a.providers[a.choice] : null; };
/** true when the picked model still needs the user's own login (server edition) */
P.aiNeedsLogin = () => { const c = active(); return !!(c && c.required && !c.connected); };
P.aiChip = () => {
  const a = P.ai; if (!a) return "";
  const c = active(), bad = c && ((c.required && !c.connected) || c.found === false);
  return `<button class="small ${bad ? "bad" : ""}" data-act="ai" title="Which AI analyses the calls">AI: ${NAMES[a.choice]}${bad ? " · set up" : ""}</button>`;
};
P.aiSig = () => JSON.stringify(P.ai && [P.ai.choice, ...Object.values(P.ai.providers).flatMap(c => [c.connected, c.found, c.token])]);

const WHERE = () => (P.ai && P.ai.tokens ? "on this PC (tied to your Windows login)" : "on the server");
const HOW = {
  claude: () => `<ol class="steps small"><li>Install <a href="https://docs.claude.com/en/docs/claude-code/overview" target="_blank" rel="noreferrer">Claude Code</a> on your own computer (Claude Pro or Max plan).</li>
    <li>In a terminal run <code>claude setup-token</code>, sign in in the browser it opens and copy the long token it prints.</li>
    <li>Paste it here. It is tested with a tiny request, then stored encrypted ${WHERE()} and never shown again. It lasts about a year, unlike the CLI's own login.</li></ol>`,
  copilot: () => `<ol class="steps small"><li>You need a GitHub account with a <a href="https://github.com/features/copilot" target="_blank" rel="noreferrer">Copilot</a> plan.</li>
    <li>On GitHub open <b>Settings > Developer settings > Personal access tokens > Fine-grained tokens</b>, create a token and give it the <b>Copilot Requests</b> permission.</li>
    <li>Paste it here. It is tested with a tiny request, then stored encrypted ${WHERE()} and never shown again.</li></ol>`,
};
const LOCAL = {
  claude: "Uses the Claude Code login on this PC. If it is not signed in, open a terminal, run <code>claude</code> and type <code>/login</code>.",
  copilot: "Uses the GitHub Copilot CLI and the login on this PC. Install it once with <code>npm install -g @github/copilot</code>, then run <code>copilot login</code> in a terminal.",
};

function body() {
  const a = P.ai; if (!a) return "<p class=\"small\">Loading…</p>";
  const rows = ["claude", "copilot"].map(p => {
    const c = a.providers[p], on = a.choice === p;
    const state = c.required ? (c.connected ? '<span class="chip ok">Connected</span>' : '<span class="chip bad">Not connected</span>')
      : c.token ? '<span class="chip ok">Token saved</span>' : c.found === false ? '<span class="chip bad">CLI not installed</span>' : c.found ? '<span class="chip ok">Uses the login on this PC</span>' : "";
    let more = "";
    const saved = c.required ? c.connected : !!c.token;
    if (on && (c.required || a.tokens)) {
      more = `${a.tokens ? `<p class="small mute" style="margin:4px 0">${LOCAL[p]} A pasted token is used instead and does not expire every few hours.</p>` : ""}${HOW[p]()}<input id="aiTok" type="password" placeholder="Paste your ${p === "claude" ? "token from claude setup-token" : "GitHub token"}" autocomplete="off" spellcheck="false">
        <div class="errbox" id="aiErr" style="display:none"></div>
        <div class="btnrow" style="margin-top:6px"><button class="primary" id="aiGo">${saved ? "Replace token" : "Connect"}</button>${saved ? '<button id="aiDisc">Remove token</button>' : ""}${a.tokens ? '<button id="aiTest">Test it</button>' : ""}</div><div class="small" id="aiRes"></div>`;
    } else if (on) {
      more = `<p class="small mute" style="margin:4px 0 0">${LOCAL[p]}</p><div class="btnrow" style="margin-top:6px"><button id="aiTest">Test it</button></div><div class="small" id="aiRes"></div>`;
    }
    return `<label class="card" style="display:block;cursor:pointer;${on ? "border-color:var(--accent)" : ""}">
      <div style="display:flex;gap:8px;align-items:center"><input type="radio" name="aiPick" value="${p}" ${on ? "checked" : ""} style="width:auto"> <b>${NAMES[p]}</b> ${state}</div>${more ? `<div style="margin-top:6px" data-stop>${more}</div>` : ""}</label>`;
  }).join("");
  return `<p class="small mute">Pick the AI that listens to the call and writes the analysis. You can switch at any time, even during a call: the next analysis uses the new choice.</p>${rows}`;
}

function draw() {
  modal(`<h3 class="serif">AI model</h3><div id="aiBody">${body()}</div><div class="btnrow" style="justify-content:flex-end;margin-top:10px"><button id="aiClose">Close</button></div>`);
  $("aiClose").onclick = closeModal;
  wire();
}
function wire() {
  document.querySelectorAll("input[name=aiPick]").forEach(r => r.onchange = async () => {
    const res = await send("POST", "/api/ai", { choice: r.value });
    if (res.ok) { P.ai = res.j; dirty("top"); $("aiBody").innerHTML = body(); wire(); toast("Now using " + NAMES[res.j.choice]); } else toast(detail(res));
  });
  document.querySelectorAll("[data-stop]").forEach(e => e.addEventListener("click", ev => ev.stopPropagation()));
  const go = $("aiGo"), p = P.ai && P.ai.choice;
  if (go) go.onclick = async () => {
    const tok = $("aiTok").value.trim(); if (tok.length < 20) return;
    go.disabled = true; go.textContent = "Checking…";
    const r = await send("PUT", "/api/" + p, { token: tok });
    if (!r.ok) { const e = $("aiErr"); e.style.display = ""; e.textContent = detail(r); go.disabled = false; go.textContent = "Connect"; return; }
    P.ai = r.j; dirty("top"); closeModal(); toast(NAMES[p] + " connected");
  };
  const dis = $("aiDisc");
  if (dis) dis.onclick = async () => { await send("DELETE", "/api/" + p); await P.refreshAi(); closeModal(); toast(NAMES[p] + " disconnected"); };
  const test = $("aiTest");
  if (test) test.onclick = async () => {
    test.disabled = true; $("aiRes").textContent = "Asking " + NAMES[p] + " a tiny question…";
    const r = await send("POST", "/api/ai_test", {});
    $("aiRes").innerHTML = r.j.ok ? '<span class="chip ok">Works</span>' : `<div class="errbox">${esc(r.j.error || "It did not answer")}</div>`;
    test.disabled = false;
  };
}
P.openAiModel = async () => { await P.refreshAi(); draw(); };
P.act = Object.assign(P.act || {}, { ai() { P.openAiModel(); } });
if (typeof ACT !== "undefined") ACT.ai = P.act.ai;                    // app.js copied P.act before the page booted; add ours to its table too
};
})();
