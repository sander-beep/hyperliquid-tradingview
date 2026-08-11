"""Webhook ingestion: validate -> dedupe -> persist target + heartbeat.

Shared between the HTTP handler and the replay tests so both run the exact
same pipeline. Raises PayloadError on invalid payloads.
"""
from __future__ import annotations

from .config import Config
from .db import Database
from .models import Target, parse_payload
from .state import State


def ingest(body: bytes | str | dict, cfg: Config, db: Database, state: State) -> tuple[str, Target]:
    """Returns (status, target) where status is 'accepted' or 'duplicate'."""
    target = parse_payload(body, cfg)

    if not db.insert_signal(target.seq, target.bar_time, target.changed, target.raw):
        return "duplicate", target

    # Late/out-of-order heartbeats never regress the target.
    current = state.get_target()
    if current is None or target.seq >= current.seq:
        state.set_target(target)
    state.touch_heartbeat()
    return "accepted", target
