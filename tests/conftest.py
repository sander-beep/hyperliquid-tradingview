from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config
from app.db import Database
from app.hl import AccountView, LegOutcome
from app.plan import PlannedLeg
from app.state import State

TOKEN = "test-token-secret"


def make_config(**overrides) -> Config:
    base = Config(
        hl_api_wallet_key="0x" + "1" * 64,
        hl_account_address="0x" + "2" * 40,
        hl_testnet=True,
        dry_run=False,
        webhook_token=TOKEN,
        webhook_path_secret="pathsecret",
        telegram_bot_token="tg-token",
        telegram_chat_id=123,
        healthchecks_url="",
        strategy_id="RSPS-1",
        symbol_map={
            "BTCUSD": "BTC", "ETHUSD": "ETH", "SOLUSD": "SOL", "XRPUSD": "XRP",
            "BNBUSD": "BNB", "DOGEUSD": "DOGE", "GOLD": "PAXG", "USD": None,
        },
        deploy_fraction=0.98,
        threshold_pct=0.02,
        threshold_usd=15.0,
        min_order_usd=10.0,
        slippage=0.005,
        leg_retries=3,
        max_legs=4,
        weight_sum_tolerance=0.001,
        reconcile_interval_s=900,
        heartbeat_stale_s=13 * 3600,
        critical_repeat_s=6 * 3600,
        equity_drop_pause_pct=0.10,
        armed_warn_day=53,
        armed_nag_day=57,
        armed_expiry_day=60,
        daily_summary_utc="09:00",
        deadman_ping_s=300,
        db_path=":memory:",
    )
    return replace(base, **overrides) if overrides else base


@pytest.fixture
def cfg() -> Config:
    return make_config()


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture
def state(db) -> State:
    return State(db)


class StubNotifier:
    """Captures notifications instead of hitting Telegram."""

    def __init__(self):
        self.messages: list[tuple[str, str]] = []
        self.criticals: dict[str, str] = {}

    async def send(self, text, level="INFO"):
        self.messages.append((level, text))
        return True

    async def info(self, text):
        await self.send(text, "INFO")

    async def warn(self, text):
        await self.send(text, "WARN")

    async def critical(self, key, text):
        self.criticals[key] = text
        await self.send(text, "CRITICAL")

    def resolve(self, key):
        return self.criticals.pop(key, None) is not None

    def is_active(self, key):
        return key in self.criticals

    async def repeat_tick(self):
        pass


class FakeHL:
    """In-memory exchange: fills IOCs at mid, tracks cash + positions."""

    def __init__(self, cfg: Config, equity: float = 10_000.0, mids: dict[str, float] | None = None):
        self.cfg = cfg
        self.cash = equity
        self.positions: dict[str, float] = {}
        self.mids = mids or {
            "BTC": 100_000.0, "ETH": 4_000.0, "SOL": 200.0, "XRP": 3.0,
            "BNB": 800.0, "DOGE": 0.25, "PAXG": 2_500.0,
        }
        self.sz_decimals = {"BTC": 5, "ETH": 4, "SOL": 2, "XRP": 0, "BNB": 3, "DOGE": 0, "PAXG": 4}
        self.open_orders: list[tuple[str, int]] = []
        self.fill_ratio = 1.0          # set <1 to simulate partial fills
        self.executed: list[PlannedLeg] = []
        self.cancelled: list[tuple[str, int]] = []
        self.last_ok = 1.0

    @property
    def equity(self) -> float:
        return self.cash + sum(szi * self.mids[c] for c, szi in self.positions.items())

    async def start(self):
        pass

    async def account(self) -> AccountView:
        return AccountView(
            equity=self.equity,
            positions={c: s for c, s in self.positions.items() if s != 0},
            entry_px={},
            mids=dict(self.mids),
            open_orders=list(self.open_orders),
        )

    async def cancel_open_orders(self, orders):
        self.cancelled.extend(orders)
        self.open_orders = []

    async def execute(self, leg: PlannedLeg) -> LegOutcome:
        self.executed.append(leg)
        mid = self.mids[leg.coin]
        szi = self.positions.get(leg.coin, 0.0)
        if leg.is_close:
            fill = abs(szi)
            direction = -1 if szi > 0 else 1
        else:
            fill = leg.sz * self.fill_ratio
            direction = 1 if leg.is_buy else -1
        self.positions[leg.coin] = szi + direction * fill
        self.cash -= direction * fill * mid
        if abs(self.positions[leg.coin]) < 1e-12:
            del self.positions[leg.coin]
        status = "filled" if fill >= leg.sz * 0.999 or leg.is_close else "partial"
        return LegOutcome(
            coin=leg.coin, side=leg.side, requested_sz=leg.sz,
            filled_sz=fill, avg_px=mid, oid=len(self.executed), fee=fill * mid * 0.00045,
            status=status,
        )


def payload(seq: int, targets: list[dict], changed: bool = False, token: str = TOKEN) -> dict:
    return {
        "v": 1, "strategy": "RSPS-1", "token": token, "seq": seq,
        "bar_time": "2026-08-11T00:00:00Z", "changed": changed, "targets": targets,
    }
