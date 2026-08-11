"""Telegram command interface (long polling). Commands are accepted only from
the configured numeric chat id."""
from __future__ import annotations

import asyncio

import httpx

from . import log
from .config import Config
from .notify import Notifier
from .reconciler import Reconciler
from .state import State
from .watchdog import Watchdog

logger = log.get("telegram")

HELP = (
    "Commands:\n"
    "/status — heartbeat age, target, positions, drift, equity\n"
    "/positions — open positions\n"
    "/reconcile — force a reconcile run now\n"
    "/pause — halt trading (never touches positions)\n"
    "/resume — resume trading\n"
    "/armed — record that the TV alert was (re)created now\n"
    "/help — this message"
)


class TelegramBot:
    def __init__(
        self,
        cfg: Config,
        state: State,
        reconciler: Reconciler,
        watchdog: Watchdog,
        notifier: Notifier,
        hl,
    ):
        self.cfg = cfg
        self.state = state
        self.reconciler = reconciler
        self.watchdog = watchdog
        self.notifier = notifier
        self.hl = hl
        self._base = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
        self._client = httpx.AsyncClient(timeout=70)
        self._offset = 0

    async def loop(self) -> None:
        while True:
            try:
                updates = await self._get_updates()
                for u in updates:
                    self._offset = max(self._offset, u["update_id"] + 1)
                    await self._handle(u)
            except httpx.HTTPError as e:
                logger.warning(f"telegram poll error: {e}")
                await asyncio.sleep(5)
            except Exception:
                logger.exception("telegram handler crashed")
                await asyncio.sleep(5)

    async def _get_updates(self) -> list[dict]:
        r = await self._client.get(
            f"{self._base}/getUpdates",
            params={"timeout": 60, "offset": self._offset, "allowed_updates": '["message"]'},
        )
        data = r.json()
        return data.get("result", []) if data.get("ok") else []

    async def _handle(self, update: dict) -> None:
        msg = update.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if chat_id != self.cfg.telegram_chat_id:
            logger.warning(f"ignoring message from unauthorized chat {chat_id}")
            return
        if not text.startswith("/"):
            return
        cmd = text.split()[0].split("@")[0].lower()
        logger.info(f"command: {cmd}")

        try:
            reply = await self._dispatch(cmd)
        except Exception as e:
            logger.exception("command failed")
            reply = f"Command failed: {e}"
        if reply:
            await self.notifier.send(reply)

    async def _dispatch(self, cmd: str) -> str:
        if cmd == "/help" or cmd == "/start":
            return HELP
        if cmd == "/status":
            return await self._status()
        if cmd == "/positions":
            return await self._positions()
        if cmd == "/reconcile":
            return await self.reconciler.run("manual")
        if cmd == "/pause":
            self.state.pause("manual /pause")
            return "Paused. Trading halted; positions untouched. /resume to continue."
        if cmd == "/resume":
            was = self.state.pause_reason
            self.state.resume()
            self.notifier.resolve("equity_drop")
            self.notifier.resolve("foreign_position")
            self.reconciler.trigger("resume")
            return f"Resumed (was paused: {was or 'manual'}). Reconciling now."
        if cmd == "/armed":
            self.state.set_armed_now()
            return (
                f"Armed: TV alert countdown reset. I'll remind you at day "
                f"{self.cfg.armed_warn_day} and daily from day {self.cfg.armed_nag_day}."
            )
        return f"Unknown command {cmd}. {HELP}"

    async def _status(self) -> str:
        acct = await self.hl.account()
        target = self.state.get_target()
        lines = ["Status"]
        if self.cfg.dry_run:
            lines.append("Mode: DRY RUN")
        lines.append(f"Network: {'testnet' if self.cfg.hl_testnet else 'mainnet'}")
        lines.append(f"Paused: {'yes — ' + self.state.pause_reason if self.state.paused else 'no'}")
        age = self.state.heartbeat_age_s()
        lines.append(f"Heartbeat: {age / 3600:.1f}h ago" if age is not None else "Heartbeat: never received")
        lines.append(f"Equity: ${acct.equity:,.2f}")
        if target:
            lines.append(f"Target ({target.bar_time}): {target.label()}")
            deployable = acct.equity * self.cfg.deploy_fraction
            for coin in sorted(set(target.weights) | set(acct.positions)):
                tgt = target.weights.get(coin, 0.0) * deployable
                cur = acct.positions.get(coin, 0.0) * acct.mids.get(coin, 0.0)
                lines.append(f"  {coin}: ${cur:,.0f} now vs ${tgt:,.0f} target (drift ${cur - tgt:+,.0f})")
        else:
            lines.append("Target: none yet (waiting for first heartbeat)")
        days = self.state.armed_age_days()
        lines.append(
            f"TV alert age: {days:.0f}d / ~{self.cfg.armed_expiry_day}d" if days is not None
            else "TV alert age: unknown (/armed after creating it)"
        )
        return "\n".join(lines)

    async def _positions(self) -> str:
        acct = await self.hl.account()
        if not acct.positions:
            return f"No open positions. Equity ${acct.equity:,.2f} (all cash)."
        lines = ["Positions"]
        for coin, szi in sorted(acct.positions.items()):
            mark = acct.mids.get(coin, 0.0)
            entry = acct.entry_px.get(coin)
            entry_s = f", entry {entry:g}" if entry else ""
            lines.append(f"  {coin}: {szi:g} @ {mark:g}{entry_s} (~${szi * mark:,.2f})")
        lines.append(f"Equity: ${acct.equity:,.2f}")
        return "\n".join(lines)
