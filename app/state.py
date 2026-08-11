"""Typed persistent state on top of the kv table."""
from __future__ import annotations

from datetime import datetime, timezone

from .db import Database
from .models import Target

K_LAST_TARGET = "last_target"
K_LAST_HEARTBEAT = "last_heartbeat_at"
K_PAUSED = "paused"
K_PAUSE_REASON = "pause_reason"
K_ARMED_AT = "armed_at"
K_LAST_RUN_EQUITY = "last_run_equity"
K_LAST_SUMMARY_DATE = "last_summary_date"
K_LAST_ARMED_REMINDER = "last_armed_reminder_date"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class State:
    def __init__(self, db: Database):
        self._db = db

    # -- target --------------------------------------------------------------

    def get_target(self) -> Target | None:
        raw = self._db.kv_get(K_LAST_TARGET)
        return Target.from_json(raw) if raw else None

    def set_target(self, t: Target) -> None:
        self._db.kv_set(K_LAST_TARGET, t.to_json())

    # -- heartbeat -----------------------------------------------------------

    def touch_heartbeat(self) -> None:
        self._db.kv_set(K_LAST_HEARTBEAT, utcnow().isoformat(timespec="seconds"))

    def heartbeat_age_s(self) -> float | None:
        raw = self._db.kv_get(K_LAST_HEARTBEAT)
        if not raw:
            return None
        return (utcnow() - datetime.fromisoformat(raw)).total_seconds()

    # -- pause / resume ------------------------------------------------------

    @property
    def paused(self) -> bool:
        return self._db.kv_get(K_PAUSED) == "1"

    def pause(self, reason: str) -> None:
        self._db.kv_set(K_PAUSED, "1")
        self._db.kv_set(K_PAUSE_REASON, reason)

    def resume(self) -> None:
        self._db.kv_set(K_PAUSED, None)
        self._db.kv_set(K_PAUSE_REASON, None)

    @property
    def pause_reason(self) -> str:
        return self._db.kv_get(K_PAUSE_REASON) or ""

    # -- TV alert armed countdown ---------------------------------------------

    def set_armed_now(self) -> None:
        self._db.kv_set(K_ARMED_AT, utcnow().isoformat(timespec="seconds"))

    def armed_age_days(self) -> float | None:
        raw = self._db.kv_get(K_ARMED_AT)
        if not raw:
            return None
        return (utcnow() - datetime.fromisoformat(raw)).total_seconds() / 86400

    # -- equity tracking -----------------------------------------------------

    def get_last_run_equity(self) -> float | None:
        raw = self._db.kv_get(K_LAST_RUN_EQUITY)
        return float(raw) if raw else None

    def set_last_run_equity(self, equity: float) -> None:
        self._db.kv_set(K_LAST_RUN_EQUITY, repr(equity))

    # -- once-per-day markers ------------------------------------------------

    def get_marker(self, key: str) -> str | None:
        return self._db.kv_get(key)

    def set_marker(self, key: str, value: str) -> None:
        self._db.kv_set(key, value)
