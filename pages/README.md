# Debrief AI - GitHub Pages edition

The same interface and features as the desktop and server editions, with **no server at all**. Every visitor uses their own machine:

- **Speech to text** - Whisper (transformers.js, WebGPU or WASM) runs in a worker in the page. Audio never leaves the browser.
- **Who is speaking** - an in-browser speaker model (about 400 MB, downloaded once, cached) numbers the people on the other side (Speaker 1, 2, 3, renameable).
- **Analysis** - the page calls the Anthropic API directly with the visitor's own API key (typed in Settings, kept in the tab only unless "remember" is ticked). With no key the page runs in a demo mode (a replayed sample call, fake analysis).
- **Storage** - sessions, screenshots and recordings stay in the browser (IndexedDB). Reports, zips and the day summary download from the page.

## How it fits together

`shared/ui` (the same interface as the other editions) calls `/api/...`; `engine/api.js` answers those calls inside the page (a fetch shim), driving `engine/session.js`, a port of the Python `Session`. `engine/analyzer.js` and `engine/reports.js` are ports of `shared/core`; `platform.js` is this edition's hooks (settings, audio, downloads).

```
pages/
├─ engine/        analyzer.js reports.js session.js api.js storage.js audio.js
├─ platform.js    hooks for the shared UI
├─ whisper-worker.js
├─ tests/         make_golden.py (Python -> golden.json), golden.test.mjs, engine.test.mjs
└─ build.mjs      assembles dist/ from shared/ui, shared/profiles, shared/samples and this folder
```

## Run and test

```
scripts\run-pages.bat            build + serve on http://localhost:8765
node pages/tests/golden.test.mjs   prompts, schemas and reports are identical to the Python code
node pages/tests/engine.test.mjs   a whole call, start to report
python pages/tests/make_golden.py --check   fails if the golden file is stale after a change to shared/core
```

## Publish

Push to GitHub, then **Settings > Pages > Source: GitHub Actions**. `.github/workflows/pages.yml` runs the tests, builds `pages/dist` and deploys it on every push to `main`.

## Differences from the other editions

- No accurate "refine" pass over the recording and no server-side files; the transcript is the live one.
- The other side of a call is heard through the browser's share window (pick the Teams/Zoom tab with "Share tab audio"), exactly as in the server edition.
- Filling the portal form uses the helper bookmark (a web page cannot drive another window).
- Pages are public: never put a key in the code.
