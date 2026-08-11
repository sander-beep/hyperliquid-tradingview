"""Hyperliquid client wrapper: meta cache, 1x leverage, IOC execution with
retries, and read calls with indefinite exponential backoff.

The official SDK is synchronous (requests-based); every SDK call is pushed to a
worker thread so the event loop never blocks.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from . import log
from .config import Config
from .notify import Notifier
from .plan import PlannedLeg

logger = log.get("hl")


@dataclass(frozen=True)
class AccountView:
    equity: float
    positions: dict[str, float]            # coin -> signed size (szi), all coins with a position
    entry_px: dict[str, float]
    mids: dict[str, float]                 # coin -> mark price
    open_orders: list[tuple[str, int]]     # (coin, oid)


@dataclass
class LegOutcome:
    coin: str
    side: str
    requested_sz: float
    filled_sz: float = 0.0
    avg_px: float | None = None
    oid: int | None = None
    fee: float | None = None
    status: str = "unfilled"               # filled | partial | unfilled | error | dry_run
    error: str | None = None
    attempts: int = 0


class HLError(Exception):
    pass


class HLClient:
    def __init__(self, cfg: Config, notifier: Notifier):
        self.cfg = cfg
        self.notifier = notifier
        base_url = constants.TESTNET_API_URL if cfg.hl_testnet else constants.MAINNET_API_URL
        self.info = Info(base_url, skip_ws=True)
        wallet = Account.from_key(cfg.hl_api_wallet_key)
        self.exchange = Exchange(wallet, base_url, account_address=cfg.hl_account_address)
        self.address = cfg.hl_account_address
        self.sz_decimals: dict[str, int] = {}
        self.last_ok: float | None = None  # monotonic ts of last successful API call

    # ---- resilient read wrapper -------------------------------------------

    async def _read(self, label: str, fn, *args):
        """Run a read call with jittered exponential backoff (1s -> 60s),
        retrying indefinitely. WARN after 5 min, CRITICAL after 30 min."""
        delay = 1.0
        started = time.monotonic()
        warned = criticaled = False
        while True:
            try:
                res = await asyncio.to_thread(fn, *args)
                self.last_ok = time.monotonic()
                if criticaled:
                    self.notifier.resolve("hl_api")
                    await self.notifier.info(f"Hyperliquid API recovered ({label}).")
                return res
            except Exception as e:
                elapsed = time.monotonic() - started
                logger.warning(f"HL {label} failed ({e}); retrying in {delay:.0f}s")
                if elapsed > 300 and not warned:
                    warned = True
                    await self.notifier.warn(f"Hyperliquid API failing for 5+ min ({label}): {e}")
                if elapsed > 1800 and not criticaled:
                    criticaled = True
                    await self.notifier.critical(
                        "hl_api", f"Hyperliquid API failing for 30+ min ({label}): {e}"
                    )
                await asyncio.sleep(delay + random.uniform(0, delay / 2))
                delay = min(delay * 2, 60.0)

    async def _write(self, label: str, fn, *args, **kwargs):
        """Run a write call with a small bounded retry (network errors only).
        Failures propagate — the reconciler's next pass is the real retry."""
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                res = await asyncio.to_thread(fn, *args, **kwargs)
                self.last_ok = time.monotonic()
                return res
            except Exception as e:
                last_exc = e
                logger.warning(f"HL {label} attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(1 + attempt * 2)
        raise HLError(f"{label} failed after retries: {last_exc}") from last_exc

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Fetch exchange meta (szDecimals) and set 1x cross leverage on every
        coin in the universe. Meta is cached for the process lifetime."""
        meta = await self._read("meta", self.info.meta)
        for asset in meta["universe"]:
            self.sz_decimals[asset["name"]] = int(asset["szDecimals"])
        missing = [c for c in self.cfg.universe if c not in self.sz_decimals]
        if missing:
            raise RuntimeError(f"coins missing from Hyperliquid meta: {missing}")
        if self.cfg.dry_run:
            logger.info("dry-run: skipping leverage setup")
            return
        for coin in sorted(self.cfg.universe):
            try:
                await self._write(f"update_leverage({coin})", self.exchange.update_leverage, 1, coin, True)
            except HLError as e:
                # Non-fatal (e.g. already set); positions are still opened at whatever
                # leverage is configured — 1x is re-attempted on next boot.
                logger.warning(f"leverage setup for {coin}: {e}")

    # ---- reads -------------------------------------------------------------

    async def account(self) -> AccountView:
        st = await self._read("user_state", self.info.user_state, self.address)
        mids_raw = await self._read("all_mids", self.info.all_mids)
        oo = await self._read("open_orders", self.info.open_orders, self.address)

        positions: dict[str, float] = {}
        entry_px: dict[str, float] = {}
        for ap in st.get("assetPositions", []):
            p = ap.get("position", {})
            szi = float(p.get("szi", 0) or 0)
            if szi != 0:
                positions[p["coin"]] = szi
                if p.get("entryPx"):
                    entry_px[p["coin"]] = float(p["entryPx"])

        return AccountView(
            equity=float(st["marginSummary"]["accountValue"]),
            positions=positions,
            entry_px=entry_px,
            mids={k: float(v) for k, v in mids_raw.items()},
            open_orders=[(o["coin"], int(o["oid"])) for o in oo],
        )

    # ---- writes ------------------------------------------------------------

    async def cancel_open_orders(self, orders: list[tuple[str, int]]) -> None:
        if self.cfg.dry_run:
            if orders:
                logger.info(f"dry-run: would cancel {len(orders)} open orders")
            return
        for coin, oid in orders:
            try:
                await self._write(f"cancel({coin},{oid})", self.exchange.cancel, coin, oid)
            except HLError as e:
                logger.warning(f"cancel failed (may already be gone): {e}")

    async def execute(self, leg: PlannedLeg) -> LegOutcome:
        """Execute one leg as aggressive IOC(s): up to cfg.leg_retries attempts,
        each repriced fresh via the SDK's slippage-capped market order."""
        out = LegOutcome(coin=leg.coin, side=leg.side, requested_sz=leg.sz)
        if self.cfg.dry_run:
            out.status = "dry_run"
            return out

        decimals = self.sz_decimals.get(leg.coin, 0)
        remaining = leg.sz
        fills_sz = 0.0
        fills_px_wsum = 0.0

        for attempt in range(self.cfg.leg_retries):
            out.attempts = attempt + 1
            try:
                if leg.is_buy and not leg.is_close:
                    res = await self._write(
                        f"market_open({leg.coin})",
                        self.exchange.market_open,
                        leg.coin,
                        True,
                        remaining,
                        None,
                        self.cfg.slippage,
                    )
                else:
                    # Sells and full closes (either direction) go through market_close:
                    # it is reduce-only, so it can never overshoot through zero. Full
                    # closes pass sz=None (exact position size, no rounding dust).
                    res = await self._write(
                        f"market_close({leg.coin})",
                        self.exchange.market_close,
                        leg.coin,
                        None if leg.is_close else remaining,
                        None,
                        self.cfg.slippage,
                    )
            except HLError as e:
                out.status = "error"
                out.error = str(e)
                break

            filled, avg_px, oid, err = _parse_order_result(res)
            if oid is not None:
                out.oid = oid
            if filled > 0:
                fills_sz += filled
                fills_px_wsum += filled * (avg_px or 0.0)
                remaining = max(0.0, round(remaining - filled, 10))
            if err and filled == 0:
                out.error = err
                logger.warning(f"{leg.coin} {leg.side} IOC attempt {attempt + 1}: {err}")

            # Round the residual down to a valid size; dust below one step is done.
            step = 10 ** -decimals
            if remaining < step or fills_sz >= leg.sz - 1e-12:
                break
            await asyncio.sleep(1)

        out.filled_sz = fills_sz
        if fills_sz > 0:
            out.avg_px = fills_px_wsum / fills_sz
        if out.error and fills_sz == 0 and out.status != "error":
            out.status = "error"
        elif fills_sz >= leg.sz * 0.999:
            out.status = "filled"
        elif fills_sz > 0:
            out.status = "partial"
        else:
            out.status = out.status if out.status == "error" else "unfilled"

        if out.oid is not None:
            out.fee = await self._fee_for_oid(out.oid)
        return out

    async def _fee_for_oid(self, oid: int) -> float | None:
        """Best-effort: sum fees of recent fills matching this order id."""
        try:
            fills = await asyncio.to_thread(self.info.user_fills, self.address)
        except Exception as e:
            logger.warning(f"user_fills failed (fee lookup): {e}")
            return None
        total = 0.0
        found = False
        for f in fills or []:
            if int(f.get("oid", -1)) == oid:
                found = True
                try:
                    total += float(f.get("fee", 0))
                except (TypeError, ValueError):
                    pass
        return total if found else None


def _parse_order_result(res) -> tuple[float, float | None, int | None, str | None]:
    """Extract (filled_sz, avg_px, oid, error) from an SDK order response."""
    if not isinstance(res, dict):
        return 0.0, None, None, f"unexpected response: {res!r}"
    if res.get("status") != "ok":
        return 0.0, None, None, str(res.get("response") or res)
    try:
        statuses = res["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return 0.0, None, None, f"unexpected response shape: {res!r}"
    filled_sz = 0.0
    px_wsum = 0.0
    oid = None
    error = None
    for st in statuses:
        if "filled" in st:
            f = st["filled"]
            sz = float(f["totalSz"])
            filled_sz += sz
            px_wsum += sz * float(f["avgPx"])
            oid = int(f["oid"])
        elif "error" in st:
            error = str(st["error"])
        elif "resting" in st:
            oid = int(st["resting"]["oid"])
    avg = (px_wsum / filled_sz) if filled_sz > 0 else None
    return filled_sz, avg, oid, error
