from pathlib import Path

import pytest

from core.geo import haversine_km, nearest_team
from core.nmea import (
    NmeaError,
    NmeaParser,
    count_navic,
    latest_fix,
    nmea_checksum,
    parse_file,
    parse_lines,
    parse_sentence,
    verify_checksum,
    with_checksum,
)
from core.store import NotFound, Store, StoreError

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "navic_sample.nmea"

GGA = with_checksum("GNGGA,063010.00,3044.07600,N,07904.01400,E,1,12,1.10,3583.2,M,-40.2,M,,")
RMC = with_checksum("GNRMC,063010.00,A,3044.07600,N,07904.01400,E,1.500,35.0,150726,,,A")
GPGSV = with_checksum("GPGSV,1,1,03,03,72,041,40,06,35,300,38,11,18,075,30")
GIGSV1 = with_checksum("GIGSV,2,1,05,02,52,214,41,03,61,182,43,04,54,147,39,05,38,239,36")
GIGSV2 = with_checksum("GIGSV,2,2,05,06,44,121,35")


# ---------------------------------------------------------------- checksums
def test_known_checksum():
    # Classic reference sentence from the NMEA spec examples
    line = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
    assert verify_checksum(line)
    assert nmea_checksum(line[1:-3]) == "47"


def test_bad_checksum_rejected():
    bad = GGA[:-2] + ("00" if GGA[-2:] != "00" else "11")
    assert not verify_checksum(bad)
    with pytest.raises(NmeaError):
        parse_sentence(bad)


@pytest.mark.parametrize("line", ["", "garbage", "$GNGGA,1,2,3", "$GNGGA,1*ZZ", "GNGGA,1*00"])
def test_malformed_lines_fail_checksum(line):
    assert not verify_checksum(line)


def test_bad_lines_skipped_without_crash():
    bad = GGA[:-2] + "00"
    fixes, stats = parse_lines([bad, "random noise", GGA, RMC, "$XXYYY,1,2*00"])
    assert stats["skipped"] == 3
    assert stats["parsed"] == 2
    assert len(fixes) == 1 and fixes[0].valid


def test_lowercase_checksum_accepted():
    assert verify_checksum(GGA[:-2] + GGA[-2:].lower())


# ---------------------------------------------------------------- parsing
def test_gga_rmc_parse():
    fixes, _ = parse_lines([GGA, RMC])
    fix = fixes[0]
    assert fix.lat == pytest.approx(30.7346, abs=1e-4)
    assert fix.lon == pytest.approx(79.0669, abs=1e-4)
    assert fix.fix_quality == 1
    assert fix.sats_used == 12
    assert fix.hdop == 1.10
    assert fix.accuracy_m == pytest.approx(5.5)
    assert fix.altitude_m == pytest.approx(3583.2)
    assert fix.speed_kmh == pytest.approx(1.5 * 1.852, abs=0.01)
    assert fix.timestamp == "2026-07-15T06:30:10Z"


def test_gsv_gi_talker_is_navic():
    fixes, _ = parse_lines([GGA, RMC, GPGSV, GIGSV1, GIGSV2])
    fix = fixes[0]
    assert count_navic(fix) == 5
    assert len(fix.gps_sats) == 3
    assert [s.prn for s in fix.navic_sats] == [2, 3, 4, 5, 6]
    s = fix.navic_sats[0]
    assert (s.elev, s.az, s.snr) == (52, 214, 41)
    d = fix.to_dict()
    assert d["navic_in_view"] == 5 and d["gps_in_view"] == 3


def test_gsv_empty_snr_is_none():
    gsv = with_checksum("GIGSV,1,1,02,02,52,214,,03,61,182,40")
    fixes, _ = parse_lines([GGA, gsv])
    assert fixes[0].navic_sats[0].snr is None
    assert fixes[0].navic_sats[1].snr == 40


def test_no_fix_epoch():
    gga = with_checksum("GNGGA,063000.00,,,,,0,00,99.99,,,,,,")
    fixes, _ = parse_lines([gga])
    assert not fixes[0].valid
    assert fixes[0].lat is None and fixes[0].accuracy_m is None


def test_epochs_split_by_time():
    gga2 = with_checksum("GNGGA,063011.00,3044.07700,N,07904.01500,E,1,11,1.20,3583.3,M,-40.2,M,,")
    fixes, _ = parse_lines([GGA, RMC, GIGSV1, GIGSV2, gga2, GPGSV])
    assert len(fixes) == 2
    assert count_navic(fixes[0]) == 5
    assert count_navic(fixes[1]) == 0 and len(fixes[1].gps_sats) == 3


