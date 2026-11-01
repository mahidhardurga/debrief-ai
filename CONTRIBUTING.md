# Contributing

1. Fork, branch, make a focused change.
2. Run `scripts\test-server.bat` (or `cd server/backend && pytest -q && ruff check app tests`) and, for desktop changes, `scripts\test-desktop.bat`. For `shared/core` changes run `python pages/tests/make_golden.py` then `node pages/tests/golden.test.mjs` (the JS port must give identical prompts and reports). CI runs the server tests, the Pages tests, a syntax check of the UI and a Docker build.
3. **Keep the editions in step.** A feature goes into all three: the Pages edition's JS engine (`pages/engine/session.js`, `api.js`) as well as both `desktop/backend/session.py` + `server.py` and `server/backend/app/engine/session.py` + `routers/api.py`. `tests/test_parity.py` fails when one has something the other lacks; if a feature truly cannot exist in one edition, list it in that test with the reason.
4. **Shared code** lives in `shared/` and is used by both editions: the interface (`shared/ui`), the analysis code (`shared/core`), call profiles (`shared/profiles`) and sample calls. Change it once and test both editions. Edition-specific UI goes in `desktop/frontend/platform.js` or `server/frontend/platform.js`.
5. New call types: add a JSON profile in `shared/profiles/` (see `meeting.json` for the minimal shape); add a test if you change schema building.
6. New LLM providers (server edition): implement `structured(system, prompt, schema, model, images, auth)` in `server/backend/app/llm/`, register it in `llm/__init__.py`.
7. Database changes (accounts only) need an Alembic migration: `cd server/backend && alembic revision --autogenerate -m "what changed"`.
8. Never commit secrets, recordings or real interview data.
