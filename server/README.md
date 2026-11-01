# Debrief AI: server edition

Self-hosted, multi-user version of Debrief AI. It listens to a call (an interview or a meeting) through the
**browser**, transcribes it with Whisper on **your server**, analyses it live with **Claude**, and writes the final
feedback, scores, drafts and the evaluation form when the call ends. It has the same interface and the same features as
the Windows desktop edition (see the table below), plus accounts: every user signs in and connects **their own** Claude login.

* **Same UI and analysis as the desktop edition**: the interface (`../shared/ui`) and the AI code (`../shared/core`: prompts,
  scoring, role-based topic lists, topics added live, chat) are shared, so the two editions cannot drift apart.
  `frontend/platform.js` is the only web-specific UI code (sign-in, browser audio/screen/recording).
* **Backend**: FastAPI. Postgres (SQLite in dev) holds accounts and the encrypted Claude tokens; each user's sessions are files
  under `DATA_DIR/users/<id>/` in the desktop edition's format (`report.md`, `evaluation.json`, `transcript.txt`, ...).
* **Speech**: faster-whisper on the server; the browser streams 16 kHz PCM over a WebSocket.
* **LLM**: `cli` = the Claude Code CLI with each user's own login (default), `anthropic` = one server API key, `mock` = demo/tests.

## Quick start (Docker)

```bash
cp .env.example .env            # set POSTGRES_PASSWORD and SECRET_KEY (python -c "import secrets;print(secrets.token_hex(32))")
docker compose up -d --build
# open http://localhost:8000, sign up (the first account becomes the administrator), then connect your Claude account
```

On Windows you can run `scripts\docker-up.bat` instead. Then set `ALLOW_REGISTRATION=false` and `ASR_ALLOW_DOWNLOAD=false` in
`.env` and run it again. The first start downloads the Whisper model (`small.en`, about 0.5 GB) into the `hf-cache` volume.
Browsers only allow the microphone and screen sharing on **HTTPS or localhost**, so put a reverse proxy (Caddy, nginx) in front for a real deployment.

Without Docker (mock model, nothing to install except Python): `scripts\dev-server.bat`, then sign up and press *Start listening*:
a sample call is replayed so you can click through every feature.

## Features compared with the desktop edition

Both editions share the interface and the AI code, and a test (`tests/test_parity.py`) fails when a feature exists in only one of them without a written reason. See also the table in the main README.

