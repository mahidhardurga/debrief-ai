"use strict";
const PLATFORM = window.PLATFORM || {name: "desktop"};   // edition hooks, see desktop/frontend/platform.js and server/frontend/platform.js
/* ============================== helpers ============================== */
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const LS = {get(k, d){try{const v=localStorage.getItem(k);return v===null?d:JSON.parse(v)}catch(e){return d}}, set(k, v){try{localStorage.setItem(k, JSON.stringify(v))}catch(e){}}};
const fmt = s => { s = Math.max(0, Math.floor(s||0)); const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60; return (h?h+":"+String(m).padStart(2,"0"):m)+":"+String(x).padStart(2,"0"); };
const wait = ms => new Promise(r => setTimeout(r, ms));
async function api(path, data){
  try{
    const r = await fetch(path, data===undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)});
    const j = await r.json().catch(()=>({}));
    if(r.status===401 && PLATFORM.onUnauthorized) PLATFORM.onUnauthorized();
    if(!r.ok && !j.error) j.error = j.detail || "Request failed ("+r.status+")";
    j._status = r.status; return j;
  }catch(e){ return {error:"The app is not reachable ("+e.message+")"}; }
}
function toast(msg, ms=3500){ const t=document.createElement("div"); t.className="toast"; t.textContent=msg; $("toasts").appendChild(t); setTimeout(()=>t.remove(), ms); }
function md(text){
  const lines = esc(text||"").split("\n"); let out="", list=false;
  const inline = s => s.replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/(^|[^*])\*(?!\s)(.+?)\*/g,"$1<i>$2</i>").replace(/`(.+?)`/g,"<code>$1</code>");
  for(const raw of lines){
    const l = raw.trimEnd();
    if(/^\s*[-*] /.test(l)){ if(!list){out+="<ul class='tight'>";list=true} out+="<li>"+inline(l.replace(/^\s*[-*] /,""))+"</li>"; continue; }
    if(list){out+="</ul>";list=false}
    if(/^#{1,4} /.test(l)) out+="<h4>"+inline(l.replace(/^#+ /,""))+"</h4>";
    else if(l) out+="<p>"+inline(l)+"</p>";
  }
  return out + (list?"</ul>":"");
}
function setRegion(el, sig, html){ if(!el) return false; if(el.dataset.sig===sig) return false; el.dataset.sig=sig; el.innerHTML=html; return true; }
function dateLabel(d){
  const t = new Date(); const iso = x => x.toISOString().slice(0,10);
  const loc = x => new Date(x.getTime()-x.getTimezoneOffset()*60000).toISOString().slice(0,10);
  const y = new Date(t.getTime()-86400000);
  if(d===loc(t)) return "Today"; if(d===loc(y)) return "Yesterday";
  const dt = new Date(d+"T12:00:00"); return dt.toLocaleDateString("en-US",{weekday:"short",month:"short",day:"numeric"});
}
function copyText(text){ navigator.clipboard.writeText(text).then(()=>toast("Copied"), ()=>toast("Copy failed")); }
function download(name, text){ const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([text],{type:"text/markdown"})); a.download=name; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),2000); }

/* ============================== state ============================== */
const ST = {cfg:{}, profiles:{}, sessions:[], sid:null, live:null, detail:null, rev:-1, since:0, first:1, lines:new Map(),
  events:[], chatOpen:LS.get("dbf.chat", window.innerWidth>=1500), mode:LS.get("dbf.mode","type"), collapsed:new Set(LS.get("dbf.collapsed",[])),
  viewKey:"", seenAreas:new Set(), pops:{}, portal:LS.get("dbf.portal",""), recOpts:LS.get("dbf.rec",{audio:true,video:true}),
  chatMode:"ask", lastErr:"", polling:false};
const profileOf = id => ST.profiles[id] || {};
const cur = () => ST.detail && ST.detail.id===ST.sid ? ST.detail : null;
const curProfile = () => cur() ? profileOf(cur().profile) : {};
const listItem = sid => ST.sessions.find(s=>s.id===sid);

/* ============================== polling ============================== */
async function poll(){
  if(ST.polling) return; ST.polling = true;
  try{
    const q = new URLSearchParams({sid:ST.sid||"", since:ST.since, rev:ST.rev, first:ST.first});
    const r = await api("/api/state?"+q);
    if(r.error && !r.sessions){ ST.lastErr = r.error; renderTop(); return; }
    ST.lastErr = "";
    ST.sessions = r.sessions || [];
    ST.modelStatus = r.capture_models;
    if(ST.sid && !listItem(ST.sid)){ ST.sid = null; ST.detail = null; ST.live = null; }
    if(r.live){
      ST.live = r.live; ST.since = Math.max(ST.since, r.live.max_v);
      for(const l of r.live.lines) ST.lines.set(l.id, l);
      if(r.detail){ ST.detail = r.detail; ST.rev = r.rev; if(r.detail.notes!==undefined) ST.notesVal = r.detail.notes; }
      ST.events = r.events || [];
      ST.first = 0;
    }
  } finally { ST.polling = false; }
  render();
}

function selectSession(sid){
  ST.sid = sid; ST.detail = null; ST.live = null; ST.rev = -1; ST.notesVal = ""; ST.since = 0; ST.first = 1; ST.lines = new Map(); ST.viewKey = ""; ST.seenAreas = new Set();
  $("side").classList.remove("open"); $("scrim").classList.remove("open");
  poll();
}

/* ============================== render dispatch ============================== */
function viewMode(){
  const d = cur(); if(!ST.sid) return "start"; if(!d) return "loading";
  if(d.state==="live" || d.state==="stopping") return "live";
  if(d.state==="finalizing" && !d.final) return "live";
  return "review";
}
function render(){
  renderSide(); renderTop(); renderChat();
  const mode = viewMode(), key = mode+":"+ST.sid;
  const view = $("view");
  if(key!==ST.viewKey){
    ST.viewKey = key; view.dataset.sig="";
    if(mode==="start") view.innerHTML = startHtml();
    else if(mode==="live") mountLive();
    else if(mode==="review") view.innerHTML = '<div id="rv"></div>';
    else view.innerHTML = '<div class="mute">Loading…</div>';
    if(mode==="start") afterStart();
  }
  if(mode==="start") renderPending();
  if(mode==="live") updateLive();
  if(mode==="review") renderReview();
  document.title = recTitle();
  if(PLATFORM.afterRender) PLATFORM.afterRender();
}
function recTitle(){
  const d = cur(); if(d && ST.live && ST.live.rec_active) return "● REC · "+d.name+" · Debrief AI";
  return d ? d.name+" · Debrief AI" : "Debrief AI";
}

