"""Pure target/diff math: target weights + live account state -> order plan.

Kept side-effect free so it is trivially unit- and replay-testable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True)
class PlannedLeg:
    coin: str
    is_buy: bool
    sz: float                 # coin units, already rounded to szDecimals
    est_notional: float       # at current mark, for logging/telegram
    is_close: bool            # full close of the position (use exact position size)

    @property
    def side(self) -> str:
        return "buy" if self.is_buy else "sell"


@dataclass(frozen=True)
class SkippedLeg:
    coin: str
    delta_usd: float
    reason: str


def round_sz(sz: float, decimals: int, up: bool = False) -> float:
    f = 10 ** decimals
    v = math.ceil(sz * f - 1e-9) if up else math.floor(sz * f + 1e-9)
    return v / f


def build_plan(
    weights: dict[str, float],
    equity: float,
    positions: dict[str, float],          # coin -> signed size (szi)
    mids: dict[str, float],               # coin -> mark price
    sz_decimals: dict[str, int],
    cfg: Config,
) -> tuple[list[PlannedLeg], list[SkippedLeg]]:
    """Compute the per-coin deltas and return orders, sells first.

    Rounding is always toward the *smaller* resulting position:
    buys round the size down, sells round the size up (capped at the position).
    """
    deployable = equity * cfg.deploy_fraction
    threshold = max(cfg.threshold_pct * equity, cfg.threshold_usd)

    sells: list[PlannedLeg] = []
    buys: list[PlannedLeg] = []
    skipped: list[SkippedLeg] = []

    for coin in sorted(set(weights) | set(positions)):
        szi = positions.get(coin, 0.0)
        mark = mids.get(coin)
        if mark is None or mark <= 0:
            skipped.append(SkippedLeg(coin, 0.0, "no mark price"))
            continue
        target_usd = weights.get(coin, 0.0) * deployable
        current_usd = szi * mark
        delta = target_usd - current_usd

        if abs(delta) < threshold:
            if abs(delta) > 1e-9:
                skipped.append(SkippedLeg(coin, delta, f"below threshold ${threshold:.2f}"))
            continue

        decimals = sz_decimals.get(coin, 0)
        if delta > 0:
            sz = round_sz(delta / mark, decimals, up=False)
            if sz <= 0 or sz * mark < cfg.min_order_usd:
                skipped.append(SkippedLeg(coin, delta, "below min order size"))
                continue
            buys.append(PlannedLeg(coin, True, sz, sz * mark, is_close=False))
        else:
            if target_usd <= 0:
                # Full exit: close the exact position, no rounding dust left behind.
                sz = abs(szi)
                if sz <= 0:
                    continue
                sells.append(PlannedLeg(coin, False, sz, sz * mark, is_close=True))
            else:
                sz = round_sz(-delta / mark, decimals, up=True)
                sz = min(sz, abs(szi))
                if sz <= 0 or sz * mark < cfg.min_order_usd:
                    skipped.append(SkippedLeg(coin, delta, "below min order size"))
                    continue
                sells.append(PlannedLeg(coin, False, sz, sz * mark, is_close=False))

    # Sells first (frees margin before consuming it), largest notional first.
    sells.sort(key=lambda l: -l.est_notional)
    buys.sort(key=lambda l: -l.est_notional)
    return sells + buys, skipped
