# Debrief AI

A local web app for Windows 11 that listens to any call on this PC (Teams, Zoom, Meet, Slack huddles),
transcribes it **locally**, analyses it with the **Claude Code CLI** (your Claude subscription, never an
API key), shows live panels during the call, writes the final feedback afterwards, records the call
(audio + screen video) with consent, and can open the firm's feedback form in an in-app browser and fill it in.

## Claude or GitHub Copilot

The top bar shows **AI: Claude** (or **AI: GitHub Copilot**). Click it to switch; the choice is saved in `settings.json` and applies to the next analysis, even during a call.
You can **paste a token** for either model, the same as on the server, so you do not depend on a CLI login that expires every few hours:
* **Claude**: run `claude setup-token` once (it needs Claude Code and a Pro/Max plan) and paste the long token it prints. It lasts about a year.
* **GitHub Copilot**: create a fine-grained GitHub token with the **Copilot Requests** permission (your account needs a Copilot plan) and paste it.

A pasted token is tested with a tiny request, then stored in `desktop\secrets.json` encrypted with your Windows login (git-ignored, useless on another PC) and used for every call instead of the CLI's own login.
Without a pasted token the app falls back to the login of the CLI on this PC (`claude` then `/login`, or `copilot login`). The CLIs themselves are still needed:
`npm install -g @anthropic-ai/claude-code` and/or `npm install -g @github/copilot`. `COPILOT_MODEL=auto` lets Copilot pick the model; set `COPILOT_CLI_PATH` if it is not on your PATH.
"Test it" sends a tiny request so a missing install, login or token shows up before a call. The Copilot CLI cannot force a JSON schema like Claude's, so the prompt carries the schema and a reply that does not fit is asked for once more.

## Several people on the call

Tick "Several people on the call" on the start screen and the people on the other side are told apart by their voices (Speaker 1, 2, 3...). Click a name in the
transcript to rename it; the transcript, the AI and the report use the name. A 26 MB voice model is needed once: downloaded automatically on first run (it is saved in
`desktop\models`), or set `SPEAKER_MODEL`. Your own microphone is always "you". Tuning: `SPEAKER_THRESHOLD`, `SPEAKERS_DEFAULT` in `.env`.

## Privacy

* Audio never leaves the PC. Speech recognition is faster-whisper running offline.
* Only transcript text, your notes, context lines and screenshots are sent to Claude, and only through
  `claude -p` (headless). `ANTHROPIC_API_KEY` is removed from the child environment, and the `anthropic`
  package is never imported.
* Recordings start only when you press **Record** inside a live session, and stay in
  `%LOCALAPPDATA%\DebriefAI\recordings` (deliberately outside OneDrive and the project folder). The app warns if
  you point `RECORDINGS_DIR` into a cloud-synced folder.
* The form filler never submits anything, never types passwords and never touches the photo question,
  self-declaration, interviewer, dates or attachments.

## Setup

