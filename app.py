"""ASTRA RESQ - NavIC positioning + SOS coordination. One Flask app: pages + JSON API.

Run:  python app.py [--host 0.0.0.0] [--port 5000]      or   flask --app app run
Vercel picks up the module-level `app` automatically.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from functools import lru_cache
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from core.geo import nearest_team
from core.nmea import latest_fix, parse_file, parse_lines
from core.store import STATUS_FLOW, NotFound, Store, StoreError

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SAMPLE_NMEA = DATA / "navic_sample.nmea"

app = Flask(
    __name__,
    static_folder=str(ROOT / "public"),
    static_url_path="",
    template_folder=str(ROOT / "templates"),
)
app.json.sort_keys = False
store = Store()

SEVERITIES = ("low", "medium", "critical")
SOURCES = ("simulator", "device")
POSITION_SOURCES = ("navic", "browser")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
PHONE_RE = re.compile(r"^[0-9+()\- ]{3,20}$")
MAX_NMEA_LINES = 2000


class BadRequest(ValueError):
    pass


@app.errorhandler(BadRequest)
def _bad_request(exc):
    return jsonify(error=str(exc)), 400


@app.errorhandler(404)
def _not_found(exc):
    if request.path.startswith("/api/"):
        return jsonify(error="not found"), 404
    return exc


@app.errorhandler(405)
def _method_not_allowed(exc):
    if request.path.startswith("/api/"):
        return jsonify(error="method not allowed"), 405
    return exc


# --------------------------------------------------------------------------- data
@lru_cache(maxsize=1)
def load_teams() -> list[dict]:
    with open(DATA / "teams.json", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def replay_fixes() -> list[dict]:
    fixes, _ = parse_file(SAMPLE_NMEA)
    return [f.to_dict() for f in fixes]


def team_ids() -> set[str]:
    return {t["id"] for t in load_teams()}


def with_nearest(rec: dict) -> dict:
    rec["nearest_team"] = nearest_team(rec["lat"], rec["lon"], load_teams())
    return rec


# --------------------------------------------------------------------------- validation
def _json_body() -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise BadRequest("request body must be a JSON object")
    return body


def _num(body, key, lo, hi, required=True):
    v = body.get(key)
    if v is None:
        if required:
            raise BadRequest(f"'{key}' is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise BadRequest(f"'{key}' must be a number")
    if not (lo <= v <= hi):
        raise BadRequest(f"'{key}' must be between {lo} and {hi}")
    return v


def _str(body, key, max_len, required=False, default=""):
    v = body.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise BadRequest(f"'{key}' is required")
        return default
    if not isinstance(v, str):
        raise BadRequest(f"'{key}' must be a string")
    v = v.strip()
    if len(v) > max_len:
        raise BadRequest(f"'{key}' must be at most {max_len} characters")
    return v


def _choice(body, key, options, default):
    v = body.get(key, default)
    if v not in options:
        raise BadRequest(f"'{key}' must be one of: {', '.join(options)}")
    return v


def validate_sos(body: dict) -> dict:
    sos_id = body.get("id")
    if not isinstance(sos_id, str) or not ID_RE.match(sos_id):
        raise BadRequest("'id' must be a client-generated id (8-64 chars: letters, digits, - or _)")
    phone = _str(body, "phone", 20)
    if phone and not PHONE_RE.match(phone):
        raise BadRequest("'phone' may contain only digits, spaces and + - ( )")
    people = body.get("people_count", 1)
    if isinstance(people, bool) or not isinstance(people, int) or not (1 <= people <= 500):
        raise BadRequest("'people_count' must be an integer between 1 and 500")
    navic = body.get("navic_sats", 0)
    if isinstance(navic, list):
        navic = len(navic)
    if isinstance(navic, bool) or not isinstance(navic, int) or not (0 <= navic <= 64):
        raise BadRequest("'navic_sats' must be an integer between 0 and 64")
    created_at = body.get("created_at")
    if created_at is not None:
        if not isinstance(created_at, str):
            raise BadRequest("'created_at' must be an ISO-8601 string")
        try:
            dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BadRequest("'created_at' must be an ISO-8601 string") from exc

    sos = {
        "id": sos_id,
        "name": _str(body, "name", 80, required=True),
        "phone": phone,
        "message": _str(body, "message", 500),
        "people_count": people,
        "severity": _choice(body, "severity", SEVERITIES, "medium"),
        "lat": _num(body, "lat", -90, 90),
        "lon": _num(body, "lon", -180, 180),
        "accuracy_m": _num(body, "accuracy_m", 0, 100_000, required=False),
        "navic_sats": navic,
        "source": _choice(body, "source", SOURCES, "simulator"),
        "position_source": _choice(body, "position_source", POSITION_SOURCES, "navic"),
    }
    if created_at:
        sos["created_at"] = created_at
    return sos


# --------------------------------------------------------------------------- pages
@app.get("/")
def index():
    return render_template("index.html", page="home")


@app.get("/sos")
def sos_page():
    return render_template("sos.html", page="sos")


@app.get("/dashboard")
def dashboard_page():
    return render_template("dashboard.html", page="dashboard")


# --------------------------------------------------------------------------- API
@app.get("/api/health")
def health():
    return jsonify(status="ok", sos_count=len(store))


@app.get("/api/nmea/replay")
def nmea_replay():
    return jsonify(replay_fixes())


@app.post("/api/nmea/parse")
def nmea_parse():
    body = _json_body()
    lines = body.get("lines")
    if not isinstance(lines, list) or not lines or not all(isinstance(x, str) for x in lines):
        raise BadRequest("'lines' must be a non-empty list of NMEA sentences")
    if len(lines) > MAX_NMEA_LINES:
        raise BadRequest(f"at most {MAX_NMEA_LINES} lines per request")
    fixes, stats = parse_lines(lines)
    fix = latest_fix(fixes)
    return jsonify(fix=fix.to_dict(include_sentences=False) if fix else None, stats=stats)


@app.get("/api/sos")
def list_sos():
    return jsonify([with_nearest(r) for r in store.list()])


@app.post("/api/sos")
def create_sos():
    sos = validate_sos(_json_body())
    rec, created = store.create(sos)
    return jsonify(with_nearest(rec)), (201 if created else 200)


@app.get("/api/sos/<sos_id>")
def get_sos(sos_id):
    rec = store.get(sos_id)
    if rec is None:
        return jsonify(error="SOS not found"), 404
    return jsonify(with_nearest(rec))


@app.patch("/api/sos/<sos_id>")
def patch_sos(sos_id):
    body = _json_body()
    status, team_id = body.get("status"), body.get("team_id")
    if status is None and team_id is None:
        raise BadRequest("provide 'status' and/or 'team_id'")
    if status is not None and status not in STATUS_FLOW:
        raise BadRequest(f"'status' must be one of: {', '.join(STATUS_FLOW)}")
    if team_id is not None and team_id not in team_ids():
        raise BadRequest("unknown 'team_id'")
    try:
        rec = store.update(sos_id, status=status, team_id=team_id)
    except NotFound:
        return jsonify(error="SOS not found"), 404
    except StoreError as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(with_nearest(rec))


@app.get("/api/teams")
def teams():
    return jsonify(load_teams())


# --------------------------------------------------------------------------- dev server
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ASTRA RESQ dev server")
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow phones on the same Wi-Fi")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--no-debug", action="store_true")
    args = ap.parse_args()
    app.run(host=args.host, port=args.port, debug=not args.no_debug)
