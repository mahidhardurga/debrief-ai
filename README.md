# Debrief AI

Listens to a call (an interview or a meeting), transcribes it, analyses it live with Claude and writes the
final feedback, scores and drafts when the call ends. Three editions share the same interface and the same features:

| Folder | Edition | Stack | Start it |
|---|---|---|---|
| [`desktop/`](desktop/README.md) | Windows app: captures your speakers + mic, records audio/screen, fills the feedback form | Flask + vanilla JS, faster-whisper, Claude Code CLI | `scripts\run-desktop.bat` |
| [`server/`](server/README.md) | Self-hosted multi-user edition (Docker): same interface and features, in the browser, with accounts and each user's own Claude login | FastAPI + Postgres + the shared UI | `scripts\docker-up.bat` |
| [`pages/`](pages/README.md) | Browser-only edition for GitHub Pages: no server, everything runs in the visitor's browser with their own Anthropic API key | the shared UI + an in-page JS engine, transformers.js | `scripts
un-pages.bat`, deployed by GitHub Actions |

## The editions have the same features

All three share the interface (`shared/ui`), the call types (`shared/profiles`) and the sample calls. Desktop and server share the AI code (`shared/core`); the Pages edition has a JS port of it whose prompts, schemas and reports are checked against the Python code by golden tests (`pages/tests`). A test
(`server/backend/tests/test_parity.py`) fails if a session feature or API route exists in one edition without a written reason.
What differs is only what a browser cannot do, or what a single-user PC does not need:

| | Desktop (Windows) | Server (browser) | GitHub Pages (browser) |
|---|---|---|---|
| Hearing the other side of the call | automatically, all speakers | the browser's share window, once per session | the same share window |
| Telling several people apart, naming them | yes | yes | yes (a 400 MB speaker model, downloaded once) |
| Filling the portal form | in-app window, planned by Claude | helper bookmark in your own browser | helper bookmark |
| Where recordings and files live | folders on your PC | on the server, downloads in the page | in this browser (IndexedDB), downloads in the page |
| AI model | Claude **or GitHub Copilot** (your choice in the top bar), using a pasted token (long-lived) or the CLI login on this PC | Claude **or GitHub Copilot**, chosen by each user, each with their own login (accounts) | the visitor's own Anthropic API key (no Copilot option) |
| Speech to text | faster-whisper on your PC | faster-whisper on the server | Whisper in the browser (WebGPU/WASM), audio never uploaded |
| Everything else (live analysis, role-based topic lists, topics added live, chat, screenshots and watching, recording, scoring, drafts, evaluation form, exports, day summary) | yes | yes | yes |

## Layout

```
Debrief AI/
├─ shared/                  used by BOTH editions, so they cannot drift apart
│  ├─ ui/                   the interface (index.html, app.css, app.js)
│  ├─ core/                 analysis code: prompts, schemas, scoring, topic lists, chat (analyzer.py, reports.py)
│  └─ profiles/ samples/    call types (interview, meeting) and sample calls
├─ desktop/                 Windows edition
│  ├─ backend/              Flask server, device capture, recorders, portal form filler
│  ├─ frontend/             platform.js (desktop hooks for the shared UI)
│  ├─ tests/  requirements.txt  .env.example  README.md
│  └─ sessions/             your data (git-ignored)
├─ server/                  self-hosted multi-user edition
│  ├─ backend/              FastAPI: accounts, per-user Claude logins, session engine, audio WebSocket, tests/
│  ├─ frontend/             platform.js (sign-in, browser audio / screen / recording)
│  └─ Dockerfile  docker-compose.yml  .env.example  README.md
├─ pages/                   GitHub Pages edition: engine/ (JS port of the session engine), platform.js, whisper-worker.js, tests/, build.mjs
├─ scripts/                 all launchers (.bat), see below
├─ .github/workflows/       CI
└─ README.md  LICENSE  CONTRIBUTING.md  .gitignore
```

## Scripts (`scripts\`)

| Script | What it does |
|---|---|
| `run-desktop.bat` | Desktop app: creates `desktop\.venv`, installs, starts on http://127.0.0.1:5057 |
| `run-desktop-demo.bat` | Desktop app with a mock analyzer and a replayed sample call (no Claude, no models) |
| `test-desktop.bat` | Desktop backend smoke test |
| `dev-server.bat` | Server edition on http://localhost:8000 with the mock model (no Docker, no Claude) |
| `test-server.bat` | Server lint + tests + UI syntax check (same as CI) |
| `docker-up.bat` / `docker-down.bat` | Run / stop the server edition in Docker |
| `run-pages.bat` | Builds the GitHub Pages edition and serves it on http://localhost:8765 |

Licence: MIT. See `CONTRIBUTING.md`.