/* ============================== sidebar ============================== */
function stateText(s){
  if(s.state==="live") return s.rec==="rec" ? "● REC" : s.paused ? "Paused" : !s.listening ? "Audio paused" : "Live";
  if(s.state==="stopping"||s.state==="finalizing") return "Summarising…";
  if(s.needs_summary) return "Needs summary";
  if(s.state==="review") return "Ready to review";
  return "Saved";
}
function itemHtml(s){
  const fb = s.fb==="pending" ? "Feedback pending" : s.fb==="filled" ? "Feedback filled, submit it" : s.fb==="submitted" ? "Feedback done" : "";
  const decided = s.state==="decided" && s.dec;
  const chip = decided && s.dec==="advance" ? '<span class="chip ok">✓ Selected</span>' : decided && s.dec==="reject" ? '<span class="chip bad">✗ Not selected</span>'
    : '<span class="chip'+(s.rec==="rec"?" rec":"")+'">'+esc(stateText(s))+'</span>';
  const dot = s.state==="live" ? (s.rec==="rec"?"rec":s.paused?"paused":"live") : s.state==="review"?"review":"decided";
  const fin = s.state==="decided"||s.state==="review";
  return `<div class="item ${s.id===ST.sid?"sel":""}" data-act="select" data-sid="${esc(s.id)}"><span class="dot ${dot}"></span>
    <div style="min-width:0;flex:1"><div class="nm">${esc(s.name)}</div><div class="sub">${esc(s.type_name.split(" (")[0])}${fb?" · "+fb:""}</div>
    <div style="margin-top:2px">${chip}${s.err?' <span class="chip bad" title="Needs attention">!</span>':""}</div></div>
    ${fin?`<button class="x" data-act="dismiss" data-sid="${esc(s.id)}" title="Close (files are kept)">×</button>`:""}</div>`;
}
function groupByDate(list){
  const m = new Map(); for(const s of list){ if(!m.has(s.date)) m.set(s.date, []); m.get(s.date).push(s); }
  return [...m.entries()].sort((a,b)=>b[0].localeCompare(a[0]));
}
function dateBlocks(list, keyPrefix){
  return groupByDate(list).map(([d, items])=>{
    const k = keyPrefix+d, open = !ST.collapsed.has(k);
    return `<div class="dh" data-act="toggle" data-k="${k}">${open?"▾":"▸"} ${esc(dateLabel(d))}</div>${open?items.map(itemHtml).join(""):""}`;
  }).join("");
}
function renderSide(){
  const sel = $("modeSel");
  const opts = [["type","By type"],["all","All sessions"],...Object.values(ST.profiles).map(p=>["t:"+p.id, p.name.split(" (")[0]]),["submit","To submit"]];
  const sig = JSON.stringify(opts)+ST.mode;
  if(sel.dataset.sig!==sig){ sel.dataset.sig=sig; sel.innerHTML = opts.map(o=>`<option value="${o[0]}" ${o[0]===ST.mode?"selected":""}>${esc(o[1])}</option>`).join(""); }
  const vis = ST.sessions.filter(s=>!s.dismissed);
  let html = "";
  if(ST.mode==="type"){
    for(const p of Object.values(ST.profiles)){
      const items = vis.filter(s=>s.type===p.id); if(!items.length) continue;
      const k = "g:"+p.id, open = !ST.collapsed.has(k), sub = items.filter(s=>s.fb==="pending"||s.fb==="filled").length;
      html += `<div class="grp"><div class="gh" data-act="toggle" data-k="${k}">${open?"▾":"▸"} ${esc(p.name.split(" (")[0])}<span class="cnt">${items.length}${sub?" · "+sub+" to submit":""}</span></div>${open?dateBlocks(items,"d:"+p.id+":"):""}</div>`;
    }
  } else if(ST.mode==="all") html = dateBlocks(vis,"d:all:");
  else if(ST.mode==="submit") html = dateBlocks(vis.filter(s=>s.fb==="pending"||s.fb==="filled"),"d:sub:");
  else html = dateBlocks(vis.filter(s=>"t:"+s.type===ST.mode),"d:f:");
  setRegion($("sbList"), JSON.stringify([ST.mode,[...ST.collapsed],ST.sid,vis.map(s=>[s.id,s.state,s.rec,s.paused,s.dec,s.fb,s.err,s.listening,s.needs_summary])]),
    html || '<div class="mute small" style="padding:10px">Nothing here yet.</div>');
  const today = ST.sessions.filter(s=>s.date===dateKey(new Date()));
  const sel2 = today.filter(s=>s.dec==="advance").length, rej = today.filter(s=>s.dec==="reject").length, sub = ST.sessions.filter(s=>s.fb==="pending"||s.fb==="filled").length;
  setRegion($("today"), JSON.stringify([today.length,sel2,rej,sub]), `<b>Today</b> · ${today.length} session${today.length===1?"":"s"}<br>${sel2} selected · ${rej} not selected · ${sub} feedback to submit${PLATFORM.todayExtra?PLATFORM.todayExtra():""}`);
}
function dateKey(d){ return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,10); }

/* ============================== top bar ============================== */
function recPill(){
  const d = cur(); if(!d) return "";
  const clips = d.clips||[]; const active = clips.find(c=>c.state==="recording"||c.state==="paused");
  if(active){
    return `<span class="recpill ${active.state==="paused"?"paused":""}" id="recPill">${active.state==="paused"?"❚❚ REC paused":"● REC"} <span id="recDur"></span> · ${[active.audio&&"audio",active.video&&"video"].filter(Boolean).join(" + ")}</span>`;
  }
  const saved = clips.filter(c=>c.state==="saved"); if(saved.length)
    return `<span class="recpill saved">${saved.length} recording${saved.length>1?"s":""} saved · ${humanSize(saved.reduce((a,c)=>a+c.size,0))}</span>`;
  return "";
}
function humanSize(n){ const u=["B","KB","MB","GB"]; let i=0; while(n>=1024&&i<3){n/=1024;i++} return (i?n.toFixed(1):n.toFixed(0))+" "+u[i]; }
function renderTop(){
  const d = cur(), L = ST.live, p = curProfile();
  const unread = d ? (d.chat||[]).filter(m=>m.unread && m.role==="assistant").length : 0;
  const sig = JSON.stringify([d&&d.id, d&&d.name, d&&d.state, d&&d.decision, L&&L.paused, L&&L.listening, unread, ST.lastErr, ST.chatOpen, (d&&d.clips||[]).map(c=>[c.state,c.n,c.size]), L&&L.capture&&L.capture.error, PLATFORM.topSig?PLATFORM.topSig():""]);
  if($("top").dataset.sig!==sig){
    $("top").dataset.sig = sig;
    let pill = "";
    if(d){
      if(d.state==="live") pill = L&&L.paused ? '<span class="pill warn">Paused</span>' : '<span class="pill live">Live</span>';
      else if(d.state==="stopping"||d.state==="finalizing") pill = '<span class="pill warn"><span class="spin"></span> Summarising…</span>';
      else if(d.state==="review") pill = '<span class="pill">Review</span>';
      else if(d.decision==="advance") pill = '<span class="chip ok">✓ Selected</span>';
      else if(d.decision==="reject") pill = '<span class="chip bad">✗ Not selected</span>';
      else pill = '<span class="pill">Saved</span>';
    }
    $("top").innerHTML = `<button id="menuBtn" data-act="openSide">☰</button>
      <span class="ttl">${d?esc(d.name):"Debrief AI"}</span>
      ${d?`<span class="mute small">${esc(profileOf(d.profile).name||"")}</span>`:""}
      ${pill}
      <span class="sp"></span>
      ${ST.lastErr?`<span class="chip bad" title="${esc(ST.lastErr)}">offline</span>`:""}
      ${d&&d.state==="live"?`<span class="meters">${esc(p.you_label||"You")} <span class="meter"><i id="mYou"></i></span> ${esc(p.other_label||"Others")} <span class="meter"><i id="mOther"></i></span></span>`:""}
      ${recPill()}
      ${d&&d.state==="live"?`<span class="timer" id="timer">0:00</span>
        ${PLATFORM.noListenSwitch?"":`<label class="sw ${L&&L.listening?"on":""}" data-act="listen"><i></i>Listening</label>`}`:""}
      ${PLATFORM.topExtra?PLATFORM.topExtra():""}
      <button id="chatBtn" data-act="chat" ${d?"":"disabled"}>💬 Chat with AI${unread?`<span class="badge">${unread}</span>`:""}</button>`;
  }
  if(d && L){
    if($("timer")) $("timer").textContent = fmt(L.elapsed);
    if($("mYou")){ $("mYou").style.width = Math.min(100, L.capture.levels.you*900)+"%"; $("mOther").style.width = Math.min(100, L.capture.levels.other*900)+"%"; }
    const a = (d.clips||[]).find(c=>c.state==="recording"||c.state==="paused");
    if(a && $("recDur")) $("recDur").textContent = fmt(L.rec_dur);
    const rs = $("recStat"), ra = (d.clips||[]).find(c=>c.state==="recording"||c.state==="paused");
    if(rs && ra) rs.textContent = `Recording ${ra.n} · ${fmt(L.rec_dur)} · ${[ra.audio&&"audio",ra.video&&"video"].filter(Boolean).join(" + ")}${ra.state==="paused"?" (paused)":""}`;
  }
}

/* ============================== start screen ============================== */
function startHtml(){
  const profs = Object.values(ST.profiles);
  return `<div class="start"><h1 class="serif">What are we listening to?</h1>
    <input id="stName" placeholder="Name" autocomplete="off">
    <div class="row" id="stRoleRow"><input id="stRole" placeholder="Role you are hiring for, e.g. Senior Java developer (Spring Boot, AWS)" autocomplete="off" maxlength="200"></div>
    <div class="small mute" id="stRoleHint" style="margin:-2px 0 4px">The AI builds the topic list and scoring from this role, and adds topics live if the interview goes elsewhere. Leave it empty to use the default Java list.</div>
    <div class="row"><textarea id="stCtx" rows="3" placeholder="Optional context"></textarea></div>
    <div class="row"><select id="stProfile">${profs.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select><select id="stExtra"></select></div>
    <label class="small" id="stSpkRow" style="display:none;gap:8px;align-items:center;margin:2px 0 10px;cursor:pointer"><input type="checkbox" id="stSpk" style="width:auto">
      Several people on the call: tell the speakers apart <span class="mute">(Speaker 1, 2, 3… and you can name them)</span></label>
    <div class="btnrow"><button class="primary" data-act="start" style="padding:10px 22px;font-size:15px">Start listening</button></div>
    <div class="fine">${esc(PLATFORM.fine || "Audio is transcribed on this PC and never leaves it. Only text, your notes and screenshots go to Claude. Recording starts later, only when you press the Record button.")}</div>
    <div id="pending" style="margin-top:22px"></div></div>`;
}
function afterStart(){
  const sel = $("stProfile"), saved = LS.get("dbf.profile", "");
  if(saved in ST.profiles) sel.value = saved;
  sel.onchange = syncStart; syncStart();
}
function syncStart(){
  const p = profileOf($("stProfile").value); LS.set("dbf.profile", p.id);
  const sr = $("stSpkRow"); if(sr){ sr.style.display = PLATFORM.speakers ? "flex" : "none"; $("stSpk").checked = LS.get("dbf.speakers", true); $("stSpk").onchange = e => LS.set("dbf.speakers", e.target.checked); }
  $("stName").placeholder = p.subject_label || "Name";
  $("stCtx").placeholder = p.context_hint || "Optional context";
  $("stRoleRow").style.display = $("stRoleHint").style.display = p.has_rubric ? "" : "none";
  const ex = $("stExtra"); ex.style.display = p.extra ? "" : "none";
  if(p.extra) ex.innerHTML = p.extra.options.map(o=>`<option ${o===p.extra.default?"selected":""}>${esc(o)}</option>`).join("");
}
function renderPending(){
  const items = ST.sessions.filter(s=>(s.fb==="pending"||s.fb==="filled") && !s.dismissed);
  setRegion($("pending"), JSON.stringify(items.map(s=>[s.id,s.fb])), items.length ? `<h3 class="serif">Feedback to submit (${items.length})</h3>`+
    items.map(s=>`<div class="item" data-act="select" data-sid="${esc(s.id)}"><div style="flex:1"><div class="nm">${esc(s.name)}</div><div class="sub">${s.fb==="filled"?"Feedback filled, submit it":"Feedback pending"}</div></div><button class="small">Open</button></div>`).join("") : "");
}

