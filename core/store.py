"""Thread-safe in-memory SOS store. No database - by design (see README "Sync model")."""
from __future__ import annotations

import copy
import datetime as dt
import threading
from typing import Optional

STATUS_FLOW = ["new", "assigned", "en_route", "rescued"]


class StoreError(ValueError):
    """Invalid state change (maps to HTTP 400/409)."""


class NotFound(KeyError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sos: dict[str, dict] = {}

    def create(self, sos: dict) -> tuple[dict, bool]:
        """Insert an SOS. Idempotent by id: returns (record, created)."""
        with self._lock:
            existing = self._sos.get(sos["id"])
            if existing is not None:
                return copy.deepcopy(existing), False
            now = utc_now()
            record = {
                **sos,
                "status": "new",
                "team_id": None,
                "received_at": now,
                "updated_at": now,
                "history": [{"status": "new", "at": now}],
            }
            record.setdefault("created_at", now)
            self._sos[sos["id"]] = record
            return copy.deepcopy(record), True

    def get(self, sos_id: str) -> Optional[dict]:
        with self._lock:
            rec = self._sos.get(sos_id)
            return copy.deepcopy(rec) if rec else None

    def list(self) -> list[dict]:
        with self._lock:
            items = copy.deepcopy(list(self._sos.values()))
        return sorted(items, key=lambda r: (r.get("created_at") or "", r["received_at"]), reverse=True)

    def update(self, sos_id: str, status: Optional[str] = None, team_id: Optional[str] = None) -> dict:
        """Assign a team and/or move status forward along new -> assigned -> en_route -> rescued."""
        with self._lock:
            rec = self._sos.get(sos_id)
            if rec is None:
                raise NotFound(sos_id)
            cur = STATUS_FLOW.index(rec["status"])

            if team_id is not None:
                if rec["status"] == "rescued":
                    raise StoreError("cannot reassign a rescued SOS")
                rec["team_id"] = team_id
                if status is None and rec["status"] == "new":
                    status = "assigned"

            if status is not None and status != rec["status"]:
                if status not in STATUS_FLOW:
                    raise StoreError(f"unknown status {status!r}")
                nxt = STATUS_FLOW.index(status)
                if nxt != cur + 1:
                    raise StoreError(f"invalid transition {rec['status']} -> {status}")
                if nxt >= 1 and not rec["team_id"]:
                    raise StoreError("assign a team before moving past 'new'")
                rec["status"] = status
                rec["history"].append({"status": status, "at": utc_now()})

            rec["updated_at"] = utc_now()
            return copy.deepcopy(rec)

    def clear(self) -> None:
        with self._lock:
            self._sos.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sos)
