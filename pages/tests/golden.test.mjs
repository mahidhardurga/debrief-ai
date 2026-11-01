// The browser port must build exactly the prompts, schemas and reports the Python code builds (golden.json comes from make_golden.py).
import assert from "node:assert/strict";
import fs from "node:fs";
import * as A from "../engine/analyzer.js";
import * as R from "../engine/reports.js";

const golden = JSON.parse(fs.readFileSync(new URL("./golden.json", import.meta.url), "utf-8"));
const fns = { ...A, ...R };
const norm = (x) => JSON.parse(JSON.stringify(x === undefined ? null : x));
let failed = 0;
for (const c of golden.cases) {
  try {
    A.cfg.MOCK_ANALYZER = !!c.mock;
    const calls = [], canned = structuredClone(c.canned || []);
    A.set_backend((prompt, system, schema, model, images) => { calls.push({ prompt, system, schema, images: (images || []).length }); return structuredClone(canned.shift()); });
    const args = structuredClone(c.args);
    const result = await fns[c.fn](...args);
    assert.deepEqual(norm(result), norm(c.expected.result), "result differs");
    if (!c.pure) {
      assert.deepEqual(norm(calls), norm(c.expected.calls.map(({ prompt, system, schema, images }) => ({ prompt, system, schema, images }))), "prompts or schemas differ");
      if (c.expected.profile_after) assert.deepEqual(norm(args[0].profile), norm(c.expected.profile_after), "the topic list differs after the call");
    }
  } catch (e) { failed++; console.log(`FAIL  ${c.name}: ${e.message}`); if (e.diff) console.log(String(e.diff).slice(0, 600)); else if (e.actual !== undefined) console.log(JSON.stringify(e.actual).slice(0, 300), "\n  !=\n", JSON.stringify(e.expected).slice(0, 300)); }
}
A.cfg.MOCK_ANALYZER = false; A.set_backend(null);
console.log(`${golden.cases.length - failed}/${golden.cases.length} cases match the Python code`);
process.exit(failed ? 1 : 0);
