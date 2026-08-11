"""The reconciler: makes the account converge to the last known target.

Single-flight async job triggered by (1) a new accepted signal, (2) a periodic
15-minute tick, (3) manual /reconcile. Re-running is always safe: each run is a
pure function of (last persisted target, live account state).
"""
from __future__ import annotations

import asyncio
import contextlib

from . import log
from .config import Config
from .db import Database
from .hl import AccountView, LegOutcome
from .notify import Notifier
from .plan import PlannedLeg, build_plan
from .state import State

logger = log.get("reconciler")


class Reconciler:
    def __init__(self, cfg: Config, db: Database, state: State, hl, notifier: Notifier):
        self.cfg = cfg
        self.db = db
        self.state = state
        self.hl = hl
        self.notifier = notifier
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._wake_trigger = "tick"

    def trigger(self, reason: str = "signal") -> None:
        self._wake_trigger = reason
        self._wake.set()

    async def loop(self) -> None:
        """Background loop: run on wake events and every reconcile_interval_s."""
        while True:
            trigger = "tick"
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.cfg.reconcile_interval_s)
                trigger = self._wake_trigger
                self._wake.clear()
            try:
                await self.run(trigger)
            except Exception:
                logger.exception("reconcile run crashed")

    # ------------------------------------------------------------------------

    async def run(self, trigger: str) -> str:
        """Execute one reconcile pass. Returns a short human summary."""
        async with self._lock:
            return await self._run_locked(trigger)

    async def _run_locked(self, trigger: str) -> str:
        target = self.state.get_target()
        if target is None:
            logger.info("no target yet; nothing to reconcile")
            return "no target yet — waiting for the first TradingView heartbeat"

        if self.state.paused:
            logger.info(f"paused ({self.state.pause_reason}); skipping reconcile")
            return f"paused: {self.state.pause_reason}"

        run_id = self.db.start_run(trigger)
        try:
            return await self._reconcile(run_id, trigger, target)
        except Exception as e:
            self.db.finish_run(run_id, "error", error=str(e))
            await self.notifier.warn(f"Reconcile run failed ({trigger}): {e}")
            raise

    async def _reconcile(self, run_id: int, trigger: str, target) -> str:
        acct: AccountView = await self.hl.account()

        # ---- anomaly checks (the account is 100% bot-owned) ----
        halted = await self._check_anomalies(run_id, acct)
        if halted:
            self.db.finish_run(run_id, "paused", equity=acct.equity)
            return halted

        # ---- cancel stale open orders from previous runs ----
        if acct.open_orders:
            await self.hl.cancel_open_orders(acct.open_orders)

        # ---- plan ----
        legs, skipped = build_plan(
            target.weights, acct.equity, acct.positions, acct.mids, self.hl.sz_decimals, self.cfg
        )
        plan_json = [
            {"coin": l.coin, "side": l.side, "sz": l.sz, "usd": round(l.est_notional, 2), "close": l.is_close}
            for l in legs
        ]
        for s in skipped:
            logger.info(
                "skip leg", extra={"data": {"coin": s.coin, "delta_usd": round(s.delta_usd, 2), "reason": s.reason}}
            )

        if not legs:
            self.db.finish_run(run_id, "ok", equity=acct.equity, plan=plan_json, result={"converged": True})
            self.state.set_last_run_equity(acct.equity)
            logger.info(f"reconcile({trigger}): converged, nothing to trade (equity ${acct.equity:,.2f})")
            return f"converged — no trades needed (equity ${acct.equity:,.2f}, target {target.label()})"

        if self.cfg.dry_run:
            lines = [f"  {l.side.upper()} {l.sz:g} {l.coin} (~${l.est_notional:,.0f})" for l in legs]
            msg = (
                f"DRY RUN — would rebalance ({trigger}) to {target.label()}:\n" + "\n".join(lines)
            )
            for l in legs:
                self.db.insert_order(run_id, l.coin, l.side, l.sz, "dry_run")
            self.db.finish_run(run_id, "ok", equity=acct.equity, plan=plan_json, result={"dry_run": True})
            self.state.set_last_run_equity(acct.equity)
            await self.notifier.info(msg)
            return msg

        # ---- execute: sells first, then buys (ordering baked into the plan) ----
        outcomes: list[LegOutcome] = []
        for l in legs:
            out = await self.hl.execute(l)
            outcomes.append(out)
            self.db.insert_order(
                run_id, out.coin, out.side, out.requested_sz,
                out.status, out.filled_sz, out.avg_px, out.fee, out.oid, out.error,
            )

        # ---- verify convergence ----
        post: AccountView = await self.hl.account()
        residual_legs, _ = build_plan(
            target.weights, post.equity, post.positions, post.mids, self.hl.sz_decimals, self.cfg
        )
        converged = not residual_legs
        status = "ok" if converged else "partial"
        self.db.finish_run(
            run_id, status, equity=post.equity, plan=plan_json,
            result={
                "converged": converged,
                "fills": [
                    {"coin": o.coin, "side": o.side, "req": o.requested_sz, "filled": o.filled_sz,
                     "avg_px": o.avg_px, "fee": o.fee, "status": o.status}
                    for o in outcomes
                ],
            },
        )
        self.state.set_last_run_equity(post.equity)

        # ---- report ----
        msg = self._format_report(trigger, target, outcomes, post, converged)
        level_send = self.notifier.info if converged else self.notifier.warn
        await level_send(msg)
        return msg

    async def _check_anomalies(self, run_id: int, acct: AccountView) -> str | None:
        """Returns a message if trading must halt this run, else None."""
        # 1) Foreign position (coin outside the universe): pause, never touch it.
        foreign = [c for c in acct.positions if c not in self.cfg.universe]
        if foreign:
            reason = f"foreign position(s) in account: {', '.join(foreign)}"
            self.state.pause(reason)
            await self.notifier.critical(
                "foreign_position",
                f"{reason}. This account must be 100% bot-owned. Trading PAUSED — "
                "inspect the account, remove the position manually, then /resume.",
            )
            return f"paused: {reason}"

        # 2) Short position in a universe coin: flatten it (long-only strategy).
        shorts = [c for c, szi in acct.positions.items() if szi < 0]
        for coin in shorts:
            await self.notifier.critical(
                f"short_{coin}",
                f"Short position detected in {coin} ({acct.positions[coin]}). "
                "Strategy is long-only — flattening it now.",
            )
            leg = PlannedLeg(coin=coin, is_buy=True, sz=abs(acct.positions[coin]),
                             est_notional=abs(acct.positions[coin]) * acct.mids.get(coin, 0.0),
                             is_close=True)
            if not self.cfg.dry_run:
                out = await self.hl.execute(leg)
                self.db.insert_order(run_id, out.coin, "flatten_short", out.requested_sz,
                                     out.status, out.filled_sz, out.avg_px, out.fee, out.oid, out.error)
            self.notifier.resolve(f"short_{coin}")
        if shorts:
            # Positions changed; let the next pass (or the rest of this run on fresh
            # state) handle the rebalance. Simplest correct behavior: stop here.
            self.trigger("post_flatten")
            return "flattened short position(s); re-running shortly"

        # 3) Equity anomaly: >X% drop since the previous run -> pause.
        prev = self.state.get_last_run_equity()
        if prev and prev > 0 and acct.equity < prev * (1 - self.cfg.equity_drop_pause_pct):
            drop = (1 - acct.equity / prev) * 100
            reason = f"equity dropped {drop:.1f}% since last run (${prev:,.2f} -> ${acct.equity:,.2f})"
            self.state.pause(reason)
            # Re-baseline so an acknowledged /resume doesn't instantly re-trip
            # on the same (already reported) drop.
            self.state.set_last_run_equity(acct.equity)
            await self.notifier.critical(
                "equity_drop",
                f"{reason}. Trading PAUSED — investigate, then /resume.",
            )
            return f"paused: {reason}"

        return None

    def _format_report(
        self, trigger: str, target, outcomes: list[LegOutcome], post: AccountView, converged: bool
    ) -> str:
        lines = [f"Rebalance ({trigger}) -> {target.label()}"]
        total_fees = 0.0
        for o in outcomes:
            fee_s = ""
            if o.fee is not None:
                total_fees += o.fee
                fee_s = f", fee ${o.fee:.2f}"
            px_s = f" @ {o.avg_px:g}" if o.avg_px else ""
            lines.append(
                f"  {o.side.upper()} {o.coin}: {o.filled_sz:g}/{o.requested_sz:g}{px_s} [{o.status}]{fee_s}"
            )
        if total_fees:
            lines.append(f"Fees: ${total_fees:.2f}")
        lines.append(f"Equity: ${post.equity:,.2f}")
        if not converged:
            lines.append("Not fully converged — the 15-min pass will close the residual.")
        return "\n".join(lines)