/* ============================== live view ============================== */
function mountLive(){
  $("view").innerHTML = `<div id="banner"></div><div id="bar" class="card"></div>
   <div id="cols"><section id="cL"><div class="card"><h3>Live transcript</h3><div id="trBox"></div></div>
     <div class="card"><h3>Your notes <span class="mute small" style="font-weight:400">· included in the analysis</span></h3><textarea id="notes" rows="5" placeholder="Type notes here"></textarea></div></section>
   <section id="cM"><div id="mid"></div></section>
   <section id="cR"><div id="srcs"></div><div id="shots"></div><div id="alog"></div></section></div>`;
  const ta = $("notes"); let tm=null;
  ta.value = ST.notesVal || "";
  ta.oninput = ()=>{ ST.notesVal = ta.value; clearTimeout(tm); tm=setTimeout(()=>api("/api/notes",{sid:ST.sid,text:ta.value}),500); };
  ST.trRendered = new Map(); ST.pv = 0; ST.spkSig = "";
}
function updateLive(){
  const d = cur(), L = ST.live, p = curProfile(); if(!d||!L) return;
  // banner
  let ban = "";
  if(d.state==="stopping"||d.state==="finalizing") ban = `<div class="warnbox"><span class="spin"></span> Finishing the session and writing the final analysis. This can take a minute.</div>`;
  if(L.capture.error) ban += `<div class="errbox">${esc(L.capture.error)}</div>`;
  else if(L.capture.errors && L.capture.errors.length) ban += `<div class="warnbox">${esc(L.capture.errors.join(" · "))}</div>`;
  if(ST.modelStatus && /loading/.test(ST.modelStatus) && d.state==="live") ban += `<div class="warnbox"><span class="spin"></span> Loading the speech models… the first lines may be delayed.</div>`;
  if(PLATFORM.liveBanner) ban += PLATFORM.liveBanner(d, L);
  setRegion($("banner"), ban, ban);
  renderBar(); renderTranscript(); renderMid(); renderRight();
}
function renderBar(){
  const d = cur(), L = ST.live, ro = ST.recOpts;
  const rec = (d.clips||[]).find(c=>c.state==="recording"||c.state==="paused"), saving = (d.clips||[]).some(c=>c.state==="saving");
  const w = L.watch, src = L.source;
  const sig = JSON.stringify([!!rec, saving, ro, L.listening, w.on, w.interval, w.kept, w.status, src&&src.label, L.paused, d.state, ST.pops, ST.cfg.recordings_cloud_warning, L.analyzing, PLATFORM.barSig?PLATFORM.barSig():""]);
  setRegion($("bar"), sig, `
    ${PLATFORM.barExtra?PLATFORM.barExtra(d,L):""}
    <div class="grp2"><button class="${rec?"bad":"primary"}" data-act="record" ${saving||d.state!=="live"?"disabled":""}>${rec?"■ Stop":"● Record"}</button>
      <span class="small mute" id="recStat"></span>
      <span class="rel"><button class="small" data-act="pop" data-p="rec">Options ▾</button>
        ${ST.pops.rec?`<div class="pop" style="top:30px;left:0"><label><input type="checkbox" data-change="recopt" data-k="audio" ${ro.audio?"checked":""} ${rec?"disabled":""}> Audio</label>
          <label><input type="checkbox" data-change="recopt" data-k="video" ${ro.video?"checked":""} ${rec?"disabled":""}> Screen video</label>
          ${rec?'<div class="small mute">Locked while recording</div>':""}</div>`:""}</span></div>
    ${PLATFORM.noListenSwitch?"":`<label class="sw ${L.listening?"on":""}" data-act="listen"><i></i>Transcribe audio</label>`}
    <div class="grp2"><label class="sw ${w.on?"on":""}" data-act="watch"><i></i>Watch screen for the AI</label>
      <span class="small mute">${w.on?`● Watching <a href="#" data-act="pop" data-p="interval">every ${fmtInterval(w.interval)}</a> · ${w.kept} key frame${w.kept===1?"":"s"} kept${w.status?" · "+esc(w.status):""}`:`<a href="#" data-act="pop" data-p="interval">every ${fmtInterval(w.interval)}</a>`}</span>
      <span class="rel">${ST.pops.interval?intervalPop():""}</span></div>
    <div class="grp2"><button class="small" data-act="picker">${src?esc(src.label):"Choose what to share"} ▾</button><img class="pv" id="pvImg" alt="">
      <button class="small" data-act="capture">Capture now</button>
      <span class="rel"><button class="small" data-act="pop" data-p="more">⋯</button>
        ${ST.pops.more?`<div class="pop" style="top:30px;right:0"><div class="it" data-act="addShot">Add screenshot…</div><hr style="border-color:var(--line)">
          <div class="small mute" style="padding:2px 8px">Screenshot interval</div>${intervalItems()}</div>`:""}</span></div>
    <div class="grp2" style="margin-left:auto"><button class="small" data-act="analyze" ${d.state==="live"?"":"disabled"}>${L.analyzing?'<span class="spin"></span> ':""}Analyze now</button>
      <button class="small" data-act="pause" ${d.state==="live"?"":"disabled"}>${L.paused?"▶ Continue session":"❚❚ Pause session"}</button>
      <button class="small bad" data-act="end" ${d.state==="live"?"":"disabled"}>End session</button></div>
    ${ST.cfg.recordings_cloud_warning?`<div class="warnbox small" style="flex-basis:100%">The recordings folder is inside a cloud-synced folder (${esc(ST.cfg.recordings_dir)}). Move it with RECORDINGS_DIR.</div>`:""}
    <input type="file" id="shotFile" accept="image/*" style="display:none">`);
  const stat = $("recStat"); if(stat){
    stat.textContent = rec ? `Recording ${rec.n} · ${rec.duration_text} · ${[rec.audio&&"audio",rec.video&&"video"].filter(Boolean).join(" + ")}${rec.state==="paused"?" (paused)":""}` : saving ? "Saving…" : "Not recording";
  }
  const pv = $("pvImg"); if(PLATFORM.preview){ PLATFORM.preview(pv); } else if(pv && src && Date.now()-ST.pv>5000){ ST.pv=Date.now(); pv.style.display="block"; pv.src="/api/thumb?sid="+encodeURIComponent(ST.sid)+"&w=120&t="+Date.now(); pv.onerror=()=>pv.style.display="none"; }
  const sf = $("shotFile"); if(sf) sf.onchange = ()=>{ const f=sf.files[0]; if(f) readFile(f); sf.value=""; };
}
function fmtInterval(s){ return s>=60 && s%60===0 ? (s/60)+" min" : s+" s"; }
const PRESETS = [10,20,30,60,120,300];
function intervalItems(){ return PRESETS.map(s=>`<div class="it" data-act="interval" data-s="${s}">${s===60?"1 min":s===120?"2 min":s===300?"5 min":s+" s"}</div>`).join("")+
  `<div class="it"><input id="custInt" type="number" min="5" max="3600" placeholder="custom (5–3600 s)" style="width:150px" data-change="custint"></div>`; }
function intervalPop(){ return `<div class="pop" style="top:20px;left:0">${intervalItems()}</div>`; }

