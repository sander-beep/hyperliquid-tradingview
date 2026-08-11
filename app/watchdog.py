"""Watchdog: heartbeat staleness, alert-expiry countdown, dead-man ping,
CRITICAL repeats, daily summary."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import log, state as state_keys
from .config import Config
from .db import Database
from .notify import Notifier
from .state import State

logger = log.get("watchdog")


class Watchdog:
    def __init__(self, cfg: Config, db: Database, state: State, hl, notifier: Notifier):
        self.cfg = cfg
        self.db = db
        self.state = state
        self.hl = hl
        self.notifier = notifier
        self._last_ping = 0.0
        self._client = httpx.AsyncClient(timeout=10)

    async def loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:
                logger.exception("watchdog tick failed")
            await asyncio.sleep(30)

    async def _tick(self) -> None:
        await self._check_heartbeat()
        await self._check_armed_countdown()
        await self._deadman_ping()
        await self._daily_summary()
        await self.notifier.repeat_tick()

    # ------------------------------------------------------------------------

    async def _check_heartbeat(self) -> None:
        age = self.state.heartbeat_age_s()
        if age is None:
            return  # never seen one (fresh install) — nothing to compare against
        if age > self.cfg.heartbeat_stale_s:
            if not self.notifier.is_active("heartbeat"):
                hours = age / 3600
                await self.notifier.critical(
                    "heartbeat",
                    f"No TradingView heartbeat for {hours:.1f}h — the alert likely expired "
                    "(Essential plan, ~60 days) or stopped. Re-arm it on TradingView, then "
                    "send /armed. Holding the last known target meanwhile.",
                )
        elif self.notifier.resolve("heartbeat"):
            await self.notifier.info("TradingView heartbeats resumed.")

    async def _check_armed_countdown(self) -> None:
        days = self.state.armed_age_days()
        if days is None:
            return
        day = int(days)
        if day < self.cfg.armed_warn_day:
            return
        if day < self.cfg.armed_nag_day and day != self.cfg.armed_warn_day:
            return
        today = datetime.now(timezone.utc).date().isoformat()
        if self.state.get_marker(state_keys.K_LAST_ARMED_REMINDER) == today:
            return
        self.state.set_marker(state_keys.K_LAST_ARMED_REMINDER, today)
        left = self.cfg.armed_expiry_day - day
        await self.notifier.warn(
            f"TradingView alert is {day} days old — it expires around day "
            f"{self.cfg.armed_expiry_day} (~{max(left, 0)} days left). "
            "Re-create the alert on TradingView and send /armed."
        )

    async def _deadman_ping(self) -> None:
        if not self.cfg.healthchecks_url:
            return
        now = time.monotonic()
        if now - self._last_ping < self.cfg.deadman_ping_s:
            return
        self._last_ping = now
        try:
            await self._client.get(self.cfg.healthchecks_url)
        except httpx.HTTPError as e:
            logger.warning(f"healthchecks ping failed: {e}")

    async def _daily_summary(self) -> None:
        now = datetime.now(timezone.utc)
        hh, mm = self.cfg.daily_summary_utc.split(":")
        due = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if now < due:
            return
        today = now.date().isoformat()
        if self.state.get_marker(state_keys.K_LAST_SUMMARY_DATE) == today:
            return
        self.state.set_marker(state_keys.K_LAST_SUMMARY_DATE, today)
        try:
            await self.notifier.info(await self.build_summary())
        except Exception as e:
            logger.warning(f"daily summary failed: {e}")

    async def build_summary(self) -> str:
        acct = await self.hl.account()
        target = self.state.get_target()
        prev_eq = self.state.get_last_run_equity()

        lines = ["Daily summary"]
        lines.append(f"Equity: ${acct.equity:,.2f}")
        if prev_eq:
            pnl = acct.equity - prev_eq
            lines.append(f"Change since last run: ${pnl:+,.2f}")
        if target:
            lines.append(f"Target: {target.label()} (bar {target.bar_time})")
        else:
            lines.append("Target: none yet")
        if acct.positions:
            for coin, szi in sorted(acct.positions.items()):
                mark = acct.mids.get(coin, 0.0)
                lines.append(f"  {coin}: {szi:g} (~${szi * mark:,.2f})")
        else:
            lines.append("  no open positions (cash)")

        age = self.state.heartbeat_age_s()
        lines.append(f"Last heartbeat: {age / 3600:.1f}h ago" if age is not None else "Last heartbeat: never")

        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        n = self.db.heartbeats_since(since)
        lines.append(f"Heartbeats last 24h: {n} (expected 2)")
        lines.append(f"Fees last 24h: ${self.db.fees_since(since):.2f}")

        days = self.state.armed_age_days()
        if days is not None:
            lines.append(
                f"TV alert age: {days:.0f}d (expires ~day {self.cfg.armed_expiry_day})"
            )
        else:
            lines.append("TV alert age: unknown — send /armed after creating the alert")
        if self.state.paused:
            lines.append(f"⚠️ PAUSED: {self.state.pause_reason}")
        if self.cfg.dry_run:
            lines.append("Mode: DRY RUN")
        return "\n".join(lines)
