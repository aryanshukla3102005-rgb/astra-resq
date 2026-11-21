import uuid

import pytest

from app import app, store
from core.nmea import with_checksum


@pytest.fixture()
def client():
    store.clear()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
    store.clear()


def sos_body(**over):
    body = {
        "id": str(uuid.uuid4()),
        "name": "Ravi Negi",
        "phone": "+91 98100 00000",
        "message": "Trail washed out, two injured",
        "people_count": 3,
        "severity": "critical",
        "lat": 30.7346,
        "lon": 79.0669,
        "accuracy_m": 5.5,
        "navic_sats": 7,
        "created_at": "2026-07-15T06:31:00Z",
        "source": "simulator",
    }
    body.update(over)
    return body


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.get_json()["status"] == "ok"


@pytest.mark.parametrize("path", ["/", "/sos", "/dashboard", "/css/app.css", "/js/common.js"])
def test_pages_and_static(client, path):
    assert client.get(path).status_code == 200


def test_unknown_api_route_is_json_404(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and "error" in r.get_json()


def test_replay(client):
    fixes = client.get("/api/nmea/replay").get_json()
    assert len(fixes) >= 55
    assert fixes[0]["valid"] is False
    good = [f for f in fixes if f["valid"]]
    assert good and all(f["navic_in_view"] >= 5 for f in good)
    assert all(f["sentences"] for f in fixes)


def test_nmea_parse(client):
    lines = [
        with_checksum("GNGGA,063010.00,3044.07600,N,07904.01400,E,1,12,1.10,3583.2,M,-40.2,M,,"),
        with_checksum("GIGSV,1,1,02,02,52,214,41,03,61,182,43"),
        "$GNGGA,bad*00",
    ]
    r = client.post("/api/nmea/parse", json={"lines": lines})
    data = r.get_json()
    assert r.status_code == 200
    assert data["fix"]["valid"] and data["fix"]["navic_in_view"] == 2
    assert data["stats"]["skipped"] == 1


@pytest.mark.parametrize("payload", [None, {}, {"lines": []}, {"lines": "x"}, {"lines": [1, 2]}])
def test_nmea_parse_validation(client, payload):
    r = client.post("/api/nmea/parse", json=payload) if payload is not None else client.post("/api/nmea/parse", data="nope")
    assert r.status_code == 400 and "error" in r.get_json()


def test_create_and_list_sos(client):
    body = sos_body()
    r = client.post("/api/sos", json=body)
    assert r.status_code == 201
    rec = r.get_json()
    assert rec["status"] == "new" and rec["team_id"] is None
    assert rec["nearest_team"]["id"] == "med-gaurikund"
    lst = client.get("/api/sos").get_json()
    assert len(lst) == 1 and lst[0]["id"] == body["id"]
    assert lst[0]["nearest_team"]["distance_km"] > 0


def test_create_is_idempotent(client):
    body = sos_body()
    assert client.post("/api/sos", json=body).status_code == 201
    r = client.post("/api/sos", json={**body, "name": "Someone else"})
    assert r.status_code == 200 and r.get_json()["name"] == "Ravi Negi"
    assert len(client.get("/api/sos").get_json()) == 1


def test_list_newest_first(client):
    client.post("/api/sos", json=sos_body(created_at="2026-07-15T06:00:00Z", name="Old"))
    client.post("/api/sos", json=sos_body(created_at="2026-07-15T07:00:00Z", name="New"))
    assert [s["name"] for s in client.get("/api/sos").get_json()] == ["New", "Old"]


@pytest.mark.parametrize(
    "over, field",
    [
        ({"id": "short"}, "id"),
        ({"id": None}, "id"),
        ({"name": ""}, "name"),
        ({"name": "x" * 81}, "name"),
        ({"lat": 91}, "lat"),
        ({"lat": "30.7"}, "lat"),
        ({"lon": None}, "lon"),
        ({"lon": True}, "lon"),
        ({"severity": "apocalyptic"}, "severity"),
        ({"people_count": 0}, "people_count"),
        ({"people_count": 2.5}, "people_count"),
        ({"phone": "call me maybe"}, "phone"),
        ({"source": "carrier-pigeon"}, "source"),
        ({"created_at": "yesterday"}, "created_at"),
        ({"navic_sats": -1}, "navic_sats"),
        ({"message": "x" * 501}, "message"),
    ],
)
def test_create_validation(client, over, field):
    r = client.post("/api/sos", json=sos_body(**over))
    assert r.status_code == 400
    assert field in r.get_json()["error"]


def test_create_rejects_non_object(client):
    assert client.post("/api/sos", json=[1, 2]).status_code == 400
    assert client.post("/api/sos", data="hello", content_type="text/plain").status_code == 400


def test_optional_fields_default(client):
    body = sos_body()
    for k in ("phone", "message", "accuracy_m", "source", "created_at"):
        body.pop(k)
    rec = client.post("/api/sos", json=body).get_json()
    assert rec["source"] == "simulator" and rec["position_source"] == "navic"
    assert rec["phone"] == "" and rec["accuracy_m"] is None and rec["created_at"]


def test_patch_status_flow(client):
    sid = client.post("/api/sos", json=sos_body()).get_json()["id"]

    r = client.patch(f"/api/sos/{sid}", json={"status": "assigned"})
    assert r.status_code == 409  # needs a team first

    r = client.patch(f"/api/sos/{sid}", json={"team_id": "ndrf-15"})
    assert r.status_code == 200
    assert r.get_json()["status"] == "assigned" and r.get_json()["team_id"] == "ndrf-15"

    assert client.patch(f"/api/sos/{sid}", json={"status": "rescued"}).status_code == 409  # skip
    assert client.patch(f"/api/sos/{sid}", json={"status": "en_route"}).get_json()["status"] == "en_route"
    assert client.patch(f"/api/sos/{sid}", json={"status": "new"}).status_code == 409  # backwards
    rec = client.patch(f"/api/sos/{sid}", json={"status": "rescued"}).get_json()
    assert rec["status"] == "rescued"
    assert [h["status"] for h in rec["history"]] == ["new", "assigned", "en_route", "rescued"]
    assert rec["updated_at"] >= rec["received_at"]


def test_patch_assign_and_status_together(client):
    sid = client.post("/api/sos", json=sos_body()).get_json()["id"]
    rec = client.patch(f"/api/sos/{sid}", json={"team_id": "sdrf-uk", "status": "assigned"}).get_json()
    assert rec["status"] == "assigned" and rec["team_id"] == "sdrf-uk"


@pytest.mark.parametrize("payload", [{}, {"status": "teleported"}, {"team_id": "nobody"}])
def test_patch_validation(client, payload):
    sid = client.post("/api/sos", json=sos_body()).get_json()["id"]
    r = client.patch(f"/api/sos/{sid}", json=payload)
    assert r.status_code == 400 and "error" in r.get_json()


def test_patch_missing(client):
    assert client.patch("/api/sos/does-not-exist", json={"status": "assigned"}).status_code == 404


def test_teams(client):
    teams = client.get("/api/teams").get_json()
    assert 5 <= len(teams) <= 6
    assert {"id", "name", "lat", "lon", "base"} <= set(teams[0])
