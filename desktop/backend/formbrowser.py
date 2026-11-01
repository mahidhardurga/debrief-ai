"""In-app browser worker: a WebView2 window (pywebview) that opens the portal and fills the evaluation form.

Started by formfill.py as a separate process (pywebview must own the main thread) with one argument:
the work folder holding job.json / status.json / control.json.

Safety rules, in code:
  * it never clicks Submit/Save, never types passwords, never touches the photo question,
    self-declaration, interviewer, dates or attachments;
  * the only things it clicks are the exact "Provide/Give/Enter/Add/Start/Complete (Interview) Feedback"
    link or button and, when needed, the one link Claude picks that opens the candidate's page (links
    about withdraw/reject/decline/cancel/delete/reschedule/sign out/log out/remove are filtered out);
  * the window stays open afterwards so the user reviews the form and presses Submit themselves.
This uses WebView2, not Playwright/Chrome remote debugging, which employer policy may block.
"""
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "shared" / "core"))   # analyzer.py, reports.py
import webview

import analyzer
import config

WORK = Path(sys.argv[1])
JOB = json.loads((WORK / "job.json").read_text(encoding="utf-8"))
STATUS = {"state": "starting", "log": [], "report": "", "error": "", "acks": [], "steps": 0}
CLOSED = threading.Event()
BAD_LINK = re.compile(r"withdraw|reject|decline|cancel|delete|reschedule|sign out|log out|logout|remove", re.I)
FEEDBACK_RE = r"^\s*(provide|give|enter|add|start|complete)\s+(interview\s+)?feedback\s*$"
_write_lock = threading.Lock()


