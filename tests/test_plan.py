import pytest

from app.plan import build_plan, round_sz

SZ_DEC = {"BTC": 5, "ETH": 4, "SOL": 2, "XRP": 0, "DOGE": 0, "PAXG": 4}
MIDS = {"BTC": 100_000.0, "ETH": 4_000.0, "SOL": 200.0, "XRP": 3.0, "DOGE": 0.25, "PAXG": 2_500.0}


def test_round_sz_directions():
    assert round_sz(1.23456789, 2) == 1.23
    assert round_sz(1.23456789, 2, up=True) == 1.24
    assert round_sz(0.999999, 0) == 0
    assert round_sz(0.1, 2) == pytest.approx(0.1)          # exact stays exact
    assert round_sz(0.1, 2, up=True) == pytest.approx(0.1)


def test_empty_account_full_buy(cfg):
    legs, _ = build_plan({"SOL": 0.8, "BTC": 0.2}, 10_000, {}, MIDS, SZ_DEC, cfg)
    assert [l.coin for l in legs] == ["SOL", "BTC"]  # both buys, largest first
    sol = legs[0]
    assert sol.is_buy and not sol.is_close
    # 0.8 * 10000 * 0.98 / 200 = 39.2 -> rounded DOWN to 2 decimals
    assert sol.sz == pytest.approx(39.2)
    btc = legs[1]
    assert btc.sz == pytest.approx(round_sz(0.2 * 10_000 * 0.98 / 100_000, 5))


def test_sells_before_buys(cfg):
    positions = {"SOL": 49.0}  # 9800 USD in SOL
    legs, _ = build_plan({"BTC": 1.0}, 10_000, positions, MIDS, SZ_DEC, cfg)
    assert [(l.coin, l.side) for l in legs] == [("SOL", "sell"), ("BTC", "buy")]
    assert legs[0].is_close  # full exit uses exact position size
    assert legs[0].sz == 49.0


def test_threshold_skips_small_drift(cfg):
    # Target 9800 SOL, current 9700 -> delta 100 < max(2% * 10k, 15) = 200 -> skip
    positions = {"SOL": 48.5}
    legs, skipped = build_plan({"SOL": 1.0}, 10_000, positions, MIDS, SZ_DEC, cfg)
    assert legs == []
    assert skipped and "threshold" in skipped[0].reason


def test_threshold_floor_usd(cfg):
    # Tiny account: threshold = max(2% * 500, 15) = 15
    positions = {"SOL": 2.4}  # 480 target vs 490*0.98=490... compute: target 0.98*500=490, current 480, delta 10 < 15
    legs, skipped = build_plan({"SOL": 1.0}, 500, positions, MIDS, SZ_DEC, cfg)
    assert legs == []


def test_partial_sell_rounds_up(cfg):
    # From 100% SOL to 50/50: sell leg size rounds UP (toward smaller position).
    positions = {"SOL": 49.0}
    legs, _ = build_plan({"SOL": 0.5, "BTC": 0.5}, 10_000, positions, MIDS, SZ_DEC, cfg)
    sell = next(l for l in legs if l.coin == "SOL")
    assert not sell.is_buy and not sell.is_close
    exact = (49.0 * 200 - 0.5 * 10_000 * 0.98) / 200  # 24.5 - 24.5 = ... compute below
    assert sell.sz >= exact - 1e-9
    assert sell.sz == round_sz(exact, 2, up=True)


def test_min_order_usd_skip(cfg):
    from conftest import make_config

    c = make_config(threshold_usd=1.0, threshold_pct=0.0)
    legs, skipped = build_plan({"XRP": 0.01}, 900, {}, MIDS, SZ_DEC, c)
    # target = 0.01 * 900 * 0.98 = 8.82 -> above threshold 1, below min order 10
    assert legs == []
    assert any("min order" in s.reason for s in skipped)


def test_integer_size_coin_rounding(cfg):
    # DOGE has 0 szDecimals: sizes must be whole coins, rounded down on buys.
    legs, _ = build_plan({"DOGE": 1.0}, 10_000, {}, MIDS, SZ_DEC, cfg)
    doge = legs[0]
    assert doge.sz == float(int(doge.sz))
    assert doge.sz == int(0.98 * 10_000 / 0.25)


def test_missing_mark_price_skipped(cfg):
    legs, skipped = build_plan({"SOL": 1.0}, 10_000, {}, {}, SZ_DEC, cfg)
    assert legs == []
    assert skipped[0].reason == "no mark price"


def test_cash_target_closes_everything(cfg):
    positions = {"SOL": 24.5, "BTC": 0.049}
    legs, _ = build_plan({}, 10_000, positions, MIDS, SZ_DEC, cfg)
    assert all(l.is_close and not l.is_buy for l in legs)
    assert {l.coin for l in legs} == {"SOL", "BTC"}
