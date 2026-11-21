# ASTRA RESQ - conventions for Claude Code sessions

NavIC positioning + SOS coordination prototype. One Flask app (Jinja pages + JSON API), vanilla JS, no build step.

## Run / test
- Setup: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt`
- Run: `python app.py --port 5055` (macOS AirPlay holds 5000) or `flask --app app run`. `--host 0.0.0.0` for phones.
- Test: `pytest -q`. Must stay green; add tests for every API/core change.
- Regenerate sample NMEA: `python scripts/gen_nmea.py` (deterministic, seed 7).

## Hard rules
- **No database or storage service** (no SQLite/Postgres/Redis/KV/Firebase...). Server state lives only in `core/store.py` (in-memory, behind a lock). Seed data = read-only files in `data/`.
- No frontend framework, no Node build. Leaflet 1.9.4 from unpkg (pinned + SRI). One CSS file.
- `requirements.txt` = only Flask + pynmea2 (what Vercel ships). Dev deps → `requirements-dev.txt`; hardware deps → `device/requirements.txt`.
- Vercel: `app` must stay exported from root `app.py`; static files live in `public/` (`static_url_path=""`, so `/css/...`, `/js/...`).
- No secrets / API keys.

## Sync model (don't break it)
- `POST /api/sos` → in-memory store; `GET /api/sos` polled by dashboard every 3 s.
- Client mirror: `localStorage["astra.sos"]` + `BroadcastChannel("astra-resq")` (see `public/js/common.js` `Sync`).
- Merge by SOS `id`; later `updated_at` wins (tie → further status). SOS creation is idempotent by client UUID.
- Offline queue: `localStorage["astra.queue"]`; only mirrored/broadcast once the simulated network is ON.
- Status flow `new → assigned → en_route → rescued`, forward one step only; team required past `new`. Rules exist in BOTH `core/store.py` and `dashboard.js` `localTransition` - keep them in sync.

## Layout
- `core/` nmea.py (parsing, `$GI` = NavIC), geo.py (haversine), store.py
- `app.py` routes + validation (400 bad input, 404 missing, 409 bad transition)
- `templates/` base/index/sos/dashboard; `public/js/` common, sos, dashboard, map
- `device/serial_bridge.py` real receiver → API; `tests/` pytest

## Style
- Python: type hints, `from __future__ import annotations` (keep 3.9-compatible), small pure functions, no new deps without reason.
- JS: IIFE per file, `"use strict"`, escape all user text with `ASTRA.escapeHtml`, wrap localStorage in try/catch (use `ASTRA.lsGet/lsSet`).
- UI: dark mission-control; saffron = SOS/action, teal = NavIC. Monospace for NMEA/coords. No emoji.
