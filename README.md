# ASTRA RESQ

**NavIC-based positioning and emergency SOS coordination.** ASTRA 2026 hackathon - Domain 04: Satellite Communication, Navigation & Connectivity.

A person stranded in a remote valley with no mobile network gets a position fix from a NavIC-capable GNSS receiver (India's own IRNSS constellation, read as NMEA 0183), raises an SOS, and the SOS is carried out by a store-and-forward comms layer to a rescue dashboard. The dashboard shows every victim on an ISRO Bhuvan map with the nearest rescue team, so control rooms can assign teams and track each rescue through to completion. The victim sees "help is on the way" on their device.

## Architecture

```
 ┌──────────────────┐   NMEA 0183    ┌─────────────┐     ┌──────────────┐     ┌──────────┐
 │ NavIC GNSS rcvr  │ ─────────────▶ │ NMEA reader │ ──▶ │ Positioning  │ ──▶ │   SOS    │
 │ $GIGSV = NavIC   │  serial / USB  │ (checksums) │     │ lat/lon, HDOP│     │  form    │
 │ $GPGSV = GPS     │                └─────────────┘     │ NavIC count  │     └────┬─────┘
 └──────────────────┘                                    └──────────────┘          │
                                                                                   ▼
 ┌──────────────────┐      ┌─────────────────────────────┐      ┌─────────────────────────┐
 │  Coordination    │ ◀─── │ Rescue dashboard            │ ◀─── │ Comms layer (SIMULATED) │
 │ assign → en route│      │ Leaflet + Bhuvan WMS (ISRO) │      │ offline queue + retry   │
 │ → rescued        │      │ nearest team (haversine)    │      │ prod: LoRa mesh / SMS   │
 └──────────────────┘      └─────────────────────────────┘      └─────────────────────────┘
```

All of this runs as **one Flask app**: Jinja pages plus a JSON API, with vanilla JS and no build step.

## Features

- **NavIC positioning**: parses GGA, RMC and GSV sentences with checksum validation, and counts NavIC satellites (`$GIGSV`, NMEA 4.11) separately from GPS (`$GPGSV`). Bad lines are skipped and never crash the parser.
- **Victim device simulator** (`/sos`):
  - Live NMEA terminal replaying a generated Kedarnath log.
  - Fix status, NavIC and GPS satellites in view, a sky plot and SNR bars.
  - An optional browser-location fallback, clearly labelled as not NavIC.
- **Offline store-and-forward**: a network toggle simulates no connectivity. While it's off, each SOS is queued in `localStorage`. When the network comes back (or the browser fires `online`), the queue is flushed with exponential-backoff retry. Sends are idempotent by client UUID.
- **Rescue dashboard** (`/dashboard`):
  - Bhuvan WMS base map, with automatic OSM fallback if Bhuvan tiles keep failing.
  - Severity-coloured SOS markers that pulse while new, team markers, and dashed assignment lines.
  - Header KPIs and an SOS list with nearest team and distance.
  - Assign a team and advance status: `new → assigned → en_route → rescued`.
  - Toast alerts for new SOS.
- **Hardware bridge**: `device/serial_bridge.py` reads a real receiver over serial (or replays a file) and can send an SOS from the command line.

## Tech stack

Python 3.12 · Flask · pynmea2 · pyserial (bridge only) · Leaflet 1.9 · Bhuvan WMS (ISRO/NRSC) · OpenStreetMap · localStorage + BroadcastChannel · Vercel Python runtime. **No database.**

## Local setup

You need Python 3.12 (recommended; Vercel uses it). Python 3.9+ also works locally, and the test suite passes on 3.9.

**macOS / Linux**
```bash
cd astra-resq
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python app.py              # macOS: use  python app.py --port 5055  (see note)
```

**Windows (PowerShell)**
```powershell
cd astra-resq
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python app.py
```

Open http://127.0.0.1:5000/ for the overview, `/sos` for the victim device and `/dashboard` for the rescue dashboard.

> **macOS note:** the AirPlay Receiver uses port 5000, so `python app.py` fails with "Address already in use". Run `python app.py --port 5055` instead (and use that port in the URLs), or turn off AirPlay Receiver in System Settings → General → AirDrop & Handoff.

The app also runs with `flask --app app run`.

### Running tests
```bash
pytest -q
```
The tests cover checksum validation, GGA/RMC/GSV parsing including the `GI` talker, NavIC counting, haversine and nearest team, SOS idempotency, the PATCH status flow and input validation.

### Demo on a phone (same Wi-Fi)
```bash
python app.py --host 0.0.0.0 --port 5055
```
Find your laptop's LAN IP: `ipconfig getifaddr en0` on macOS, `ipconfig` on Windows, `hostname -I` on Linux. Then open `http://<LAN-IP>:5055/sos` on the phone and `/dashboard` on the laptop. Browser geolocation needs HTTPS, so over plain LAN http the phone uses the NavIC replay.

### Hardware bridge (optional)
```bash
pip install -r device/requirements.txt
python device/serial_bridge.py --port COM3 --baud 9600 --api http://localhost:5000
python device/serial_bridge.py --port /dev/ttyUSB0 --sos "Trapped by landslide" --name Asha
# no hardware? replay the sample file:
python device/serial_bridge.py --replay data/navic_sample.nmea --sos "test" --name Demo --api http://localhost:5055
```
The bridge prints each fix (NavIC and GPS in view, HDOP, accuracy) and reconnects if the receiver is unplugged. `--check` cross-checks the server's parser, and `--offline` only prints fixes. Any receiver that emits `$GIGSV` works, for example a NavIC-capable module such as the Quectel L89.

## Deploying to Vercel

1. Push this repo to GitHub.
2. Go to https://vercel.com/new and import the repo. The framework preset is detected automatically: Vercel finds `app.py` exporting `app`, uses `requirements.txt`, and serves `public/` from its CDN.
3. Click **Deploy**. No environment variables are needed.

CLI alternative: `npm i -g vercel && vercel`.

## No database: the sync model

There is intentionally **no database** of any kind of.

- **Server store:** `core/store.py` keeps SOS records in memory behind a lock. Seed data (teams, sample NMEA) are read-only files in `data/`. Locally (`python app.py`) this is one process, so it works across devices on the same Wi-Fi.
- **Client mirror:** every SOS and every dashboard change is also written to `localStorage["astra.sos"]` and broadcast on `BroadcastChannel("astra-resq")`.
- **Dashboard merge:** the dashboard polls `GET /api/sos` every 3 s and listens to the channel and `storage` events. It merges both sources by `id`, and the record with the later `updated_at` wins. If a serverless instance has never seen an SOS, the dashboard re-creates it there (idempotent POST) before patching.

**On Vercel**, serverless instances don't share memory, so run the demo with **the SOS page and the dashboard in two tabs or windows of the same browser**. Locally, it also works across devices. The dashboard header shows `Sync: server + local`.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | `{status: "ok"}` |
| GET | `/api/nmea/replay` | parsed fixes from `data/navic_sample.nmea` (incl. raw sentences) |
| POST | `/api/nmea/parse` | `{lines: [str]}` → `{fix, stats}` (latest valid fix) |
| POST | `/api/sos` | create. Idempotent by client `id`: 201 if new, 200 if it already exists |
| GET | `/api/sos` | newest first, each with `nearest_team {id, name, distance_km}` |
| GET | `/api/sos/<id>` | one SOS |
| PATCH | `/api/sos/<id>` | `{status?, team_id?}`. Assigning a team to a `new` SOS moves it to `assigned`. Bad input gives 400; an invalid transition gives 409 |
| GET | `/api/teams` | from `data/teams.json` |

## 3-minute demo script

1. **(0:00) Problem.** On the overview page: "Kedarnath 2013, Chamoli 2021: disasters hit where there's no network." Show the architecture.
2. **(0:30) NavIC fix.** Click *Open both side by side*. On the victim device, point at the NMEA stream: the `$GIGSV` lines are NavIC. The fix goes from acquiring to locked, and the panel shows **NavIC satellites in view** separately from GPS, with the sky plot and accuracy.
3. **(1:00) Online SOS.** Enter a name, pick *Critical*, press **SOS**. It appears on the Bhuvan map with a pulsing marker, a toast and the nearest team.
4. **(1:30) No network.** Turn the network switch **off** and send another SOS. It shows "Queued, will send when connectivity returns", and the dashboard stays empty. Turn the network **on** and the queue flushes, so the SOS appears. "In production this hop is a LoRa mesh or SMS gateway."
5. **(2:15) Coordinate.** On the dashboard, assign the nearest team (a dashed line is drawn), then click *Mark en route*. On the victim device: "Help is on the way."
6. **(2:45) Real hardware.** Show `device/serial_bridge.py` and close with what's real vs simulated.

## Project structure

```
app.py                  Flask app: pages + /api/* (exports `app`)
core/nmea.py            NMEA parsing (pynmea2) -> Fix objects, checksum validation
core/geo.py             haversine distance, nearest team
core/store.py           thread-safe in-memory SOS store + status flow
templates/              base, index (landing), sos (victim device), dashboard
public/css/app.css      single stylesheet
public/js/common.js     API helper, geo, toasts, localStorage/BroadcastChannel sync
public/js/sos.js        NMEA replay, sky plot, SOS form, offline queue
public/js/dashboard.js  polling + merge, SOS cards, actions
public/js/map.js        Leaflet map, Bhuvan/OSM layers, markers
data/navic_sample.nmea  generated replay file (60 epochs near Kedarnath)
data/teams.json         seed rescue teams
scripts/gen_nmea.py     regenerates the sample:  python scripts/gen_nmea.py
device/serial_bridge.py real receiver -> API (deps in device/requirements.txt)
tests/                  pytest suite
```

## What's real vs simulated

- **Real:**
  - NMEA parsing and checksum validation, and the NavIC vs GPS satellite split.
  - The offline queue with idempotent retry.
  - Nearest-team routing, the Bhuvan WMS map, and the serial bridge for real receivers.
- **Simulated:**
  - The receiver on `/sos`, which replays a generated log.
  - The comms link: a network toggle stands in for LoRa / satellite / SMS.
  - The rescue teams, which are seed data.

## Team / credits

- Team name: _TBD_
- Members: _TBD_
- Map data: Bhuvan (ISRO/NRSC), © OpenStreetMap contributors. Built with Flask, pynmea2 and Leaflet.
