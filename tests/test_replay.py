"""Replay tests: feed sequences of heartbeats (gaps, duplicates, malformed
payloads, mid-sequence 'crash') through the full pipeline against the mock
exchange; assert final positions."""
from __future__ import annotations

import pytest

from app.ingest import ingest
from app.models import PayloadError
from app.reconciler import Reconciler
from app.state import State

from conftest import FakeHL, StubNotifier, payload


def approx_notional(hl: FakeHL, coin: str) -> float:
    return hl.positions.get(coin, 0.0) * hl.mids[coin]


@pytest.fixture
def rig(cfg, db, state):
    hl = FakeHL(cfg, equity=10_000)
    notifier = StubNotifier()
    rec = Reconciler(cfg, db, state, hl, notifier)
    return cfg, db, state, hl, notifier, rec


async def feed(rig_tuple, seq, targets):
    cfg, db, state, hl, notifier, rec = rig_tuple
    status, _ = ingest(payload(seq, targets), cfg, db, state)
    if status == "accepted":
        await rec.run("signal")
    return status


async def test_converges_to_first_target(rig):
    cfg, db, state, hl, notifier, rec = rig
    await feed(rig, 1, [{"sym": "SOLUSD", "w": 0.8}, {"sym": "BTCUSD", "w": 0.2}])
    assert approx_notional(hl, "SOL") == pytest.approx(0.8 * 10_000 * 0.98, rel=0.02)
    assert approx_notional(hl, "BTC") == pytest.approx(0.2 * 10_000 * 0.98, rel=0.02)


async def test_rotation_and_duplicate(rig):
    cfg, db, state, hl, notifier, rec = rig
    await feed(rig, 1, [{"sym": "SOLUSD", "w": 1.0}])
    sol_before = hl.positions["SOL"]

    # duplicate: dropped, no trades
    n_orders = len(hl.executed)
    status = await feed(rig, 1, [{"sym": "SOLUSD", "w": 1.0}])
    assert status == "duplicate"
    assert len(hl.executed) == n_orders

    # rotation to 80/20 BTC/ETH: SOL fully closed, sells before buys
    await feed(rig, 2, [{"sym": "BTCUSD", "w": 0.8}, {"sym": "ETHUSD", "w": 0.2}])
    assert "SOL" not in hl.positions
    order = [(l.coin, l.side) for l in hl.executed[n_orders:]]
    assert order[0] == ("SOL", "sell")
    assert {c for c, s in order if s == "buy"} == {"BTC", "ETH"}


async def test_malformed_payload_keeps_portfolio(rig):
    cfg, db, state, hl, notifier, rec = rig
    await feed(rig, 1, [{"sym": "SOLUSD", "w": 1.0}])
    pos = dict(hl.positions)
    with pytest.raises(PayloadError):
        ingest(payload(2, [{"sym": "WATUSD", "w": 1.0}]), cfg, db, state)
    await rec.run("tick")
    assert hl.positions == pytest.approx(pos)


async def test_crash_recovery_reconciles_from_db(rig, cfg, db):
    _, _, state, hl, notifier, rec = rig
    ingest(payload(1, [{"sym": "GOLD", "w": 1.0}]), cfg, db, state)
    # simulate crash before reconcile: build a brand-new reconciler ("restarted bot")
    rec2 = Reconciler(cfg, db, State(db), hl, StubNotifier())
    await rec2.run("startup")
    assert approx_notional(hl, "PAXG") == pytest.approx(9_800, rel=0.02)


async def test_partial_fill_self_heals_on_next_tick(rig):
    cfg, db, state, hl, notifier, rec = rig
    hl.fill_ratio = 0.5
    await feed(rig, 1, [{"sym": "ETHUSD", "w": 1.0}])
    # half-filled: next tick with full fills closes the gap
    hl.fill_ratio = 1.0
    await rec.run("tick")
    assert approx_notional(hl, "ETH") == pytest.approx(9_800, rel=0.03)


async def test_cash_target_flattens_book(rig):
    cfg, db, state, hl, notifier, rec = rig
    await feed(rig, 1, [{"sym": "SOLUSD", "w": 0.8}, {"sym": "BTCUSD", "w": 0.2}])
    await feed(rig, 2, [{"sym": "USD", "w": 1.0}])
    assert hl.positions == {}
    assert hl.cash == pytest.approx(10_000, rel=0.01)


async def test_foreign_position_pauses(rig):
    cfg, db, state, hl, notifier, rec = rig
    await feed(rig, 1, [{"sym": "SOLUSD", "w": 1.0}])
    hl.positions["WIF"] = 100.0
    hl.mids["WIF"] = 1.0
    await rec.run("tick")
    assert state.paused
    assert "foreign_position" in notifier.criticals
    # paused: further heartbeats do not trade
    n = len(hl.executed)
    await feed(rig, 2, [{"sym": "BTCUSD", "w": 1.0}])
    assert len(hl.executed) == n


async def test_short_position_flattened(rig):
    cfg, db, state, hl, notifier, rec = rig
    ingest(payload(1, [{"sym": "USD", "w": 1.0}]), cfg, db, state)
    hl.positions["ETH"] = -1.0
    hl.cash = 14_000.0
    await rec.run("tick")
    assert "ETH" not in hl.positions          # short flattened
    assert not state.paused                    # shorts flatten, they don't pause


async def test_equity_drop_pauses(rig):
    cfg, db, state, hl, notifier, rec = rig
    await feed(rig, 1, [{"sym": "SOLUSD", "w": 1.0}])
    hl.mids["SOL"] *= 0.8                      # 20% crash between ticks
    await rec.run("tick")
    assert state.paused
    assert "equity_drop" in notifier.criticals
    # /resume clears it and trading works again
    state.resume()
    notifier.resolve("equity_drop")
    await rec.run("resume")
    assert not state.paused


async def test_dry_run_never_trades(cfg, db):
    from conftest import make_config

    c = make_config(dry_run=True)
    st = State(db)
    hl = FakeHL(c)
    notifier = StubNotifier()
    rec = Reconciler(c, db, st, hl, notifier)
    ingest(payload(1, [{"sym": "SOLUSD", "w": 1.0}]), c, db, st)
    await rec.run("signal")
    assert hl.executed == []
    assert hl.positions == {}
    assert any("DRY RUN" in m for _, m in notifier.messages)


async def test_no_target_noop(rig):
    cfg, db, state, hl, notifier, rec = rig
    out = await rec.run("startup")
    assert "no target" in out
    assert hl.executed == []