function spkInfo(){            // who spoke on the other side: the tags only appear once two different people have been heard
  const ids = new Set(); for(const l of ST.lines.values()) if(l.label==="other" && l.speaker!=null) ids.add(l.speaker);
  const d = cur(); return {multi: ids.size>=2, names: (d && d.speakers && d.speakers.names) || {}};
}
function spkName(spk, id){ return spk.names[String(id)] || "Speaker "+id; }
function renderTranscript(){
  const box = $("trBox"), p = curProfile(), L = ST.live; if(!box) return;
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const spk = spkInfo(), spkSig = JSON.stringify(spk);
  if(ST.spkSig !== spkSig){ ST.spkSig = spkSig; ST.trRendered = new Map(); box.innerHTML = ""; box.dataset.empty = ""; }
  for(const [id, l] of ST.lines){
    const prev = ST.trRendered.get(id);
    if(prev && prev.v===l.v) continue;
    let el = prev && prev.el;
    const tag = spk.multi && l.label==="other" && l.speaker!=null
      ? ` · <span class="spk" data-act="spkRename" data-id="${l.speaker}" title="Click to give this person a name">${esc(spkName(spk, l.speaker))}</span>` : "";
    const html = l.marker ? esc(l.text) : `<div class="w">${esc(l.label==="you"?p.you_label:p.other_label)}${tag} · ${fmt(l.ts)}</div>${esc(l.text)}`;
    if(!el){ el = document.createElement("div"); box.appendChild(el); }
    el.className = l.marker ? "div" : "b "+l.label+(spk.multi && l.label==="other" && l.speaker!=null ? " sp"+((l.speaker-1)%6+1) : ""); el.innerHTML = html;
    ST.trRendered.set(id, {v:l.v, el});
  }
  for(const el of box.querySelectorAll(".partial")) el.remove();
  for(const pt of L.partials){ const el=document.createElement("div"); el.className="b partial "+pt.label; el.innerHTML=`<div class="w">${esc(pt.label==="you"?p.you_label:p.other_label)} …</div>${esc(pt.text)}`; box.appendChild(el); }
  if(!box.childElementCount && !box.dataset.empty){ box.innerHTML=`<div class="mute small" id="trEmpty">${esc(PLATFORM.emptyTranscript||"Listening… lines appear here as people speak.")}</div>`; box.dataset.empty="1"; }
  else if(box.childElementCount>1 && $("trEmpty")){ $("trEmpty").remove(); box.dataset.empty=""; }
  if(near) box.scrollTop = box.scrollHeight;
}

function sectionHtml(sec, val, p){
  if(val===undefined||val===null||(Array.isArray(val)&&!val.length)||val==="") return "";
  const t = sec.type; let body = "";
  if(t==="list") body = `<ul class="tight">${val.map(x=>`<li>${sec.id==="followups"?`<span class="ask">Ask: “${esc(x)}”</span>`:esc(x)}</li>`).join("")}</ul>`;
  else if(t==="text") body = `<div>${esc(val)}</div>`;
  else if(t==="flags") body = val.map(f=>`<div class="flag"><b>${esc(f.title)}</b><br>${esc(f.detail)}</div>`).join("");
  else if(t==="actions") body = `<ul class="tight">${val.map(a=>`<li>${esc(a.task)} <span class="mute">· ${esc(a.owner||"—")} · ${esc(a.due||"no date")}</span></li>`).join("")}</ul>`;
  else if(t==="topics") body = val.map(topicHtml).join("");
  return `<div class="card"><h3>${esc(sec.title)}</h3>${body}</div>`;
}
function ratingClass(r){ return r==="strong"||r==="good"?r:r==="mixed"?"mixed":r==="weak"?"weak":"not"; }
function topicHtml(t){
  return `<div class="topic"><b>${esc(t.topic)}</b> <span class="rt ${ratingClass(t.rating)}">${esc(t.rating)}</span>
    <div class="k">Asked</div><div>${esc(t.asked)}</div><div class="k">Answered</div><div>${esc(t.answered)}</div>
    <div class="k">Feedback</div><div>${esc(t.feedback)}</div><div class="k">A stronger answer</div><div>${esc(t.better_answer)}</div></div>`;
}
function topicTag(a){ return a.dynamic ? ` <span class="chip" title="${a.off_role?"Came up in the interview but is not part of the role":"Added during the interview"}">${a.off_role?"off-role · added live":"added live"}</span>` : ""; }
function rubricNote(d){
  const r = d.rubric; if(!r) return "";
  if(r.building) return `<div class="card"><span class="spin"></span> Building the topic list${r.role?` for “${esc(r.role)}”`:""}…</div>`;
  return r.note ? `<div class="warnbox small">${esc(r.note)}</div>` : "";
}
function coverageHtml(areas, d, p){
  const rub = (d.rubric && d.rubric.areas) || p.rubric.areas;
  const byId = Object.fromEntries((areas||[]).map(a=>[a.id,a]));
  const all = rub.map(r=>{ const a = byId[r.id]||{}; return {...r, covered:!!a.covered, score:a.score, evidence:a.evidence||"", hint:a.hint||r.hint||""}; });
  const cov = all.filter(a=>a.covered), unc = all.filter(a=>!a.covered).sort((a,b)=>(b.weight||0)-(a.weight||0));
  const fresh = new Set(); for(const a of cov){ if(!ST.seenAreas.has(a.id)) fresh.add(a.id); }
  const first = ST.seenAreas.size===0; for(const a of cov) ST.seenAreas.add(a.id);
  const role = d.rubric && d.rubric.role;
  return `<div class="card"><h3>Covered so far</h3>${role?`<div class="small mute" style="margin:-4px 0 6px">${esc(role)}</div>`:""}<div class="bar"><i style="width:${all.length?100*cov.length/all.length:0}%"></i></div><div class="small mute" style="margin:-4px 0 6px">${cov.length}/${all.length}</div>
    ${cov.length?cov.map(a=>`<div class="area ${!first&&fresh.has(a.id)?"new":""}"><span class="sc s${a.score||0}">${a.score??"–"}</span><div><b>${esc(a.name)}</b>${topicTag(a)}<div class="small mute">${esc(a.evidence)}</div></div></div>`).join(""):'<div class="mute small">Nothing covered yet.</div>'}</div>
    <div class="card"><h3>Still to cover <span class="mute small" style="font-weight:400">· ideas for you</span></h3>
    ${unc.map(a=>`<div class="area"><div><b>${esc(a.name)}</b>${topicTag(a)}${a.hint?`<div class="ask small">Ask: “${esc(a.hint)}”</div>`:""}</div></div>`).join("")||'<div class="mute small">Everything has come up.</div>'}</div>`;
}
function renderMid(){
  const d = cur(), p = curProfile(), a = d.analysis, L = ST.live;
  let html = "";
  if(L.analysis_error) html += `<div class="errbox">${esc(L.analysis_error)}</div>`;
  if(!a) html += `<div class="card mute">${L.analyzing?'<span class="spin"></span> Analysing…':"The first analysis appears after a few lines (or press Analyze now)."}</div>`;
  if(p.has_rubric && d.rubric) html += rubricNote(d) + coverageHtml((a&&a.areas)||[], d, p);
  if(a){
    const order = [...p.live_sections].sort((x,y)=>(x.type==="topics")-(y.type==="topics"));
    for(const sec of order) html += sectionHtml(sec, a[sec.id], p);
  }
  setRegion($("mid"), JSON.stringify([html.length, d.rubric&&[d.rubric.building,d.rubric.areas.length,d.rubric.role], a&&a.areas&&a.areas.map(x=>x.covered), L.analysis_error, L.analyzing, a&&JSON.stringify(a).length]), html);
}
function renderRight(){
  const d = cur(), L = ST.live, p = curProfile(), live = d.state==="live";
  const cap = L.capture;
  setRegion($("srcs"), JSON.stringify(cap.sources)+p.id, `<div class="card"><h3>Audio sources</h3>
    ${cap.sources.length?cap.sources.map(s=>`<div class="small"><span class="chip">${esc(s.role==="you"?p.you_label:p.other_label)}</span> ${esc(s.name)}</div>`).join(""):'<div class="mute small">No audio devices in use.</div>'}
    <div class="small mute" style="margin-top:6px">${esc(PLATFORM.sourcesNote || ("The "+(p.other_label||"other")+" side reacts when the other people talk (every output device is monitored), your side when you do."))}</div></div>`);
  const ex = d.exhibits||[];
  setRegion($("shots"), JSON.stringify(ex.map(e=>[e.n,e.status,e.analysis&&e.analysis.rating])), `<div class="card"><h3>Screenshots &amp; code review</h3>
    ${ex.length?ex.map(exhibitHtml).join(""):'<div class="mute small">Capture now, paste with Ctrl+V (try Win+Shift+S first), or Add screenshot… Claude reads the image and reviews it.</div>'}</div>`);
  const ev = ST.events.slice(-8).reverse();
  setRegion($("alog"), JSON.stringify(ev), `<div class="card"><h3>Activity log</h3>${ev.map(e=>`<div class="small"><span class="mute">${esc(e.ts)}</span> ${esc(e.text)}</div>`).join("")||'<div class="mute small">Nothing yet.</div>'}</div>`);
}
function exhibitHtml(e){
  const a = e.analysis||{};
  const st = e.status==="analyzing" ? '<span class="spin"></span> analysing…' : e.status==="error" ? `<span class="chip bad">error</span> ${esc(e.error)}` : `<span class="rt ${ratingClass(a.rating)}">${esc(a.rating||"")}</span>`;
  return `<div class="exh"><a href="${e.url}" target="_blank"><img src="${e.url}" alt=""></a><div style="flex:1;min-width:0"><b>${esc(a.title||("Exhibit "+e.n))}</b> ${st}
    <div class="small mute">${esc(e.question||"")}</div>${e.status==="done"?`<div class="small">${esc(a.feedback||"")}</div>`:""}
    <div class="btnrow">${e.status==="error"?`<button class="small" data-act="exRetry" data-n="${e.n}">Retry</button>`:""}<button class="small ghost" data-act="exDel" data-n="${e.n}">Delete</button></div></div></div>`;
}

