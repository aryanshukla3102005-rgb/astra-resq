"""Generate data/navic_sample.nmea - a reproducible NavIC + GPS replay file.

Scenario: a trekker near Kedarnath (Uttarakhand) walking slowly after a flash flood.
60 one-second epochs. The first 3 epochs have no fix (signal acquisition), then HDOP
settles. Each epoch: $GNGGA, $GNRMC, $GPGSV (GPS) and $GIGSV (NavIC).

Usage:  python scripts/gen_nmea.py [--out data/navic_sample.nmea] [--seed 7]
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.nmea import with_checksum  # noqa: E402

START = dt.datetime(2026, 7, 15, 6, 30, 0)  # UTC, monsoon season
START_LAT, START_LON = 30.73460, 79.06690  # just below Kedarnath temple
ALTITUDE_M = 3583.0
GEOID_SEP_M = -40.2
EPOCHS = 60
NO_FIX_EPOCHS = 3

# NavIC: 3 GEO + 4 IGSO (PRN 1-9). Seen from Kedarnath they barely move.
NAVIC = {
    2: (52, 214), 3: (61, 182), 4: (54, 147), 5: (38, 239),
    6: (44, 121), 7: (69, 196), 9: (23, 102),
}
# GPS: (elevation, azimuth, elevation drift deg/min). Subset visible per epoch varies.
GPS = {
    3: (72, 41, -0.2), 6: (35, 300, 0.3), 11: (18, 75, 0.4), 14: (49, 228, -0.3),
    17: (27, 158, 0.2), 19: (61, 112, -0.1), 22: (12, 330, 0.5), 24: (40, 12, -0.4),
    28: (8, 255, 0.6),
}


def dm(value: float, is_lat: bool) -> tuple[str, str]:
    hemi = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    value = abs(value)
    deg = int(value)
    minutes = (value - deg) * 60
    return (f"{deg:02d}{minutes:08.5f}" if is_lat else f"{deg:03d}{minutes:08.5f}"), hemi


def gsv(talker: str, sats: list[tuple[int, int, int, int | None]]) -> list[str]:
    msgs = max(1, math.ceil(len(sats) / 4))
    out = []
    for m in range(msgs):
        chunk = sats[m * 4:(m + 1) * 4]
        fields = [f"{talker}GSV", str(msgs), str(m + 1), f"{len(sats):02d}"]
        for prn, el, az, snr in chunk:
            fields += [f"{prn:02d}", f"{el:02d}", f"{az:03d}", "" if snr is None else f"{snr:02d}"]
        out.append(with_checksum(",".join(fields)))
    return out


def generate(seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    lines: list[str] = []
    lat, lon = START_LAT, START_LON
    heading = 35.0  # degrees, walking roughly NNE up the valley
    for i in range(EPOCHS):
        t = START + dt.timedelta(seconds=i)
        hhmmss = t.strftime("%H%M%S") + ".00"
        ddmmyy = t.strftime("%d%m%y")
        acquiring = i < NO_FIX_EPOCHS

        # --- satellites in view
        n_navic = 5 if acquiring else (7 if i % 17 not in (5, 6) else 6)
        if i in (40, 41):
            n_navic = 5
        navic = []
        for prn, (el, az) in list(NAVIC.items())[:n_navic]:
            snr = None if acquiring and rng.random() < 0.5 else rng.randint(34, 45) - (8 if acquiring else 0)
            navic.append((prn, el, az, snr))

        n_gps = 6 if acquiring else 6 + (i // 12) % 4  # 6..9
        gps = []
        for prn, (el, az, drift) in list(GPS.items())[:n_gps]:
            e = max(5, round(el + drift * i / 60))
            snr = None if acquiring and rng.random() < 0.6 else max(15, rng.randint(26, 44) - (12 if e < 15 else 0))
            gps.append((prn, e, az, snr))

        # --- position / quality
        if acquiring:
            gga = f"GNGGA,{hhmmss},,,,,0,00,99.99,,,,,,"
            rmc = f"GNRMC,{hhmmss},V,,,,,,,{ddmmyy},,,N"
        else:
            heading += rng.uniform(-12, 12)
            speed_ms = rng.uniform(0.4, 1.0)
            dist = speed_ms * 1.0
            lat += dist * math.cos(math.radians(heading)) / 111_320
            lon += dist * math.sin(math.radians(heading)) / (111_320 * math.cos(math.radians(lat)))
            jitter_lat = lat + rng.gauss(0, 1.2) / 111_320
            jitter_lon = lon + rng.gauss(0, 1.2) / 95_700
            settle = max(0.0, (10 - (i - NO_FIX_EPOCHS)) * 0.15)
            hdop = round(rng.uniform(0.8, 1.3) + settle, 2)
            used = min(len([s for s in navic + gps if s[3] and s[3] >= 25]), 16)
            la, ns = dm(jitter_lat, True)
            lo, ew = dm(jitter_lon, False)
            alt = ALTITUDE_M + rng.uniform(-2, 2) + i * 0.05
            knots = speed_ms * 3600 / 1852
            gga = f"GNGGA,{hhmmss},{la},{ns},{lo},{ew},1,{used:02d},{hdop:.2f},{alt:.1f},M,{GEOID_SEP_M},M,,"
            rmc = f"GNRMC,{hhmmss},A,{la},{ns},{lo},{ew},{knots:.3f},{heading % 360:.1f},{ddmmyy},,,A"

        lines.append(with_checksum(gga))
        lines.append(with_checksum(rmc))
        lines += gsv("GP", gps)
        lines += gsv("GI", navic)
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "data" / "navic_sample.nmea"))
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    lines = generate(args.seed)
    Path(args.out).write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    print(f"wrote {len(lines)} sentences ({EPOCHS} epochs) to {args.out}")


if __name__ == "__main__":
    main()