def save_status():
    """Write status.json with retry: Windows refuses to replace a file the server is reading."""
    with _write_lock:
        tmp = WORK / "status.json.tmp"
        for _ in range(20):
            try:
                tmp.write_text(json.dumps(STATUS, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, WORK / "status.json")
                return
            except OSError:
                time.sleep(0.1)


def log(msg):
    STATUS["log"].append(msg)
    STATUS["steps"] = len(STATUS["log"])
    save_status()


def set_state(state, **kw):
    STATUS["state"] = state
    STATUS.update(kw)
    save_status()


# --------------------------------------------------------------------------- page scripts
JS_INFO = r"""(()=>{
 const vis=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
 const isSearch=el=>el.type==='search'||/search/i.test((el.name||'')+(el.placeholder||'')+(el.id||'')+(el.getAttribute('aria-label')||''));
 const c=[...document.querySelectorAll('select,textarea,input[type=radio],input[type=text],input:not([type]),[role=combobox]')]
   .filter(el=>!isSearch(el)&&(el.tagName==='SELECT'||vis(el)));
 return JSON.stringify({url:location.href,title:document.title,text:document.body?document.body.innerText.slice(0,8000):'',controls:c.length});
})()"""

JS_SAME_WINDOW = r"""(()=>{ if(window.__dbfSame) return 1; window.__dbfSame=1;
 window.open=function(u){ if(u) location.href=u; return window; };
 document.addEventListener('click',e=>{const a=e.target.closest&&e.target.closest('a'); if(a&&a.target&&a.target!=='_self') a.target='_self';},true);
 document.querySelectorAll('a[target]').forEach(a=>a.target='_self'); return 1; })()"""

JS_CLICK_FEEDBACK = r"""((tokens, opened)=>{
 const re=new RegExp(%s,'i');
 const cands=[...document.querySelectorAll('a,button,input[type=button]')].filter(el=>{const t=(el.innerText||el.value||'').trim(); return re.test(t)&&!/submit|save/i.test(t);});
 for(const el of cands){ const row=el.closest('tr,li,[role=row],.row,.list-item,.card'); const rt=((row&&row.innerText)||'').toLowerCase();
   if(row&&tokens.every(t=>rt.includes(t))){ el.click(); return 'row'; } }
 if(opened&&cands.length===1){ cands[0].click(); return 'page'; }
 return '';
})""" % json.dumps(FEEDBACK_RE)

JS_LINKS = r"""(()=>{ const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0};
 const out=[]; let i=0; for(const a of document.querySelectorAll('a')){ const t=(a.innerText||'').replace(/\s+/g,' ').trim(); if(!t||!vis(a)||!a.href) continue;
   a.setAttribute('data-dbref','L'+i); out.push({ref:'L'+i,text:t.slice(0,120),href:a.href.slice(0,160)}); i++; if(i>=200) break; }
 return JSON.stringify(out); })()"""

JS_EXTRACT = r"""(()=>{
 const txt=s=>(s||'').replace(/\s+/g,' ').trim();
 const vis=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
 const isSearch=el=>el.type==='search'||/search/i.test((el.name||'')+(el.placeholder||'')+(el.id||'')+(el.getAttribute('aria-label')||''));
 const heads=[...document.querySelectorAll('h1,h2,h3,h4,h5,h6,legend,[role=heading]')];
 const headingFor=el=>{let best='';for(const h of heads){ if(h.compareDocumentPosition(el)&Node.DOCUMENT_POSITION_FOLLOWING) best=txt(h.innerText); else break;} return best;};
 const isWidget=n=>n.matches&&n.matches('input,select,textarea,button')||(n.querySelector&&n.querySelector('input,select,textarea'));
 const captionFor=el=>{
   if(el.id){const l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if(l) return txt(l.innerText);}
   const al=el.getAttribute('aria-label'); if(al) return txt(al);
   const lb=el.getAttribute('aria-labelledby'); if(lb){const e=document.getElementById(lb.split(' ')[0]); if(e) return txt(e.innerText);}
   const wrap=el.closest('label'); if(wrap) return txt(wrap.innerText);
   let n=el; for(let i=0;i<6&&n;i++){ let p=n.previousElementSibling; while(p){ if(!isWidget(p)){const t=txt(p.innerText); if(t&&t.length<300) return t;} p=p.previousElementSibling;} n=n.parentElement; }
   return '';};
 const out=[]; let idx=0; const seen=new Set();
 const key=(kind,el)=>kind+'|'+(el.name||el.id||'')+'|'+([...document.querySelectorAll(el.tagName+(el.name?'[name="'+el.name+'"]':''))].indexOf(el));
 for(const el of document.querySelectorAll('select')){
   if(el.disabled||el.options.length<2) continue;
   const ref='F'+(idx++); el.setAttribute('data-dbref',ref);
   const sel=el.options[el.selectedIndex];
   out.push({ref,key:key('select',el),type:'select',label:captionFor(el),heading:headingFor(el),options:[...el.options].map(o=>txt(o.text)).filter(Boolean).slice(0,60),value:(el.value&&sel)?txt(sel.text):'',visible:vis(el)});
 }
 for(const el of document.querySelectorAll('textarea,input[type=text],input:not([type])')){
   if(el.disabled||el.readOnly||!vis(el)||isSearch(el)||el.getAttribute('role')==='combobox'||el.type==='hidden') continue;
   const ref='F'+(idx++); el.setAttribute('data-dbref',ref);
   out.push({ref,key:key(el.tagName==='TEXTAREA'?'textarea':'text',el),type:el.tagName==='TEXTAREA'?'textarea':'text',label:captionFor(el),heading:headingFor(el),options:[],value:el.value||''});
 }
 const groups={}; for(const el of document.querySelectorAll('input[type=radio]')){ if(el.disabled||!(vis(el)||el.closest('label'))) continue; (groups[el.name||el.id]=groups[el.name||el.id]||[]).push(el); }
 for(const name in groups){ const rs=groups[name]; const ref='R'+(idx++);
   const optLabel=r=>{ if(r.id){const l=document.querySelector('label[for="'+CSS.escape(r.id)+'"]'); if(l) return txt(l.innerText);} const w=r.closest('label'); if(w) return txt(w.innerText); const s=r.nextSibling; return txt((s&&s.textContent)||r.value);};
   rs.forEach((r,j)=>r.setAttribute('data-dbref',ref+'.'+j));
   const opts=rs.map(optLabel); let caption='';
   const fs=rs[0].closest('fieldset'); if(fs&&fs.querySelector('legend')) caption=txt(fs.querySelector('legend').innerText);
   if(!caption){ let a=rs[0].parentElement; while(a&&!rs.every(r=>a.contains(r))) a=a.parentElement; if(a){ for(const e of a.querySelectorAll('label,legend,span,p,div,b,strong')){ if(e.querySelector('input')||(e.htmlFor&&rs.some(r=>r.id===e.htmlFor))) continue; if(!(rs[0].compareDocumentPosition(e)&Node.DOCUMENT_POSITION_PRECEDING)) continue; const t=txt(e.innerText); if(t&&t.length<300&&!opts.includes(t)){caption=t;break;} } } }
   const chk=rs.findIndex(r=>r.checked);
   out.push({ref,key:'radio|'+name,type:'radio',label:caption||captionFor(rs[0]),heading:headingFor(rs[0]),options:opts,value:chk>=0?opts[chk]:''});
 }
 for(const el of document.querySelectorAll('[role=combobox]')){
   if(el.tagName==='SELECT'||!vis(el)) continue;
   const ref='F'+(idx++); el.setAttribute('data-dbref',ref);
   out.push({ref,key:key('combo',el),type:'combo',label:captionFor(el),heading:headingFor(el),options:[],value:txt(el.value||el.innerText||'')});
 }
 return JSON.stringify(out);
})()"""

JS_APPLY = r"""((ref,value)=>{
 const norm=s=>(s||'').toLowerCase().replace(/[^a-z0-9]+/g,' ').trim();
 const best=(opts,v)=>{const nv=norm(v); let i=opts.findIndex(o=>norm(o)===nv); if(i>=0) return i;
   const lead=nv.match(/^\d+/); if(lead){ i=opts.findIndex(o=>{const n=norm(o); return n===lead[0]||n.startsWith(lead[0]+' ');}); if(i>=0) return i; }
   i=opts.findIndex(o=>norm(o).startsWith(nv)); if(i>=0) return i;
   i=opts.findIndex(o=>nv&&norm(o).includes(nv)); if(i>=0) return i;
   return opts.findIndex(o=>norm(o).length>2&&nv.includes(norm(o)));};
 const fire=(el,types)=>types.forEach(t=>el.dispatchEvent(new Event(t,{bubbles:true})));
 if(ref[0]==='R'){ const rs=[...document.querySelectorAll('[data-dbref^="'+ref+'."]')]; if(!rs.length) return 'missing';
   const opts=rs.map(r=>{ if(r.id){const l=document.querySelector('label[for="'+CSS.escape(r.id)+'"]'); if(l) return l.innerText;} const w=r.closest('label'); return w?w.innerText:r.value; });
   const i=best(opts,value); if(i<0) return 'nomatch'; rs[i].click(); return 'ok'; }
 const el=document.querySelector('[data-dbref="'+ref+'"]'); if(!el) return 'missing';
 if(el.tagName==='SELECT'){ const opts=[...el.options].map(o=>o.text); const i=best(opts,value); if(i<0) return 'nomatch';
   el.selectedIndex=i; fire(el,['input','change']); if(window.jQuery) window.jQuery(el).trigger('change'); return 'ok'; }
 if(el.getAttribute('role')==='combobox'&&el.tagName!=='INPUT'&&el.tagName!=='TEXTAREA'){ el.click(); return 'opened'; }
 const proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
 Object.getOwnPropertyDescriptor(proto,'value').set.call(el,value); fire(el,['input','change','blur']); return 'ok';
})"""

JS_OPTIONS = r"""(()=>{ const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0};
 return JSON.stringify([...document.querySelectorAll('[role=option],li[role=option],.select2-results__option,.choices__item--choice')].filter(vis).map(o=>(o.innerText||'').replace(/\s+/g,' ').trim()).filter(Boolean).slice(0,60)); })()"""

JS_PICK_OPTION = r"""((value)=>{ const norm=s=>(s||'').toLowerCase().replace(/[^a-z0-9]+/g,' ').trim(); const nv=norm(value);
 const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0};
 const os=[...document.querySelectorAll('[role=option],.select2-results__option,.choices__item--choice')].filter(vis);
 let o=os.find(x=>norm(x.innerText)===nv)||os.find(x=>norm(x.innerText).startsWith(nv))||os.find(x=>nv&&norm(x.innerText).includes(nv));
 if(!o){ const lead=nv.match(/^\d+/); if(lead) o=os.find(x=>norm(x.innerText).startsWith(lead[0]+' ')); }
 if(!o) return 'nomatch'; o.click(); return 'ok'; })"""

JS_CLOSE_COMBO = "(()=>{document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));if(document.activeElement)document.activeElement.blur();return 1})()"


# --------------------------------------------------------------------------- worker
class Worker:
    def __init__(self, window):
        self.w = window
        self.name = JOB["name"]
        self.tokens = [t for t in re.findall(r"[a-z0-9]+", self.name.lower()) if len(t) > 1]
        self.opened_candidate = False
        self.done_keys = set()

    def js(self, code, default=None):
        try:
            return self.w.evaluate_js(code)
        except Exception:
            return default

    def js_json(self, code, default=None):
        raw = self.js(code)
        try:
            return json.loads(raw) if raw else default
        except (TypeError, ValueError):
            return default

    def call(self, fn_code, *args):
        return self.js(f"({fn_code})({', '.join(json.dumps(a) for a in args)})")

    # ---- navigation ----------------------------------------------------------
    def page_info(self):
        return self.js_json(JS_INFO, {"url": "", "title": "", "text": "", "controls": 0})

    def is_candidate_form(self, info):
        text = info["text"].lower()
        first = self.tokens[0] if self.tokens else ""
        return bool(first) and first in text and info["controls"] >= 3

    def navigate_to_form(self):
        """Wait for the form page: click only the exact Feedback link, or the one candidate link Claude picks."""
        deadline = time.time() + 300
        set_state("waiting")
        told = False
        while time.time() < deadline and not CLOSED.is_set():
            info = self.page_info()
            self.js(JS_SAME_WINDOW)
            if self.is_candidate_form(info):
                return True
            clicked = self.call(JS_CLICK_FEEDBACK, self.tokens, self.opened_candidate)
            if clicked:
                log("Clicked the Provide Feedback link" + (" in the candidate's row" if clicked == "row" else ""))
                time.sleep(3)
                continue
            if not self.opened_candidate:
                links = [l for l in (self.js_json(JS_LINKS, []) or []) if not BAD_LINK.search(l["text"])]
                mine = [l for l in links if all(t in l["text"].lower() for t in self.tokens[:1])]
                if mine:
                    try:
                        pick = analyzer.pick_candidate_link(self.name, mine[:60])
                    except Exception as exc:
                        pick = {"ref": None}
                        log(f"Link picker failed: {str(exc)[:80]}")
                    ref = pick.get("ref")
                    chosen = next((l for l in mine if l["ref"] == ref), None)
                    if chosen:
                        log(f"Opening the candidate page: {chosen['text'][:60]}")
                        self.js(f"document.querySelector('[data-dbref=\"{ref}\"]').click()")
                        self.opened_candidate = True
                        time.sleep(3)
                        continue
            if not told:
                log("Waiting for the candidate's form. Sign in or open the candidate yourself if needed.")
                told = True
            time.sleep(3)
        return False

    # ---- fields ----------------------------------------------------------------
    def extract(self):
        return self.js_json(JS_EXTRACT, []) or []

    def probe_combos(self, fields):
        for f in fields:
            if f["type"] == "combo" and not f["options"]:
                if self.call(JS_APPLY, f["ref"], "") == "opened":
                    time.sleep(0.4)
                    f["options"] = self.js_json(JS_OPTIONS, []) or []
                    self.js(JS_CLOSE_COMBO)
                    time.sleep(0.2)

    def apply(self, field, value):
        res = self.call(JS_APPLY, field["ref"], value)
        if res == "opened":  # custom combobox: open, pick, done
            time.sleep(0.4)
            res = self.call(JS_PICK_OPTION, value)
        return res == "ok"

    def fill_rounds(self):
        set_state("filling")
        filled = 0
        notes = []
        for rnd in range(1, 9):
            fields = self.extract()
            todo = [f for f in fields if f["key"] not in self.done_keys and not f.get("value")
                    and not re.search(r"photo", f["label"] + f["heading"], re.I)]
            if not todo:
                break
            self.probe_combos(todo)
            log(f"Round {rnd}: asking Claude about {len(todo)} field(s)")
            slim = [{k: f[k] for k in ("ref", "label", "heading", "type", "options", "value")} for f in todo]
            try:
                plan = analyzer.plan_form_fill(JOB["answers"], slim, JOB["form"])
            except Exception as exc:
                set_state("error", error=f"Claude could not plan the fill: {exc}")
                return None
            if plan.get("notes"):
                notes.append(plan["notes"])
            by_ref = {f["ref"]: f for f in todo}
            n_ok = 0
            for act in plan.get("actions", []):
                f = by_ref.get(act.get("ref"))
                if not f:
                    continue
                self.done_keys.add(f["key"])
                if self.apply(f, act.get("value", "")):
                    n_ok += 1
                    log(f"Set {(f['label'] or f['heading'] or f['ref'])[:50]}")
                else:
                    log(f"Could not set {(f['label'] or f['ref'])[:50]}")
            for f in todo:
                self.done_keys.add(f["key"])
            filled += n_ok
            # wait up to 5 s for newly revealed fields (e.g. sub-behaviour dropdowns)
            t0, before = time.time(), len(self.extract())
            while time.time() - t0 < 5 and len(self.extract()) <= before:
                time.sleep(0.5)
            if n_ok == 0 and len(self.extract()) <= before:
                break
        return filled, notes

    def still_empty(self):
        out = []
        for f in self.extract():
            if f["type"] in ("select", "radio", "combo") and not f.get("value") and not re.search(r"photo", f["label"] + f["heading"], re.I):
                out.append((f["label"] or f["heading"] or f["ref"])[:70])
        return out

    # ---- control channel -------------------------------------------------------
    def control_loop(self):
        last = 0
        ctl = WORK / "control.json"
        while not CLOSED.is_set():
            time.sleep(1.0)
            try:
                data = json.loads(ctl.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("seq", 0) <= last:
                continue
            last = data["seq"]
            for ch in data.get("set", []):
                text = self.change_one(ch.get("field", ""), ch.get("value", ""))
                STATUS["acks"].append({"seq": last, "text": text})
            save_status()

    def change_one(self, field, value):
        want = set(re.findall(r"[a-z0-9]+", field.lower()))
        best, best_score = None, 0.0
        for f in self.extract():
            have = set(re.findall(r"[a-z0-9]+", (f["heading"] + " " + f["label"]).lower()))
            if not want or not have:
                continue
            score = len(want & have) / len(want)
            if score > best_score:
                best, best_score = f, score
        if not best or best_score < 0.6:
            return f"I could not find a field matching “{field}” in the open form."
        if self.apply(best, value):
            return f"Updated “{(best['label'] or best['heading'])[:60]}” to “{value[:80]}”. Please check it in the window."
        return f"Found “{(best['label'] or best['heading'])[:60]}” but could not set it to “{value[:60]}”."

    # ---- main ------------------------------------------------------------------
    def run(self):
        try:
            time.sleep(1.5)
            if not self.navigate_to_form():
                if not CLOSED.is_set():
                    set_state("error", error="I could not reach the candidate's form within 5 minutes. Open it in the window yourself and try again.")
                return
            log("On the candidate's form")
            res = self.fill_rounds()
            if res is None:
                return
            filled, notes = res
            empty = self.still_empty()
            report = f"Filled {filled} fields. The window is still open: review it, then submit it yourself."
            if notes:
                report += "\n\n" + "\n".join(notes)
            if empty:
                report += "\n\nStill empty on the page: " + "; ".join(empty[:15])
            set_state("done", report=report)
            self.control_loop()
        except Exception as exc:
            set_state("error", error=f"Form filler crashed: {exc}")


def main():
    config.FORM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    save_status()
    window = webview.create_window(f"Evaluation form - {JOB['name']}", JOB["url"], width=1200, height=850)
    window.events.closed += lambda: CLOSED.set()
    worker = Worker(window)
    webview.start(lambda: threading.Thread(target=worker.run, daemon=True).start(), gui="edgechromium",
                  private_mode=False, storage_path=str(config.FORM_PROFILE_DIR))
    CLOSED.set()
    if STATUS["state"] not in ("done", "error"):
        set_state("error", error="The form window was closed before the fill finished.")


if __name__ == "__main__":
    main()