| | Desktop | Server (browser) |
|---|---|---|
| Live transcript, italic drafts, AI fixes of garbled terms, echo suppression | yes | yes |
| Topic list built from the **role**, topics added live and scored (also off-role) | yes | yes |
| Live panels, follow-up questions, flags, coverage, notes, extra context, chat with the AI | yes | yes |
| Pause session, Listening on/off, End session, Retry/Regenerate, decisions, drafts, saved files | yes | yes |
| Screenshots and code review, paste with Ctrl+V, Capture now | yes | yes (you share a screen/window/tab in the browser's picker) |
| Watch screen for the AI (key frames only, unrelated ones dropped) | yes | yes (the browser grabs the frames) |
| Audio and screen **recording** with the consent step, playback, download, delete | yes (local files) | yes (recorded in the browser, stored on the server, 30-day retention) |
| **Telling several people apart** on the other side of a call (Speaker 1, 2, 3..., click a name to rename it) | yes | yes (optional at the start screen) |
| Accurate second transcription pass that quietly improves a line a few seconds later | yes (when the PC is fast enough) | yes, optional: `ASR_REFINE_MODEL` (needs much more CPU) |
| Hearing both sides of a Teams/Zoom call | all system speakers, automatically | **Start transcribing** captures both: your microphone, then the sound of the call's tab through the browser's share window (only the sound is used) |
| Day summary, export zip, feedback to submit / filled / submitted | yes | yes (download buttons instead of folders) |
| Evaluation form answers: copy, download | yes | yes |
| **Filling the portal form for you** | yes (in-app window, planned by Claude) | helper bookmark in your own browser (beta, matches fields by label); you submit |
| Open the session folder | yes | not applicable |
| Accounts, each user's own AI login (Claude or GitHub Copilot) | single user, the logins of the PC | yes |

## Claude or GitHub Copilot

Every user chooses which AI analyses their calls (top bar, **AI: ...**), and can switch at any time, even during a call: the next analysis uses the new choice.
Claude is connected as described below. **GitHub Copilot**: the user creates a fine-grained GitHub token with the **Copilot Requests** permission (their own account must have a Copilot plan),
pastes it in the same dialog, and every call for that user runs the `copilot` CLI with that token (`COPILOT_GITHUB_TOKEN`) and a private `COPILOT_HOME`. Tokens are tested when connected and stored encrypted.
The Copilot CLI has no schema-enforcing switch, so the prompt carries the JSON schema and a reply that does not fit is asked for once more. `COPILOT_MODEL=auto` lets Copilot pick the model.

## Claude logins

By default **every user connects their own Claude account** (`CLAUDE_AUTH=per_user`), so the server can serve many users and each one's
usage counts against their own plan:

1. A user signs up, then opens **Account** (their email, top right).
2. On their own computer they run `claude setup-token` (needs Claude Code and a Pro/Max plan), sign in in the browser it opens, and copy the long-lived token it prints.
3. They paste it in the app. The server verifies it with a tiny test call, then stores it **encrypted** (Fernet, key from `ENCRYPTION_KEY` or derived from `SECRET_KEY`) in the database. It is never returned to the browser or logged.
4. Every analysis for that user (live, final, screenshots, chat, topic list) runs `claude -p` with that user's token only (`CLAUDE_CODE_OAUTH_TOKEN`) and a private `CLAUDE_CONFIG_DIR` per account. There is no fallback to anyone else's login, and a user without a connection cannot start a session.

`LLM_CONCURRENCY` limits parallel `claude` processes on the server and `LLM_USER_CONCURRENCY` per user. `ANTHROPIC_API_KEY` is stripped from the CLI's environment.
Other modes: `CLAUDE_AUTH=shared` (one login stored on the server for everybody, via `docker compose run --rm -it api claude` then `/login`; small teams only) and
`LLM_PROVIDER=anthropic` with a server API key. **Back up `ENCRYPTION_KEY`/`SECRET_KEY`**: if it changes, stored tokens cannot be decrypted and users must reconnect.

## Development

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt     # Windows: .venv\Scripts\
LLM_PROVIDER=mock uvicorn app.main:app --reload --port 8000        # UI and API on http://localhost:8000
pytest -q && ruff check app tests
```

`LLM_PROVIDER=mock` runs the analyzer's offline mock and replays a sample call, so the whole product can be clicked through without Claude or a microphone.
Change the UI in `../shared/ui` (it is served by both editions) and the web-only parts in `frontend/platform.js`; there is no build step.

## How it works

1. `POST /api/start` creates a session for the signed-in user. The page opens `WS /api/sessions/{id}/audio` (cookie auth) and sends PCM16 frames
   (byte 0: `0` = microphone, `1` = shared tab/system audio) plus a JSON message naming the sources.
2. The server segments speech (adaptive noise gate), shows italic drafts, transcribes utterances in a thread pool and appends lines to the session.
   Audio is dropped while the session is paused or listening is off.
3. A loop runs the live analysis every `ANALYZE_INTERVAL` s or after `ANALYZE_NEW_LINES` new lines, or on *Analyze now*; it also fixes garbled terms in the transcript.
4. Ending the session runs the final analysis (waiting for pending screenshots), moves it to `review`; a decision writes the report and the matching draft.
5. After a restart, sessions that were live or finalizing reload as `review` with a Retry; nothing is lost (lines are on disk).

The routes mirror the desktop edition's (`/api/state`, `/api/start`, `/api/end`, `/api/chat`, ...); interactive docs are at `/docs`.

## Security notes

* bcrypt password hashes, signed JWTs in an HttpOnly `SameSite=Strict` cookie (or `Authorization: Bearer` for API clients), per-user isolation (a user can only reach their own sessions, files, recordings and exports), login throttling (in-memory; put a real rate limiter/WAF in front when exposed to the internet), no prompts exposed to clients, uploads limited (8-12 MB screenshots, `MAX_UPLOAD_MB` recordings).
* `ENV=prod` refuses to start without a 32+ character `SECRET_KEY`. **Serve it over HTTPS**: browsers only allow microphone/screen capture on HTTPS or localhost, and cookies must not travel in clear text.
* Each user's Claude token is encrypted at rest and decrypted only in memory to start that user's `claude` process. Anyone with the database **and** your `ENCRYPTION_KEY`/`SECRET_KEY` can read them: protect both like passwords.
* Audio is transcribed on your server and is not stored; transcripts, screenshots and recordings are stored under `DATA_DIR` (the `appdata` volume). Only transcript text, notes, context and screenshots are sent to Claude. You are responsible for informing participants and for consent/retention rules where you live (recording needs the consent step, `REQUIRE_CONSENT`).

## Limits (honest list)

* One API process (the audio segmenters and session loops are in-process). Scale vertically, or add Redis-backed workers first.
* Every analysis starts a `claude` (Node) process: plan for `LLM_CONCURRENCY` x ~300 MB; for hundreds of simultaneous users use `LLM_PROVIDER=anthropic` or add workers.
* Whisper on CPU is roughly real-time for `small.en`; use `ASR_DEVICE=cuda` with a GPU image for more concurrent calls. The desktop edition's extra "accurate" second pass is not part of this edition.
* **Start transcribing** captures both people: your microphone, then the sound of the Teams/Zoom tab (or the system). Browsers cannot capture that silently, so the browser's share window opens once (pick the call's tab and keep tab audio on; only the sound is kept, the video is dropped). If you cancel it, only your microphone is transcribed and **＋ Add the other person** retries. Screen sharing (screenshots, watching, video recording) is a separate, optional step.
* **Speaker recognition** tells the people on the *call* side apart by their voices (a 26 MB model, `SPEAKER_MODEL`, runs on the CPU, no audio is kept). It works best with clear audio and 2-6 people; short interjections are assigned to the nearest voice, and similar voices can be merged or split wrongly (give two speakers the same name to merge them). Your own microphone is always "you": several people sharing one room microphone are not separated. The AI is told when several people spoke and reads the lines with their names.
* The portal fill helper matches fields by label and is not verified against any particular portal: check every answer, you submit it yourself.

## License

MIT, see `../LICENSE`. Contributions welcome: see `../CONTRIBUTING.md`.