1. Install Python 3 and Claude Code. Run `claude` once in a terminal and `/login`.
2. Double-click **scripts/run-desktop.bat** (creates `.venv`, installs `requirements.txt`, starts the server, opens
   http://127.0.0.1:5057).
3. **Speech models (about 2 GB, stored inside the project).** `run-desktop.bat` downloads them into `desktop\models` the first time
   (small.en, base.en, distil-large-v3 and the 26 MB speaker model), then everything runs offline. To fetch them without starting the app run
   `scripts\download-models.bat`. The folder is git-ignored; copy it to another PC to skip the download.
   Without the models the app still runs and says so.
4. **scripts/run-desktop-demo.bat** runs with the mock analyzer and replays a sample transcript: no Claude calls, no models.

## Using it

* **Start**: name, optional context, profile (Interview / Meeting notes), level, *Start listening*.
  For interviews, enter the **role you are hiring for**: Claude builds the topic list and weights for that role
  (paste the job description into the context box to sharpen it). If the interview covers a topic that is not on the
  list, even one unrelated to the role, it is added live (tagged *added live* / *off-role*) and scored. Leave the role
  empty to use the default Java list. The list is stored per session.
  Only one session can be live; others keep their summaries in the sidebar.
* **Live view**: transcript (left; italic partial lines), analysis (middle; for interviews *Covered so far* and
  *Still to cover* with “Ask:” ideas, follow-ups, flags, impression, topic-wise feedback), controls and logs (right).
  *Analyze now* forces an analysis; *Pause session* freezes audio, watching and analysis and adds dividers;
  *Listening* switches transcription on/off (also silences a running recording).
* **Screenshots & code review**: *Capture now* (screen or one window), paste with Ctrl+V after Win+Shift+S, or
  *Add screenshot…*. Claude reads the image; each exhibit becomes its own topic and counts in scores.
  **Watch screen for the AI** samples the chosen source at an interval (10 s … 1 h) and keeps a frame only when the screen
  changed and then settled (or after ~90 s of constant change); irrelevant frames are dropped silently.
  The source picker lists every window (even behind others or minimised; minimised ones are restored without
  stealing focus) and every screen.
* **Chat with AI** (right drawer): *Ask a question*, or *Add context* (a fact kept in “Context in use” and used by
  every later analysis). It can also start/stop the form fill, change one answer in the open form, or list pending feedback;
  the app carries out actions, the model never claims them.
* **Recording**: every Start → Stop is its own recording with its own files and player
  (`<session>_recNN.ogg` stereo Opus, left = you, right = everyone else; `<session>_recNN.mp4` H.264 + audio).
  The first recording in a session asks “Everyone on the call has been told it is being recorded” (stored in
  `session.json`; `REQUIRE_CONSENT=0` turns it off). Pauses become silence / a placeholder frame so the timeline
  matches the transcript. Recordings older than `RECORDING_RETENTION_DAYS` are deleted at startup, and a recording
  stops automatically when free disk drops below `MIN_FREE_MB`.
* **Review → decision**: weighted score, AI recommendation, scores, the evaluation form (every field has *Copy*;
  *Copy whole form*, *Download .md*), recordings, exhibits and drafts. *Advance* / *Reject* saves the matching draft;
  the decision can be switched later from the saved view (rewrites report, result, draft, day summary and the form category).
* **Filling the portal**: on a decided interview press *Open the form and fill it*. A WebView2 window opens the portal
  (`FORM_URL`), uses your sign-in (kept in `FORM_PROFILE_DIR`), opens the candidate and the *Provide Feedback* link,
  fills the answers and **stays open** for you to review and press Submit yourself. Then press *I have submitted it*.
* The sidebar (By type / All sessions / one type / To submit) is rebuilt from disk on restart. `Today` shows the day’s totals.

## Files

The interface (`../shared/ui`), the analysis code (`../shared/core`) and the call profiles (`../shared/profiles`) are shared with the server edition.

`sessions/<yyyy-mm-dd>_<nn>_<slug>/` holds `session.json, transcript.jsonl/.txt, notes.txt, evaluation.json, result.json,
report.md`, the draft files, `chat.json, context.json, exhibits.json, exhibits/NN.png, events.json, clips.json,
feedback.json, evaluation-form.md`. `sessions/day-summary-<date>.csv/.md` is updated on every save.

## Settings (`.env`, see `.env.example`)

| Key | Default | Meaning |
|---|---|---|
| LIVE_MODEL / FINAL_MODEL / CORRECT_MODEL / EXHIBIT_MODEL / CHAT_MODEL / FILL_MODEL | sonnet / opus / sonnet ×4 | Claude model per task |
| CLI_TIMEOUT | 240 | seconds per Claude call |
| ANALYZE_INTERVAL / ANALYZE_MIN_GAP / ANALYZE_NEW_LINES | 60 / 30 / 4 | live analysis cadence |
| WHISPER_MODEL / DRAFT_WHISPER_MODEL / FINAL_WHISPER_MODEL | small.en / base.en / distil-large-v3 | fast finals / instant drafts / accurate refine |
| ACCURATE_MAX_DECODE | 6 | seconds; if the accurate model decodes 5 s of audio slower than this, refine is skipped |
| SPEAKER_DEVICE | (all) | only loopback-capture output devices whose name contains this |
| ALLOW_MODEL_DOWNLOAD | 1 | 1 downloads missing models into `desktop\models` on first use; 0 never goes online |
| WATCH_STABLE / WATCH_CHANGE / WATCH_FORCE | 2 / 0.02 / 90 | screen watching thresholds |
| RECORDINGS_DIR | %LOCALAPPDATA%\DebriefAI\recordings | keep outside OneDrive |
| RECORD_AUDIO / VIDEO_RECORD | 1 / 1 | default recording options |
| REQUIRE_CONSENT | 1 | ask before the first recording of a session |
| RECORDING_RETENTION_DAYS / MIN_FREE_MB | 30 / 500 | cleanup (0 keeps) / stop below this free disk |
| VIDEO_FPS / VIDEO_MAX_WIDTH / VIDEO_CRF | 5 / 1280 / 30 | video quality |
| PORT / SESSIONS_DIR | 5057 / sessions | server port and data folder |
| MOCK_ANALYZER | 0 | 1 = offline fake analysis (and replay of a sample transcript) |
| FORM_URL / FORM_MODE / FORM_BROWSER / FORM_PROFILE_DIR / FILL_TIMEOUT | portal / window / msedge / next to recordings / 900 | form filler |

## Small screens

Container queries on the main area: three columns from 1100 px; two columns from 720 px (transcript left, the
two other panels stacked on the right); one column below 720 px (the page scrolls). The sidebar becomes an overlay
below 1100 px, the chat an overlay below 1500 px, level meters hide below 1250 px, the top bar is compact below 460 px.

## Form filling and its limits

* Default `FORM_MODE=window` uses the **in-app WebView2 window**, not Chrome/Edge automation, because remote
  debugging is blocked by policy here. `FORM_MODE=playwright` can drive Edge through the Playwright MCP, but only works
  where IT allows remote debugging; a failed start is detected and reported. It is not verified.
* It fills behavioral proficiency radios (and the sub-behaviour dropdowns that appear afterwards), Technical Competency 1-3
  with subcompetencies and comments, Areas to Probe, Overall Rating, Recommendation and Applicable Category.
  Custom widgets built on real `<select>` elements (Select2/Choices/Chosen) are handled; exotic widgets may not be.
* After the fill the report lists “Still empty on the page”. Anything the portal’s own validation rejects is yours to fix.

## Tests (`tests/`)

All use an isolated server on port 5058, a mock portal on 5059 and scratch folders, never the real `sessions/` or port 5057.
`backend_smoke.py` (API, recording, decisions, files), `dynamic_rubric_test.py` (role-based topic list, topics added live), `form_fill_test.py` (needs a display + WebView2),
`real_claude_test.py` (one real Claude call per schema), `real_capture_smoke.py` (real audio devices).

## Browser edition (GitHub Pages)

See [`pages/README.md`](../pages/README.md): a separate edition with the same interface and features that runs entirely in the visitor's browser.