/* ============================== review / saved view ============================== */
function renderReview(){
  const d = cur(), p = curProfile(), host = $("rv"); if(!host) return;
  const L = ST.live;
  const sig = JSON.stringify([d.state, d.decision, d.error, !!d.final, d.final&&JSON.stringify(d.final).length, d.exhibits.map(e=>[e.n,e.status]), d.feedback, d.form_data?1:0, L&&L.form_busy, !!(L&&L.fill&&L.fill.state), d.context.length, d.drafts&&Object.keys(d.drafts).length, d.files.length, d.name]);
  if(!host.dataset.built){
    host.dataset.built = "1";
    host.innerHTML = `<div id="rvHead"></div><div id="rvBody"></div><div id="rvIef"></div><div id="rvRec"></div><div id="rvEx"></div><div id="rvSecs"></div><div id="rvAct"></div><div id="rvFiles"></div>`;
  }
  setRegion($("rvHead"), sig, headHtml(d,p));
  setRegion($("rvBody"), sig, bodyHtml(d,p));
  renderIef(d,p);
  setRegion($("rvRec"), JSON.stringify((d.clips||[]).map(c=>[c.n,c.state,c.size,c.audio_url,c.video_url]))+d.state, recHtml(d));
  setRegion($("rvEx"), JSON.stringify(d.exhibits.map(e=>[e.n,e.status,e.analysis&&e.analysis.rating])), d.exhibits.length?`<div class="card"><h3>Screen exhibits</h3>${d.exhibits.map(exhibitHtml).join("")}</div>`:"");
  setRegion($("rvSecs"), sig, d.final ? p.final_sections.map(s=>sectionHtml(s, d.final[s.id], p)).join("") : "");
  setRegion($("rvAct"), sig, actHtml(d,p));
  setRegion($("rvFiles"), sig, filesHtml(d,p));
}
function headHtml(d,p){
  const badge = d.state==="decided" && p.decisions && p.decisions.length ? (d.decision==="advance"?'<span class="chip ok">✓ Selected</span>':'<span class="chip bad">✗ Not selected</span>') : "";
  return `<div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap"><h2 class="serif" style="margin:0 0 4px;font-size:26px">${esc(d.name)}</h2>${badge}
    <span class="mute">${esc(p.name)} · ${esc(d.date)} ${esc(d.time)}${d.extra?" · "+esc(d.extra):""}</span></div>
    ${d.state==="finalizing"?`<div class="warnbox"><span class="spin"></span> Rewriting the summary…</div>`:""}
    ${d.error?`<div class="errbox">${esc(d.error)} <button class="small" data-act="retry">Retry</button></div>`:""}
    ${!d.final&&!d.error&&d.state==="review"?`<div class="errbox">No summary yet. <button class="small" data-act="retry">Retry</button></div>`:""}`;
}
function bodyHtml(d,p){
  const f = d.final; if(!f) return "";
  let h = "";
  if(p.has_rubric){
    const sc = d.score||{}, areas = f.areas||[];
    const rec = f.recommendation, thin = sc.total && (sc.scored/sc.total) < 0.4;
    h += `<div class="card"><div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap"><div><div class="score">${sc.score!=null?sc.score.toFixed(2):"–"} <span class="mute" style="font-size:18px">/ 5</span></div>
      <div class="small mute">Average of the areas that came up, judged for the candidate's level (3 = meets expectations): ${sc.scored||0} of ${sc.total||0} areas scored</div></div>
      ${rec?`<div><span class="chip ${rec.decision==="advance"?"ok":rec.decision==="reject"?"bad":"warn"}">AI recommends ${esc(rec.decision)}</span> <span class="small mute">(threshold ${p.rubric.advance_threshold.toFixed(1)})</span><div class="small">${esc(rec.reason)}</div></div>`:""}</div>
      ${thin?`<div class="warnbox">Thin evidence: only ${sc.scored} of ${sc.total} areas were scored. Treat the number with caution.</div>`:""}
      <p>${esc(f.summary)}</p></div>`;
    const rub = (d.rubric && d.rubric.areas) || p.rubric.areas;
    h += `<div class="card"><h3>Scores${d.rubric&&d.rubric.role?` <span class="mute small" style="font-weight:400">· ${esc(d.rubric.role)}</span>`:""}</h3>${areas.map(a=>{const info=rub.find(x=>x.id===a.id)||{name:a.id,weight:""};
      return `<div class="area"><span class="sc s${a.score||0}">${a.score??"–"}</span><div style="flex:1"><b>${esc(info.name)}</b>${topicTag(info)} <span class="mute small">weight ${info.weight}</span>
        <div class="bar"><i style="width:${(a.score||0)*20}%"></i></div><div class="small mute">${esc(a.evidence||"not assessed")}</div></div></div>`;}).join("")}</div>`;
  } else h += `<div class="card"><h3>Summary</h3><p>${esc(f.summary)}</p></div>`;
  return h;
}
function recHtml(d){
  const clips = d.clips||[]; if(!clips.length) return "";
  return `<div class="card"><h3>Recordings</h3>${ST.cfg.recordings_cloud_warning?`<div class="warnbox small">Stored inside a cloud-synced folder.</div>`:""}
    ${clips.map(c=>`<div class="clip"><b>${esc(c.name)}</b> <span class="chip ${c.state==="saved"?"ok":""}">${esc(c.state)}</span>
      <span class="small mute">${fmt(c.start_ts)} → ${fmt(c.start_ts+c.duration)} · ${esc(c.duration_text)} · ${esc(c.size_text)}${c.source?" · "+esc(c.source):""}</span>
      ${PLATFORM.noFolders?"":`<div class="small mute">${esc(c.path)}</div>`}
      ${c.video_url?`<video controls preload="metadata" src="${c.video_url}"></video>`:c.audio_url?`<audio controls preload="metadata" src="${c.audio_url}"></audio>`:""}
      <div class="btnrow" style="margin-top:4px">${PLATFORM.noFolders?"":`<button class="small" data-act="recOpen" data-n="${c.n}">Show in folder</button>`}
        ${c.video_url?`<a href="${c.video_url}?download=1"><button class="small">Download video</button></a>`:""}${c.audio_url?`<a href="${c.audio_url}?download=1"><button class="small">Download audio</button></a>`:""}
        <button class="small bad" data-act="recDel" data-n="${c.n}">Delete</button></div></div>`).join("")}
    <div class="btnrow"><button class="small bad" data-act="recDelAll">Delete all recordings</button><a href="/api/export/${esc(d.id)}"><button class="small">Export zip</button></a>
      <a href="/api/export/${esc(d.id)}?rec=1"><button class="small">Export with recordings</button></a>${PLATFORM.noFolders?"":'<button class="small" data-act="openFolder">Open session folder</button>'}</div></div>`;
}
function renderIef(d,p){
  const host = $("rvIef"); if(!p.has_form||!d.final){ setRegion(host,"x",""); return; }
  const L = ST.live, fl = L && L.fill;
  const sig = JSON.stringify([d.form_data?1:0, d.form_fields.map(f=>f.value).join("|").length, L&&L.form_busy, fl&&[fl.state,fl.window_open,fl.steps], d.state, d.feedback.state, d.decision]);
  if(host.dataset.sig===sig) return; host.dataset.sig = sig;
  const title = p.form.title;
  let h = "";
  if(!d.form_data){
    h = `<div class="card"><h3>${esc(title)}</h3><div class="mute small">This interview was written up before the form existed.</div>
      <div class="btnrow" style="margin-top:8px"><button class="primary" data-act="formWrite" ${L&&L.form_busy?"disabled":""}>${L&&L.form_busy?'<span class="spin"></span> ':""}Fill the evaluation form</button></div></div>`;
  } else {
    h = `<div class="card"><h3>${esc(title)}: ready to copy</h3>
      <div class="btnrow"><button data-act="copyForm">Copy the whole form as text</button><button data-act="dlForm">Download .md</button></div>
      ${d.form_fields.map((f,i)=>`<div class="field"><div class="l">${esc(f.label)}</div><div class="v">${esc(f.value)}</div><button class="small" data-act="copyField" data-i="${i}">Copy</button></div>`).join("")}
      ${d.state==="decided"&&PLATFORM.fillHtml?PLATFORM.fillHtml(d,p):d.state==="decided"?`<h4>Fill the real form for me</h4><div class="btnrow"><input id="portal" style="flex:1;min-width:240px" value="${esc(ST.portal||ST.cfg.form_url||"")}" placeholder="Portal address (https://…)">
        ${fl&&fl.state&&!["done","error"].includes(fl.state)?`<button class="bad" data-act="fillStop">Stop</button>`:`<button class="primary" data-act="fillGo">Open the form and fill it</button>`}</div>
        <div class="small mute" style="margin-top:4px">${fl?fillStatus(fl):"Opens the in-app browser window and fills the answers. It never submits; you review and press Submit yourself."}</div>`:`<div class="small mute" style="margin-top:8px">Save a decision first, then the form can be filled on the portal.</div>`}
      </div>`;
  }
  host.innerHTML = h;
  const pi = $("portal"); if(pi) pi.oninput = ()=>{ ST.portal = pi.value; LS.set("dbf.portal", pi.value); };
}
function fillStatus(f){
  if(f.state==="done") return "Done. The window is "+(f.window_open?"still open: review it, then submit it yourself.":"closed.");
  if(f.state==="error") return "The fill stopped. See the chat for details.";
  return `<span class="spin"></span> ${esc(f.state)} · ${f.steps} step${f.steps===1?"":"s"}${f.log.length?" · "+esc(f.log[f.log.length-1]):""}`;
}
function actHtml(d,p){
  if(!d.final) return "";
  let h = `<div class="card"><h3>${d.state==="decided"?"Decision":"Your decision"}</h3>`;
  if(p.decisions && p.decisions.length){
    if(d.state==="review") h += `<div class="btnrow">${p.decisions.map(x=>`<button class="${x.style}" data-act="decide" data-d="${x.id}">${esc(x.label)}</button>`).join("")}<button data-act="regen">Regenerate summary</button></div>
      <div class="small mute" style="margin-top:6px">Saving writes the matching draft and updates the day summary.</div>`;
    else h += `<div class="btnrow"><span class="small mute">Decision:</span>${p.decisions.map(x=>`<button class="${d.decision===x.id?x.style:""}" data-act="switch" data-d="${x.id}" data-label="${esc(x.short)}">${esc(x.short)}</button>`).join("")}</div>
      <div class="small mute" style="margin-top:6px">You can change it at any time; the report, result and drafts are rewritten. Nothing is changed on any website.</div>
      ${p.has_form?`<h4>Feedback on the portal</h4><div class="btnrow"><span class="chip ${d.feedback.state==="submitted"?"ok":"warn"}">${d.feedback.state==="submitted"?"Feedback done":d.feedback.state==="filled"?"Filled, submit it":"Feedback pending"}</span>
        ${d.feedback.state!=="submitted"?`<button class="small" data-act="fb" data-s="submitted">I have submitted it</button>`:`<button class="small" data-act="fb" data-s="pending">Back to pending</button>`}</div>`:""}`;
  } else {
    h += d.state==="review" ? `<div class="btnrow"><button class="primary" data-act="finish">Finish &amp; save all drafts</button><button data-act="regen">Regenerate summary</button></div>` : `<div class="mute small">Saved.</div>`;
  }
  return h+"</div>";
}
function filesHtml(d,p){
  if(!d.final) return "";
  const drafts = p.drafts.filter(x=>d.drafts[x.id] && d.files.includes(x.filename));
  return `<div class="card"><h3>${d.state==="decided"?"Saved files &amp; drafts":"Drafts"}</h3>
    ${d.state==="decided"?`<div class="small mute">${d.files.map(esc).join(" · ")}</div>`:`<div class="small mute">Drafts are written to files when you save.</div>`}
    ${(d.state==="decided"?drafts:p.drafts.filter(x=>d.drafts[x.id])).map(x=>`<h4>${esc(x.title)} <button class="small" data-act="copyDraft" data-id="${x.id}">Copy to clipboard</button></h4><pre class="draft">${esc(d.drafts[x.id])}</pre>`).join("")}
    ${d.state==="decided"?`<div class="btnrow" style="margin-top:8px">${PLATFORM.noFolders?"":'<button class="small" data-act="openFolder">Open session folder</button>'}<a href="/api/export/${esc(d.id)}"><button class="small">Export zip</button></a>
      <button class="small ghost" data-act="discard">Delete this session…</button></div>`:`<div class="btnrow" style="margin-top:8px"><button class="small ghost" data-act="discard">Delete this session…</button></div>`}</div>`;
}

