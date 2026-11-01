// Assembles the GitHub Pages site into pages/dist: the shared UI, the profiles and sample calls, and this edition's engine.
// Usage: node pages/build.mjs [--serve [port]]
import { cpSync, mkdirSync, rmSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, join, extname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url)), root = dirname(here), out = join(here, "dist");
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
for (const f of ["index.html", "app.js", "app.css", "browser.js"]) cpSync(join(root, "shared/ui", f), join(out, f));
cpSync(join(root, "shared/profiles"), join(out, "profiles"), { recursive: true });
cpSync(join(root, "shared/samples"), join(out, "samples"), { recursive: true });
cpSync(join(here, "engine"), join(out, "engine"), { recursive: true });
for (const f of ["platform.js", "whisper-worker.js"]) cpSync(join(here, f), join(out, f));
writeFileSync(join(out, ".nojekyll"), "");
console.log("built", out);

const i = process.argv.indexOf("--serve");
if (i > 0) {
  const port = Number(process.argv[i + 1]) || 8765;
  const types = { ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".json": "application/json", ".txt": "text/plain" };
  createServer((req, res) => {
    let p = decodeURIComponent(req.url.split("?")[0]); if (p.endsWith("/")) p += "index.html";
    const f = join(out, p);
    if (!f.startsWith(out) || !existsSync(f)) { res.writeHead(404); return res.end("not found"); }
    res.writeHead(200, { "content-type": types[extname(f)] || "application/octet-stream", "cache-control": "no-store" });
    res.end(readFileSync(f));
  }).listen(port, () => console.log(`http://localhost:${port}/`));
}
