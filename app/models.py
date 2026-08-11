"""Payload schema, validation and the target model.

TradingView sends (every 12h bar, and on change):

    {"v": 1, "strategy": "RSPS-1", "token": "<secret>",
     "seq": 1723377600000, "bar_time": "2026-08-11T12:00:00Z",
     "changed": false,
     "targets": [{"sym": "SOLUSD", "w": 0.8}, {"sym": "BTCUSD", "w": 0.2}]}

Symbols are TV vocabulary; the bot owns the mapping to Hyperliquid coins.
"""
from __future__ import annotations

import hmac
import json
from dataclasses import dataclass

from .config import Config


class PayloadError(Exception):
    """critical=True -> reject + Telegram CRITICAL (keep previous target).
    critical=False -> log + drop silently (bad token: don't let strangers spam us)."""

    def __init__(self, reason: str, critical: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.critical = critical


@dataclass(frozen=True)
class Target:
    """A validated allocation target, already in Hyperliquid vocabulary."""

    seq: int                      # TV bar open time in ms — idempotency/ordering key
    bar_time: str
    changed: bool
    weights: dict[str, float]     # HL coin -> weight (cash excluded)
    cash_weight: float
    raw: dict                     # original payload minus token (for persistence)

    def label(self) -> str:
        parts = [f"{w * 100:.0f}% {c}" for c, w in sorted(self.weights.items(), key=lambda x: -x[1])]
        if self.cash_weight > 0.0005:
            parts.append(f"{self.cash_weight * 100:.0f}% USD")
        return " / ".join(parts) if parts else "100% USD"

    def to_json(self) -> str:
        return json.dumps(
            {
                "seq": self.seq,
                "bar_time": self.bar_time,
                "changed": self.changed,
                "weights": self.weights,
                "cash_weight": self.cash_weight,
                "raw": self.raw,
            }
        )

    @staticmethod
    def from_json(s: str) -> "Target":
        d = json.loads(s)
        return Target(
            seq=int(d["seq"]),
            bar_time=d["bar_time"],
            changed=bool(d["changed"]),
            weights={str(k): float(v) for k, v in d["weights"].items()},
            cash_weight=float(d["cash_weight"]),
            raw=d.get("raw", {}),
        )


def parse_payload(body: bytes | str | dict, cfg: Config) -> Target:
    """Validate a webhook body and return a Target. Raises PayloadError."""
    if isinstance(body, dict):
        data = body
    else:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise PayloadError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise PayloadError("payload is not a JSON object")

    token = data.get("token")
    if not isinstance(token, str) or not hmac.compare_digest(token, cfg.webhook_token):
        raise PayloadError("bad token", critical=False)

    if data.get("v") != 1:
        raise PayloadError(f"unsupported payload version {data.get('v')!r}")
    if data.get("strategy") != cfg.strategy_id:
        raise PayloadError(f"unexpected strategy {data.get('strategy')!r} (want {cfg.strategy_id})")

    seq = data.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
        raise PayloadError(f"bad seq {seq!r}")

    bar_time = data.get("bar_time")
    if not isinstance(bar_time, str) or not bar_time:
        raise PayloadError("missing bar_time")

    changed = data.get("changed")
    if not isinstance(changed, bool):
        raise PayloadError("missing/invalid 'changed'")

    targets = data.get("targets")
    if not isinstance(targets, list) or not targets:
        raise PayloadError("missing/empty targets")
    if len(targets) > cfg.max_legs:
        raise PayloadError(f"too many legs: {len(targets)} > {cfg.max_legs}")

    weights: dict[str, float] = {}
    cash_weight = 0.0
    total = 0.0
    seen_syms: set[str] = set()
    for leg in targets:
        if not isinstance(leg, dict):
            raise PayloadError("leg is not an object")
        sym = leg.get("sym")
        w = leg.get("w")
        if not isinstance(sym, str):
            raise PayloadError(f"bad leg symbol {sym!r}")
        if not isinstance(w, (int, float)) or isinstance(w, bool):
            raise PayloadError(f"bad weight for {sym}: {w!r}")
        w = float(w)
        if w <= 0:
            raise PayloadError(f"non-positive weight for {sym}: {w}")
        if sym in seen_syms:
            raise PayloadError(f"duplicate symbol {sym}")
        seen_syms.add(sym)
        if sym not in cfg.symbol_map:
            raise PayloadError(f"unknown symbol {sym!r} — not in symbol_map")
        coin = cfg.symbol_map[sym]
        total += w
        if coin is None:
            cash_weight += w
        else:
            if coin in weights:
                raise PayloadError(f"two symbols map to the same coin {coin}")
            weights[coin] = w

    if abs(total - 1.0) > cfg.weight_sum_tolerance:
        raise PayloadError(f"weights sum to {total:.4f}, expected 1.0")

    raw = {k: v for k, v in data.items() if k != "token"}
    return Target(
        seq=seq,
        bar_time=bar_time,
        changed=changed,
        weights=weights,
        cash_weight=cash_weight,
        raw=raw,
    )