/* ============================== chat drawer ============================== */
function renderChat(){
  const el = $("chat"), d = cur(), p = curProfile();
  const wide = window.innerWidth >= 1500, open = !!d && ST.chatOpen;
  el.className = open ? "open" + (wide ? "" : " overlay") : "";
  $("app").classList.toggle("chat-docked", open && wide);
  if(!open){ el.dataset.sig=""; return; }
  const msgs = d.chat||[], L = ST.live;
  const msgSig = JSON.stringify([msgs.map(m=>[m.id,m.status,m.steps,m.state,(m.lines||[]).length,m.text.length]), L&&L.chat_busy, d.context, d.final?1:0, ST.chatMode, ST.suggOpen, d.state, (d.form_data?1:0)]);
  if(!el.dataset.built){
    el.dataset.built="1";
    el.innerHTML = `<header><b class="serif" style="font-size:17px;flex:1">Chat with AI</b><button class="small ghost" data-act="chat">✕</button></header><div id="msgs"></div><div id="ctxbox"></div>
      <div id="chatIn"><div class="suggpop" id="suggPop" hidden></div>
        <div class="seg" id="modeSeg"><button data-act="chatModeSet" data-m="ask">Ask</button><button data-act="chatModeSet" data-m="context">Add context</button>
          <span class="grow"></span><button class="small ghost" id="suggBtn" data-act="suggToggle">💡 Suggestions</button></div>
        <div class="chatrow"><textarea id="chatTxt" rows="2"></textarea><button class="primary" data-act="send">Send</button></div></div>`;
    $("chatTxt").onkeydown = e => { if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); ACT.send(); } };
  }
  if(el.dataset.sig!==msgSig){
    el.dataset.sig = msgSig;
    const box = $("msgs"), near = box.scrollHeight-box.scrollTop-box.clientHeight < 80;
    const q = p.id==="interview" || d.profile==="interview" ? ["How is the candidate doing so far?","What should I ask next?","Was the last answer correct?","Which topics are still uncovered?"] : ["Summarise so far","What did we decide?","What are the open questions?"];
    const extra = d.final && p.has_form ? ["Fill the feedback form","Which feedback is pending?"] : [];
    const sugBtns = cls => [...q,...extra].map(s=>`<button class="${cls}" data-act="sugg" data-q="${esc(s)}">${esc(s)}</button>`).join("");
    const empty = !msgs.length && !(L&&L.chat_busy);
    box.innerHTML = empty ? `<div class="chat-empty"><b class="serif">Ask about this call</b><div class="small mute">Try one of these, or type your own question below.</div>${sugBtns("")}</div>`
      : msgs.map(chatMsgHtml).join("") + (L&&L.chat_busy?'<div class="m assistant"><span class="spin"></span> Thinking…</div>':"");
    if(near) box.scrollTop = box.scrollHeight;
    $("ctxbox").style.display = d.context.length ? "" : "none";
    $("ctxbox").innerHTML = d.context.length ? `<b class="small">Context in use</b>`+d.context.map((c,i)=>`<div class="small">• ${esc(c)} <a href="#" data-act="ctxDel" data-i="${i}">×</a></div>`).join("") : "";
    $("suggPop").hidden = !(ST.suggOpen && !empty); $("suggPop").innerHTML = sugBtns("small");
    $("suggBtn").style.display = empty ? "none" : "";
    for(const b of $("modeSeg").querySelectorAll("button[data-m]")) b.classList.toggle("on", b.dataset.m===ST.chatMode);
    $("chatTxt").placeholder = ST.chatMode==="context" ? "Tell the AI a fact, e.g. he has 5 years of experience…" : "Ask about the call…";
  }
  if(d.chat.some(m=>m.unread)) api("/api/chat_dismiss",{sid:d.id}).then(()=>{ST.rev=-1;});
}
function chatMsgHtml(m){
  if(m.kind==="activity"){
    const last = (m.lines||[]).slice(-5);
    return `<div class="m assistant"><div class="act"><b>${m.state==="running"?'<span class="spin"></span> ':m.state==="done"?"✓ ":"! "}${esc(m.title)}</b> <span class="small mute">· ${m.steps} step${m.steps===1?"":"s"}</span>
      ${last.map(l=>`<div class="ln">${esc(l)}</div>`).join("")}${(m.lines||[]).length>5?`<details><summary class="small">All steps</summary>${m.lines.map(l=>`<div class="ln">${esc(l)}</div>`).join("")}</details>`:""}</div></div>`;
  }
  let extra = "";
  if(m.kind==="permission") extra = m.status==="pending" ? `<div class="btnrow"><button class="primary small" data-act="permYes" data-id="${m.id}">Open and fill</button><button class="small" data-act="permNo" data-id="${m.id}">Not now</button></div>` : `<div class="small mute">${esc(m.status)}</div>`;
  if(m.kind==="pending_list") extra = (m.items||[]).map(i=>`<div class="btnrow"><span>${esc(i.name)}</span><button class="small" data-act="select" data-sid="${esc(i.sid)}">Open</button></div>`).join("");
  return `<div class="m ${m.role}">${md(m.text)}${extra}</div>`;
}