def test_streaming_parser_emits_on_new_epoch():
    p = NmeaParser()
    assert p.feed(GGA) is None
    assert p.feed(GIGSV1) is None
    gga2 = with_checksum("GNGGA,063011.00,3044.07700,N,07904.01500,E,1,11,1.20,3583.3,M,-40.2,M,,")
    done = p.feed(gga2)
    assert done is not None and done.sats_used == 12
    assert p.flush().sats_used == 11


def test_sample_file():
    fixes, stats = parse_file(SAMPLE)
    assert stats["skipped"] == 0
    assert 55 <= len(fixes) <= 65
    invalid = [f for f in fixes if not f.valid]
    assert 2 <= len(invalid) <= 5, "sample should show signal acquisition"
    for f in fixes:
        assert 5 <= count_navic(f) <= 7
        assert 6 <= len(f.gps_sats) <= 9
        assert all(1 <= s.prn <= 9 for s in f.navic_sats)
    good = [f for f in fixes if f.valid]
    assert all(abs(f.lat - 30.735) < 0.01 and abs(f.lon - 79.067) < 0.01 for f in good)
    assert latest_fix(fixes).valid


def test_latest_fix_prefers_valid():
    gga0 = with_checksum("GNGGA,063011.00,,,,,0,00,99.99,,,,,,")
    fixes, _ = parse_lines([GGA, gga0])
    assert latest_fix(fixes).sats_used == 12
    assert latest_fix([]) is None


# ---------------------------------------------------------------- geo
def test_haversine_known_distance():
    # Kedarnath -> Guptkashi is ~24 km as the crow flies
    d = haversine_km(30.7346, 79.0669, 30.5246, 79.0798)
    assert 22 < d < 25
    assert haversine_km(10, 10, 10, 10) == 0


def test_haversine_symmetry():
    assert haversine_km(30.7, 79.0, 28.6, 77.2) == pytest.approx(haversine_km(28.6, 77.2, 30.7, 79.0))


def test_nearest_team():
    teams = [
        {"id": "far", "name": "Far", "lat": 30.3165, "lon": 78.0322},
        {"id": "near", "name": "Near", "lat": 30.6537, "lon": 79.0251},
    ]
    n = nearest_team(30.7346, 79.0669, teams)
    assert n["id"] == "near" and 8 < n["distance_km"] < 11
    assert nearest_team(0, 0, []) is None


# ---------------------------------------------------------------- store
def _sos(i="a1b2c3d4"):
    return {"id": i, "name": "Asha", "lat": 30.73, "lon": 79.06, "severity": "critical", "created_at": "2026-07-15T06:31:00Z"}


def test_store_idempotent():
    s = Store()
    rec, created = s.create(_sos())
    assert created and rec["status"] == "new"
    rec2, created2 = s.create({**_sos(), "name": "changed"})
    assert not created2 and rec2["name"] == "Asha"
    assert len(s) == 1


def test_store_status_flow():
    s = Store()
    s.create(_sos())
    with pytest.raises(StoreError):
        s.update("a1b2c3d4", status="assigned")  # no team yet
    rec = s.update("a1b2c3d4", team_id="ndrf-15")
    assert rec["status"] == "assigned"  # assigning a team auto-advances from new
    with pytest.raises(StoreError):
        s.update("a1b2c3d4", status="rescued")  # can't skip en_route
    assert s.update("a1b2c3d4", status="en_route")["status"] == "en_route"
    with pytest.raises(StoreError):
        s.update("a1b2c3d4", status="assigned")  # no going back
    rec = s.update("a1b2c3d4", status="rescued")
    assert [h["status"] for h in rec["history"]] == ["new", "assigned", "en_route", "rescued"]
    with pytest.raises(StoreError):
        s.update("a1b2c3d4", team_id="other")
    with pytest.raises(NotFound):
        s.update("missing", status="assigned")


def test_store_list_newest_first():
    s = Store()
    s.create({**_sos("old00001"), "created_at": "2026-07-15T06:00:00Z"})
    s.create({**_sos("new00001"), "created_at": "2026-07-15T07:00:00Z"})
    assert [r["id"] for r in s.list()] == ["new00001", "old00001"]
