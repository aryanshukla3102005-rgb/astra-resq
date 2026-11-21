"""Bridge a real NavIC-capable GNSS receiver (NMEA over serial/USB) to the ASTRA RESQ API.

Examples:
    python device/serial_bridge.py --port COM3 --baud 9600 --api http://localhost:5000
    python device/serial_bridge.py --port /dev/ttyUSB0 --sos "Trapped by landslide" --name Asha
    python device/serial_bridge.py --replay data/navic_sample.nmea --sos "test" --name Demo

Parsing happens locally with core.nmea (same code as the server). Batches of raw lines
are also posted to /api/nmea/parse so you can confirm the server agrees.
With --sos, the SOS is sent once the first valid fix is available (with retry).

Install:  pip install -r requirements.txt -r device/requirements.txt
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
import uuid
from pathlib import Path
from typing import Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.nmea import Fix, NmeaParser  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


# --------------------------------------------------------------------------- sources
def serial_lines(port: str, baud: int, reconnect_s: float = 3.0) -> Iterator[str]:
    """Yield NMEA lines forever, reconnecting if the receiver is unplugged."""
    try:
        import serial  # pyserial
    except ImportError:
        sys.exit("pyserial is not installed: pip install -r device/requirements.txt")
    while True:
        try:
            with serial.Serial(port, baud, timeout=2) as ser:
                print(f"[bridge] connected to {port} @ {baud} baud", flush=True)
                while True:
                    raw = ser.readline()
                    if raw:
                        yield raw.decode("ascii", errors="replace").strip()
        except serial.SerialException as exc:
            print(f"[bridge] serial error: {exc} - retrying in {reconnect_s:.0f}s", flush=True)
            time.sleep(reconnect_s)


def replay_lines(path: str, rate_hz: float = 1.0, loop: bool = False) -> Iterator[str]:
    """Yield lines from a recorded NMEA file, pacing one epoch (GGA) per 1/rate_hz seconds."""
    while True:
        with open(path, encoding="ascii", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if line[3:6] == "GGA":
                    time.sleep(1.0 / rate_hz)
                yield line
        if not loop:
            return


# --------------------------------------------------------------------------- API
class Api:
    def __init__(self, base: str, timeout: float = 5.0) -> None:
        if requests is None:
            sys.exit("requests is not installed: pip install -r device/requirements.txt")
        self.base = base.rstrip("/")
        self.timeout = timeout

    def post(self, path: str, body: dict) -> Optional[dict]:
        try:
            r = requests.post(self.base + path, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            print(f"[api] {path}: network error ({exc.__class__.__name__})", flush=True)
            return None
        if r.status_code >= 400:
            print(f"[api] {path}: HTTP {r.status_code} {r.text[:200]}", flush=True)
            if 400 <= r.status_code < 500:
                raise ValueError(r.text)
            return None
        return r.json()


def describe(fix: Fix) -> str:
    if not fix.valid:
        return f"{fix.timestamp or '--:--:--'}  NO FIX   NavIC {len(fix.navic_sats)}  GPS {len(fix.gps_sats)}"
    return (
        f"{fix.timestamp}  {fix.lat:.6f},{fix.lon:.6f}  ±{fix.accuracy_m or 0:.1f} m  "
        f"used {fix.sats_used:2d}  NavIC {len(fix.navic_sats)}  GPS {len(fix.gps_sats)}  HDOP {fix.hdop}"
    )


def build_sos(args, fix: Fix, sos_id: str) -> dict:
    body = {
        "id": sos_id,
        "name": args.name,
        "message": args.sos,
        "people_count": args.people,
        "severity": args.severity,
        "lat": fix.lat,
        "lon": fix.lon,
        "navic_sats": len(fix.navic_sats),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": "device",
        "position_source": "navic",
    }
    if fix.accuracy_m is not None:
        body["accuracy_m"] = fix.accuracy_m
    if args.phone:
        body["phone"] = args.phone
    return body


# --------------------------------------------------------------------------- main loop
def run(args) -> int:
    api = Api(args.api) if (args.api and not args.offline) else None
    lines = replay_lines(args.replay, args.rate, args.loop) if args.replay else serial_lines(args.port, args.baud)
    parser = NmeaParser()
    batch: list[str] = []
    sos_body: Optional[dict] = None
    sos_id = str(uuid.uuid4())  # stable id -> retries are idempotent on the server
    sos_sent = not args.sos
    next_retry = 0.0

    for line in lines:
        batch.append(line)
        fix = parser.feed(line)
        if fix is None:
            continue
        print(describe(fix), flush=True)

        if api and args.check and len(batch) > 1:
            res = api.post("/api/nmea/parse", {"lines": batch[:-1]})
            if res and res.get("fix"):
                f = res["fix"]
                print(f"         server: valid={f['valid']} navic={f['navic_in_view']} gps={f['gps_in_view']}", flush=True)
        batch = batch[-1:]

        if not sos_sent and fix.valid and api and time.time() >= next_retry:
            sos_body = sos_body or build_sos(args, fix, sos_id)
            try:
                res = api.post("/api/sos", sos_body)
            except ValueError:
                print("[sos] rejected by server - not retrying", flush=True)
                return 2
            if res:
                sos_sent = True
                near = res.get("nearest_team") or {}
                print(f"[sos] delivered id={sos_id} nearest team: {near.get('name')} ({near.get('distance_km')} km)", flush=True)
                if args.exit_after_sos:
                    return 0
            else:
                next_retry = time.time() + 5
                print("[sos] not delivered - will retry in 5 s (queued on device)", flush=True)

    last = parser.flush()
    if last:
        print(describe(last), flush=True)
    if not sos_sent:
        print("[sos] stream ended before the SOS could be delivered", flush=True)
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="NavIC receiver -> ASTRA RESQ bridge")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--port", help="serial port, e.g. COM3 or /dev/ttyUSB0 or /dev/tty.usbserial-XXXX")
    src.add_argument("--replay", help="replay a recorded NMEA file instead of a serial port")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--api", default="http://localhost:5000", help="ASTRA RESQ base URL")
    ap.add_argument("--offline", action="store_true", help="only print fixes, never call the API")
    ap.add_argument("--check", action="store_true", help="also post raw NMEA to /api/nmea/parse")
    ap.add_argument("--sos", metavar="MESSAGE", help="send an SOS with this message once a fix is available")
    ap.add_argument("--name", default="Field device")
    ap.add_argument("--phone", default="")
    ap.add_argument("--people", type=int, default=1)
    ap.add_argument("--severity", choices=["low", "medium", "critical"], default="critical")
    ap.add_argument("--rate", type=float, default=1.0, help="replay speed in epochs per second")
    ap.add_argument("--loop", action="store_true", help="loop the replay file")
    ap.add_argument("--exit-after-sos", action="store_true", help="stop once the SOS is delivered")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n[bridge] stopped", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