/* ============================== modals ============================== */
function modal(html){ const host=$("modalHost"); host.innerHTML = `<div class="modal"><div class="box">${html}</div></div>`; return host; }
function closeModal(){ $("modalHost").innerHTML=""; }
function confirmBox(title, text, ok="OK", cls="primary"){
  return new Promise(res=>{ modal(`<h3 class="serif">${esc(title)}</h3><p>${text}</p><div class="btnrow" style="justify-content:flex-end"><button id="cNo">Cancel</button><button id="cYes" class="${cls}">${esc(ok)}</button></div>`);
    $("cNo").onclick=()=>{closeModal();res(false)}; $("cYes").onclick=()=>{closeModal();res(true)}; });
}
function consentDialog(){
  return new Promise(res=>{
    const ann = "Heads up: I am recording this call (audio and screen) for my own notes. Please say so now if you are not comfortable with that.";
    modal(`<h3 class="serif">Before you record</h3><label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="cConf" style="width:auto"> Everyone on the call has been told it is being recorded</label>
      <h4>Suggested announcement</h4><div class="act">${esc(ann)}</div><div class="btnrow" style="margin:8px 0"><button class="small" id="cCopy">Copy</button></div>
      <div class="btnrow" style="justify-content:flex-end"><button id="cNo">Cancel</button><button id="cYes" class="primary" disabled>Start recording</button></div>`);
    $("cConf").onchange = e => $("cYes").disabled = !e.target.checked; $("cCopy").onclick = ()=>copyText(ann);
    $("cNo").onclick=()=>{closeModal();res(false)}; $("cYes").onclick=()=>{closeModal();res(true)};
  });
}
async function pickerDialog(){
  return new Promise(async res=>{
    let tab = "windows", data = {windows:[],screens:[]};
    const draw = ()=>{
      const items = tab==="windows" ? data.windows.map(w=>({k:"window",hwnd:w.hwnd,label:w.app+" — "+w.title,title:w.title,sub:w.app,min:w.minimized})) : data.screens.map(s=>({k:"screen",index:s.index,label:s.name,title:s.name,sub:s.width+"×"+s.height}));
      $("pkBody").innerHTML = items.map((x,i)=>`<div class="src" data-i="${i}"><div class="th" style="background-image:url('/api/thumb?kind=${x.k}&${x.k==="window"?"hwnd="+x.hwnd:"index="+x.index}&sid=${encodeURIComponent(ST.sid)}&t=${Date.now()}')"></div>
        <div class="t"><b>${esc(x.sub)}</b></div><div class="t">${esc(x.title)}</div>${x.min?'<div class="small mute">Minimized · will be restored when you pick it</div>':""}</div>`).join("") || '<div class="mute">Nothing found.</div>';
      for(const el of $("pkBody").querySelectorAll(".src")) el.onclick = async ()=>{ const x = items[+el.dataset.i]; if(x.min) await api("/api/restore_window",{hwnd:x.hwnd}); closeModal(); res(x.k==="window"?{kind:"window",hwnd:x.hwnd,label:x.label.slice(0,60)}:{kind:"screen",index:x.index,label:x.label}); };
      for(const b of document.querySelectorAll("#pkTabs button")) b.classList.toggle("on", b.dataset.t===tab);
    };
    const load = async ()=>{ data = await api("/api/targets"); draw(); };
    modal(`<h3 class="serif">Choose what to share</h3><div class="tabs" id="pkTabs"><button data-t="windows">Windows</button><button data-t="screens">Screens</button></div><div class="grid" id="pkBody"><div class="mute">Loading…</div></div>
      <div class="btnrow" style="margin-top:12px;justify-content:flex-end"><button id="pkRef">Refresh</button><button id="pkNo">Cancel</button></div>`);
    for(const b of document.querySelectorAll("#pkTabs button")) b.onclick = ()=>{tab=b.dataset.t;draw()};
    $("pkRef").onclick = load; $("pkNo").onclick = ()=>{closeModal();res(null)}; load();
  });
}
function exhibitDialog(image, token){
  const p = (profileOf(cur().profile).exhibit)||{};
  modal(`<h3 class="serif">Add screenshot</h3><img src="${image}" style="max-width:100%;max-height:320px;border-radius:8px;border:1px solid var(--line)">
    <h4>${esc(p.question_label||"What did you ask?")}</h4><input id="exQ"><h4>${esc(p.note_label||"What did they do or say?")}</h4><input id="exN">
    <div class="btnrow" style="margin-top:12px;justify-content:flex-end"><button id="exNo">Cancel</button><button class="primary" id="exOk">Add</button></div>`);
  $("exQ").focus(); $("exNo").onclick = closeModal;
  $("exOk").onclick = async ()=>{ const body = {sid:ST.sid, question:$("exQ").value, note:$("exN").value}; if(token) body.token=token; else body.image=image; closeModal(); const r = await api("/api/exhibit", body); if(r.error) toast(r.error); else toast("Screenshot added"); ST.rev=-1; };
}
function readFile(file){ const r = new FileReader(); r.onload = ()=>exhibitDialog(r.result, null); r.readAsDataURL(file); }
document.addEventListener("paste", e => {
  if(viewMode()!=="live" && viewMode()!=="review") return;
  for(const it of (e.clipboardData||{}).items||[]) if(it.type.startsWith("image/")){ readFile(it.getAsFile()); e.preventDefault(); return; }
});

