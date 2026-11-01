"""Mock hiring portal for testing the form filler. Port 5059 (Chromium blocks 5060).

/list   candidates with a search box and name links
/cand/x profile page with a "Provide Feedback" link (opens in a new tab), a decoy Submit and a Withdraw link
/form   evaluation form: radio whose choice reveals a dropdown, a textarea, a real <select> hidden behind a
        custom widget, a photo question that must never be touched, and Submit/Save decoys
/hits   what was clicked and which values the page holds (the test reads this)
"""
import json
import sys

from flask import Flask, request, jsonify

app = Flask(__name__)
HITS, VALUES = [], {}

PAGE = """<!doctype html><meta charset=utf-8><title>{title}</title><body style="font-family:sans-serif;padding:20px">{body}
<script>
document.addEventListener('change',e=>{{const t=e.target; if(!t.name) return; const v=(t.type==='radio')?(t.checked?t.value:null):t.value; if(v!==null) fetch('/set',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{name:t.name,value:v}})}});}});
</script></body>"""


@app.get("/list")
def list_page():
    return PAGE.format(title="Interviews", body="""<h1>Upcoming interviews</h1><input type="search" placeholder="Search candidates" name="q">
      <table border=1 cellpadding=6><tr><td><a href="/cand/asha">Verma, Asha</a></td><td>Java</td><td><a href="/form?c=asha">Provide Feedback</a></td></tr>
      <tr><td><a href="/cand/ravi">Kumar, Ravi</a></td><td>Java</td><td></td></tr></table>""")


@app.get("/cand/<c>")
def cand(c):
    return PAGE.format(title="Candidate", body=f"""<h1>Ravi Kumar</h1><p>Interview scheduled</p>
      <a class="btn" target="_blank" style="background:#06c;color:#fff;padding:8px" href="/form?c={c}">Provide Feedback</a>
      <button onclick="fetch('/hit/submit')">Submit</button> <a href="/hit/withdraw">Withdraw candidate</a>""")


@app.get("/form")
def form():
    return PAGE.format(title="Evaluation", body="""<h1>Feedback for Ravi Kumar</h1>
      <h2>Photo verification</h2><fieldset><legend>Does the photo match the candidate?</legend>
        <label><input type=radio name=photo value=Yes> Yes</label> <label><input type=radio name=photo value=No> No</label></fieldset>
      <h2>Behavioral competencies</h2><fieldset><legend>Analytical Ability</legend>
        <label><input type=radio name=analytical value=Beginner onclick="reveal()"> Beginner</label>
        <label><input type=radio name=analytical value=Intermediate onclick="reveal()"> Intermediate</label>
        <label><input type=radio name=analytical value=Advanced onclick="reveal()"> Advanced</label>
        <label><input type=radio name=analytical value=Expert onclick="reveal()"> Expert</label></fieldset>
      <div id=sub style="display:none"><label for=logic>Logical Reasoning</label>
        <select id=logic name=logic><option value="">Select</option><option>1 - Did not meet expectations</option><option>2 - Met expectations with reservations</option><option>3 - Met expectations</option><option>4 - Exceeded expectations</option></select></div>
      <h2>Overall</h2><label for=probe>Areas to Probe</label><br><textarea id=probe name=probe rows=3></textarea><br>
      <label for=rating>Overall Feedback Rating</label>
      <div class="select2-fake" style="border:1px solid #999;padding:4px;display:inline-block">custom widget</div>
      <select id=rating name=rating style="display:none"><option value="">--</option><option>1 - Did not meet expectations,</option><option>2 - Met expectations with reservations</option><option>3 - Met expectations</option><option>4 - Exceeded expectations</option><option>5 - Significantly exceeded expectations</option></select><br>
      <label for=cat>Applicable Category</label><select id=cat name=cat><option value="">Select</option><option>No further interest/Reject</option><option>Proceed to next round of an interview</option></select><br><br>
      <button id=save onclick="fetch('/hit/save')">Save</button> <button id=submit onclick="fetch('/hit/submit')">Submit</button>
      <script>function reveal(){setTimeout(()=>{document.getElementById('sub').style.display='block'},700)}</script>""")


@app.post("/set")
def set_value():
    d = request.get_json()
    VALUES[d["name"]] = d["value"]
    return "ok"


@app.get("/hit/<what>")
def hit(what):
    HITS.append(what)
    return "hit"


@app.get("/hits")
def hits():
    return jsonify({"hits": HITS, "values": VALUES})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 5059)
