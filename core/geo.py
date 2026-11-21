"""Great-circle distance and nearest-team lookup."""
from __future__ import annotations

import math
from typing import Iterable, Optional

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def nearest_team(lat: float, lon: float, teams: Iterable[dict]) -> Optional[dict]:
    """Return {id, name, distance_km} for the closest team, or None if there are none."""
    best = None
    for team in teams:
        d = haversine_km(lat, lon, team["lat"], team["lon"])
        if best is None or d < best["distance_km"]:
            best = {"id": team["id"], "name": team["name"], "distance_km": round(d, 2)}
    return best
