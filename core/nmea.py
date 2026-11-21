"""NMEA 0183 parsing for NavIC-capable GNSS receivers.

A receiver prints one "epoch" of sentences per second, e.g.::

    $GNGGA ...   position, fix quality, satellites used, HDOP   (multi-constellation)
    $GNRMC ...   date, position, speed, course
    $GPGSV ...   GPS satellites in view    (PRN, elevation, azimuth, SNR)
    $GIGSV ...   NavIC satellites in view  (talker "GI" per NMEA 4.11)

`NmeaParser` groups sentences into epochs (keyed by the UTC time in GGA/RMC) and
turns each epoch into a `Fix`. Lines with a bad/missing checksum or an unknown
format are counted and skipped - they never raise.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

import pynmea2

NAVIC_TALKER = "GI"
GPS_TALKER = "GP"
METRES_PER_HDOP = 5.0  # rough UERE: accuracy ~= HDOP x 5 m
KNOTS_TO_KMH = 1.852


# --------------------------------------------------------------------------- checksums
def nmea_checksum(body: str) -> str:
    """XOR of every character between '$' and '*', as two upper-case hex digits."""
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"{cs:02X}"


def with_checksum(body: str) -> str:
    """Build a full sentence from its body (no '$', no '*')."""
    return f"${body}*{nmea_checksum(body)}"


def verify_checksum(line: str) -> bool:
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, given = line[1:].rpartition("*")
    if len(given) != 2:
        return False
    return nmea_checksum(body) == given.upper()


# --------------------------------------------------------------------------- data types
@dataclass
class Satellite:
    prn: int
    elev: Optional[int] = None
    az: Optional[int] = None
    snr: Optional[int] = None


@dataclass
class Fix:
    timestamp: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    fix_quality: int = 0
    sats_used: int = 0
    hdop: Optional[float] = None
    accuracy_m: Optional[float] = None
    altitude_m: Optional[float] = None
    speed_kmh: Optional[float] = None
    course_deg: Optional[float] = None
    navic_sats: List[Satellite] = field(default_factory=list)
    gps_sats: List[Satellite] = field(default_factory=list)
    sentences: List[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.fix_quality > 0 and self.lat is not None and self.lon is not None

    def to_dict(self, include_sentences: bool = True) -> dict:
        d = asdict(self)
        if not include_sentences:
            d.pop("sentences")
        d["valid"] = self.valid
        d["navic_in_view"] = len(self.navic_sats)
        d["gps_in_view"] = len(self.gps_sats)
        return d


class NmeaError(ValueError):
    pass


# --------------------------------------------------------------------------- helpers
def _int(v) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_sentence(line: str) -> pynmea2.NMEASentence:
    """Parse one sentence, requiring a valid checksum. Raises NmeaError."""
    line = line.strip()
    if not verify_checksum(line):
        raise NmeaError(f"bad or missing checksum: {line[:60]!r}")
    try:
        return pynmea2.parse(line, check=True)
    except (pynmea2.ParseError, ValueError, TypeError, AttributeError) as exc:
        raise NmeaError(str(exc)) from exc


# --------------------------------------------------------------------------- parser
class NmeaParser:
    """Stateful, streaming parser: feed lines, collect completed Fix objects."""

    def __init__(self) -> None:
        self.parsed = 0
        self.skipped = 0
        self._epoch_time: Optional[dt.time] = None
        self._fix = Fix()
        self._date: Optional[dt.date] = None
        self._has_data = False

    # -- public
    def feed(self, line: str) -> Optional[Fix]:
        """Feed one line. Returns the previous epoch's Fix when a new epoch starts."""
        if not line or not line.strip():
            return None
        try:
            msg = parse_sentence(line)
        except NmeaError:
            self.skipped += 1
            return None
        self.parsed += 1

        completed = None
        t = getattr(msg, "timestamp", None) if msg.sentence_type in ("GGA", "RMC") else None
        if isinstance(t, dt.time):
            t = t.replace(tzinfo=None)
            if self._epoch_time is not None and t != self._epoch_time and self._has_data:
                completed = self._close()
            self._epoch_time = t

        self._apply(msg, line.strip())
        return completed

    def flush(self) -> Optional[Fix]:
        """Close the epoch in progress (call at end of stream)."""
        return self._close() if self._has_data else None

    # -- internals
    def _close(self) -> Fix:
        fix = self._fix
        if self._epoch_time is not None:
            if self._date is not None:
                stamp = dt.datetime.combine(self._date, self._epoch_time)
                fix.timestamp = stamp.isoformat(timespec="seconds") + "Z"
            else:
                fix.timestamp = self._epoch_time.isoformat(timespec="seconds")
        if fix.valid and fix.hdop is not None:
            fix.accuracy_m = round(fix.hdop * METRES_PER_HDOP, 1)
        self._fix = Fix()
        self._has_data = False
        return fix

    def _apply(self, msg, raw: str) -> None:
        fix = self._fix
        fix.sentences.append(raw)
        self._has_data = True
        kind = msg.sentence_type

        if kind == "GGA":
            fix.fix_quality = _int(msg.gps_qual) or 0
            fix.sats_used = _int(msg.num_sats) or 0
            fix.hdop = _float(msg.horizontal_dil)
            fix.altitude_m = _float(msg.altitude)
            if fix.fix_quality > 0 and msg.lat and msg.lon:
                fix.lat = round(msg.latitude, 7)
                fix.lon = round(msg.longitude, 7)
        elif kind == "RMC":
            if isinstance(msg.datestamp, dt.date):
                self._date = msg.datestamp
            if msg.status == "A" and msg.lat and msg.lon:
                if fix.lat is None:
                    fix.lat = round(msg.latitude, 7)
                    fix.lon = round(msg.longitude, 7)
                if fix.fix_quality == 0 and not any(s.startswith(("$GNGGA", "$GPGGA")) for s in fix.sentences):
                    fix.fix_quality = 1
                knots = _float(msg.spd_over_grnd)
                fix.speed_kmh = round(knots * KNOTS_TO_KMH, 2) if knots is not None else None
                fix.course_deg = _float(msg.true_course)
        elif kind == "GSV":
            target = {NAVIC_TALKER: fix.navic_sats, GPS_TALKER: fix.gps_sats}.get(msg.talker)
            if target is None:
                return  # other constellations (GL/GA/GB) are ignored in this prototype
            if _int(msg.msg_num) == 1:
                target.clear()
            for i in range(1, 5):
                prn = _int(getattr(msg, f"sv_prn_num_{i}", None))
                if prn is None:
                    continue
                target.append(
                    Satellite(
                        prn=prn,
                        elev=_int(getattr(msg, f"elevation_deg_{i}", None)),
                        az=_int(getattr(msg, f"azimuth_{i}", None)),
                        snr=_int(getattr(msg, f"snr_{i}", None)),
                    )
                )


def parse_lines(lines: Iterable[str]) -> tuple[list[Fix], dict]:
    parser = NmeaParser()
    fixes: list[Fix] = []
    for line in lines:
        done = parser.feed(line)
        if done:
            fixes.append(done)
    last = parser.flush()
    if last:
        fixes.append(last)
    return fixes, {"parsed": parser.parsed, "skipped": parser.skipped, "epochs": len(fixes)}


def parse_file(path: str | Path) -> tuple[list[Fix], dict]:
    with open(path, encoding="ascii", errors="replace") as fh:
        return parse_lines(fh)


def latest_fix(fixes: list[Fix]) -> Optional[Fix]:
    """Most recent fix with a valid position, else the most recent epoch, else None."""
    for fix in reversed(fixes):
        if fix.valid:
            return fix
    return fixes[-1] if fixes else None


def count_navic(fix: Fix) -> int:
    return len(fix.navic_sats)