/* ============================== actions ============================== */
const ACT = {
  new(){ selectSession(null); },
  select(ds){ selectSession(ds.sid); },
  async dismiss(ds, el, ev){ ev.stopPropagation(); await api("/api/discard",{sid:ds.sid,delete:false}); if(ST.sid===ds.sid) selectSession(null); poll(); },
  toggle(ds){ if(ST.collapsed.has(ds.k)) ST.collapsed.delete(ds.k); else ST.collapsed.add(ds.k); LS.set("dbf.collapsed",[...ST.collapsed]); $("sbList").dataset.sig=""; renderSide(); },
  openSide(){ $("side").classList.add("open"); $("scrim").classList.add("open"); },
  closeSide(){ $("side").classList.remove("open"); $("scrim").classList.remove("open"); },
  chat(){ ST.chatOpen = !ST.chatOpen; LS.set("dbf.chat", ST.chatOpen); $("chat").dataset.sig=""; $("top").dataset.sig=""; render(); },
  async start(){
    if(PLATFORM.beforeStart && !(await PLATFORM.beforeStart())) return;
    const body = {profile:$("stProfile").value, subject:$("stName").value, role:$("stRole").value.trim(), context:$("stCtx").value, extra:$("stExtra").style.display==="none"?"":$("stExtra").value};
    if(PLATFORM.speakers && $("stSpk")) body.speakers = $("stSpk").checked;
    let r = await api("/api/start", body);
    if(r.error==="live"){ if(!await confirmBox("A session is live", `“${esc(r.live_name)}” is still live. End it and start the new one? Its summary keeps generating in the sidebar.`, "End it and start")) return; r = await api("/api/start",{...body,end_current:true}); }
    if(r.error) return toast(r.error);
    selectSession(r.sid);
  },
  spkRename(ds){
    const d = cur(), spk = spkInfo(), id = +ds.id;
    modal(`<h3 class="serif">Who is ${esc(spkName(spk, id))}?</h3><p class="small mute">The name is used in the transcript, the analysis and the report. Voice recognition can make mistakes: give two speakers the same name if they are the same person.</p>
      <input id="spkNm" maxlength="40" placeholder="Name" value="${esc(spk.names[String(id)]||"")}">
      <div class="btnrow" style="justify-content:flex-end;margin-top:10px"><button id="spkNo">Cancel</button><button class="primary" id="spkOk">Save</button></div>`);
    $("spkNm").focus(); $("spkNo").onclick = closeModal;
    $("spkNm").onkeydown = e => { if(e.key==="Enter") $("spkOk").click(); };
    $("spkOk").onclick = async () => { const name = $("spkNm").value; closeModal(); await api("/api/speaker_name",{sid:ST.sid,id,name}); ST.rev=-1; poll(); };
  },
  async listen(){ const L=ST.live; await api("/api/listen",{sid:ST.sid,on:!L.listening}); poll(); },
  async pause(){ await api("/api/pause",{sid:ST.sid,on:!ST.live.paused}); poll(); },
  async analyze(){ await api("/api/analyze",{sid:ST.sid}); toast("Analysing…"); },
  async end(){ if(await confirmBox("End this session?","The final analysis is written afterwards. Recordings stop.","End session","bad")){ await api("/api/end",{sid:ST.sid}); poll(); } },
  pop(ds,el,ev){ ev.preventDefault(); ev.stopPropagation(); const k=ds.p; const was=ST.pops[k]; ST.pops = {}; if(!was) ST.pops[k]=true; $("bar").dataset.sig=""; renderBar(); },
  recopt(ds,el){ ST.recOpts[ds.k]=el.checked; LS.set("dbf.rec",ST.recOpts); },
  async interval(ds){ await api("/api/watch_interval",{sid:ST.sid,seconds:+ds.s}); ST.pops={}; poll(); },
  async custint(ds,el){ const v=+el.value; if(v>=5&&v<=3600){ await api("/api/watch_interval",{sid:ST.sid,seconds:v}); ST.pops={}; poll(); } else toast("Enter 5 to 3600 seconds"); },
  async watch(){
    const L = ST.live; if(L.watch.on){ await api("/api/watch",{sid:ST.sid,on:false}); return poll(); }
    if(!LS.get("dbf.watchOk",false)){ if(!await confirmBox("Let the AI watch your screen?","Every few seconds the app looks at the chosen window. A screenshot is sent to the AI whenever something new is on screen, and every one is kept with the session. It stops when the call ends or is paused.","Start watching")) return; LS.set("dbf.watchOk",true); }
    let src = L.source; if(!src){ src = await pickerDialog(); if(!src) return; }
    await api("/api/watch",{sid:ST.sid,on:true,source:src}); poll();
  },
  async picker(){ const s = await pickerDialog(); if(s){ await api("/api/watch",{sid:ST.sid,on:ST.live.watch.on,source:s}); ST.pv=0; poll(); } },
  async capture(){
    let src = ST.live.source; if(!src){ src = await pickerDialog(); if(!src) return; }
    const r = await api("/api/capture_screen",{sid:ST.sid,source:src}); if(r.error) return toast(r.error);
    exhibitDialog(r.image, r.token);
  },
  addShot(){ ST.pops={}; $("shotFile").click(); },
  async record(){
    const rec = (cur().clips||[]).find(c=>c.state==="recording"||c.state==="paused");
    if(rec){ await api("/api/record",{sid:ST.sid,action:"stop"}); return poll(); }
    const body = {sid:ST.sid,action:"start",audio:ST.recOpts.audio,video:ST.recOpts.video,source:ST.live.source};
    let r = await api("/api/record", body);
    if(r.need_consent){ if(!await consentDialog()) return; r = await api("/api/record",{...body,consent:true}); }
    if(r.error) return toast(r.error);
    toast("Recording started"); poll();
  },
  async exRetry(ds){ await api("/api/exhibit_retry",{sid:ST.sid,n:+ds.n}); ST.rev=-1; poll(); },
  async exDel(ds){ if(await confirmBox("Delete this screenshot?","It is removed from the analysis.","Delete","bad")){ await api("/api/exhibit_delete",{sid:ST.sid,n:+ds.n}); ST.rev=-1; poll(); } },
  async retry(){ const r=await api("/api/retry",{sid:ST.sid}); if(r.error) toast(r.error); ST.rev=-1; poll(); },
  async regen(){ await api("/api/regenerate",{sid:ST.sid}); ST.rev=-1; poll(); },
  async decide(ds){ const r = await api("/api/decision",{sid:ST.sid,decision:ds.d}); if(r.error) toast(r.error); else toast("Saved"); ST.rev=-1; poll(); },
  async finish(){ await api("/api/finish",{sid:ST.sid}); toast("Saved"); ST.rev=-1; poll(); },
  async switch(ds){ const d=cur(); if(d.decision===ds.d) return; if(await confirmBox("Change the decision?",`Switch to “${esc(ds.label)}”? The report, result, matching draft and day summary are rewritten. Nothing is changed on any website.`,"Change")){ await api("/api/decision",{sid:ST.sid,decision:ds.d}); ST.rev=-1; poll(); } },
  async fb(ds){ await api("/api/feedback",{sid:ST.sid,state:ds.s}); ST.rev=-1; poll(); },
  async discard(){ if(await confirmBox("Delete this session?","The session folder is deleted from disk. This cannot be undone.","Delete","bad")){ await api("/api/discard",{sid:ST.sid,delete:true}); selectSession(null); } },
  async openFolder(){ const r=await api("/api/open_folder",{sid:ST.sid}); if(r.error) toast(r.error); },
  async recOpen(ds){ await api("/api/recording_open",{sid:ST.sid,n:+ds.n}); },
  async recDel(ds){ if(await confirmBox("Delete this recording?","The video/audio files are deleted.","Delete","bad")){ await api("/api/recording_delete",{sid:ST.sid,n:+ds.n}); ST.rev=-1; poll(); } },
  async recDelAll(){ if(await confirmBox("Delete all recordings?","All video and audio files of this session are deleted.","Delete all","bad")){ await api("/api/recording_delete",{sid:ST.sid,all:true}); ST.rev=-1; poll(); } },
  copyField(ds){ copyText(cur().form_fields[+ds.i].value); },
  copyForm(){ copyText(cur().form_text); },
  dlForm(){ download("evaluation-form.md", cur().form_text); },
  copyDraft(ds){ copyText(cur().drafts[ds.id]); },
  async formWrite(){ const r=await api("/api/form",{sid:ST.sid}); if(r.error) toast(r.error); else toast("Writing the form answers…"); ST.rev=-1; poll(); },
  async fillGo(){
    const url = ($("portal")||{}).value || ST.portal; ST.portal = url; LS.set("dbf.portal", url);
    const r = await api("/api/fill_form",{sid:ST.sid,url}); if(r.error) toast(r.error); else { toast("Opening the form window…"); if(!ST.chatOpen){ST.chatOpen=true;LS.set("dbf.chat",true);} } ST.rev=-1; poll();
  },
  async fillStop(){ await api("/api/fill_cancel",{sid:ST.sid}); poll(); },
  chatModeSet(ds){ ST.chatMode = ds.m; $("chat").dataset.sig=""; renderChat(); $("chatTxt").focus(); },
  suggToggle(){ ST.suggOpen = !ST.suggOpen; $("chat").dataset.sig=""; renderChat(); },
  async send(){ const t=$("chatTxt"); const text=t.value.trim(); if(!text) return; t.value=""; await api("/api/chat",{sid:ST.sid,text,mode:ST.chatMode}); ST.rev=-1; poll(); },
  async sugg(ds){ ST.chatMode="ask"; ST.suggOpen=false; await api("/api/chat",{sid:ST.sid,text:ds.q,mode:"ask"}); ST.rev=-1; poll(); },
  async ctxDel(ds,el,ev){ ev.preventDefault(); await api("/api/context_remove",{sid:ST.sid,index:+ds.i}); ST.rev=-1; poll(); },
  async permYes(ds){ const r = await api("/api/fill_form",{sid:ST.sid,url:ST.portal||ST.cfg.form_url}); if(r.error) toast(r.error); ST.rev=-1; poll(); },
  async permNo(ds){ await api("/api/chat_dismiss",{sid:ST.sid,id:+ds.id}); ST.rev=-1; poll(); },
};
Object.assign(ACT, PLATFORM.act || {});
document.addEventListener("click", e => {
  const t = e.target.closest("[data-act]");
  if(!t){ if(Object.keys(ST.pops).length && !e.target.closest(".pop")){ ST.pops={}; if($("bar")){$("bar").dataset.sig=""; if(viewMode()==="live") renderBar();} } return; }
  if(t.dataset.act==="pop") { ACT.pop(t.dataset,t,e); return; }
  const fn = ACT[t.dataset.act]; if(fn){ if(t.tagName==="A") e.preventDefault(); fn(t.dataset, t, e); }
});
document.addEventListener("change", e => { const t = e.target.closest("[data-change]"); if(t && ACT[t.dataset.change]) ACT[t.dataset.change](t.dataset, t, e); });
$("modeSel").onchange = e => { ST.mode = e.target.value; LS.set("dbf.mode", ST.mode); $("sbList").dataset.sig=""; renderSide(); };
window.addEventListener("resize", ()=>{ $("chat").dataset.sig=""; render(); });

/* ============================== boot ============================== */
(async function boot(){
  if(PLATFORM.beforeBoot && !(await PLATFORM.beforeBoot())) return;      // e.g. the server edition shows sign-in first
  ST.cfg = await api("/api/config");
  const profs = await api("/api/profiles"); for(const p of profs) ST.profiles[p.id] = p;
  await poll(); setInterval(poll, 1000);
})();
