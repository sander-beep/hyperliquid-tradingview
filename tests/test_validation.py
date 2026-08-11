import json

import pytest

from app.models import PayloadError, parse_payload

from conftest import payload


def test_valid_payload(cfg):
    t = parse_payload(payload(1, [{"sym": "SOLUSD", "w": 0.8}, {"sym": "BTCUSD", "w": 0.2}]), cfg)
    assert t.weights == {"SOL": 0.8, "BTC": 0.2}
    assert t.cash_weight == 0
    assert t.seq == 1
    assert "80% SOL" in t.label() and "20% BTC" in t.label()


def test_valid_payload_from_bytes(cfg):
    body = json.dumps(payload(5, [{"sym": "GOLD", "w": 1.0}])).encode()
    t = parse_payload(body, cfg)
    assert t.weights == {"PAXG": 1.0}


def test_cash_target(cfg):
    t = parse_payload(payload(2, [{"sym": "USD", "w": 1.0}]), cfg)
    assert t.weights == {}
    assert t.cash_weight == 1.0
    assert t.label() == "100% USD"


def test_mixed_cash_leg(cfg):
    t = parse_payload(payload(3, [{"sym": "SOLUSD", "w": 0.8}, {"sym": "USD", "w": 0.2}]), cfg)
    assert t.weights == {"SOL": 0.8}
    assert t.cash_weight == pytest.approx(0.2)


def test_bad_token_is_noncritical(cfg):
    with pytest.raises(PayloadError) as e:
        parse_payload(payload(1, [{"sym": "BTCUSD", "w": 1.0}], token="wrong"), cfg)
    assert not e.value.critical


def test_missing_token(cfg):
    p = payload(1, [{"sym": "BTCUSD", "w": 1.0}])
    del p["token"]
    with pytest.raises(PayloadError) as e:
        parse_payload(p, cfg)
    assert not e.value.critical


def test_unknown_symbol_is_critical(cfg):
    with pytest.raises(PayloadError) as e:
        parse_payload(payload(1, [{"sym": "PEPEUSD", "w": 1.0}]), cfg)
    assert e.value.critical
    assert "unknown symbol" in e.value.reason


def test_weights_must_sum_to_one(cfg):
    with pytest.raises(PayloadError, match="sum"):
        parse_payload(payload(1, [{"sym": "SOLUSD", "w": 0.8}, {"sym": "BTCUSD", "w": 0.1}]), cfg)


def test_weight_tolerance_accepted(cfg):
    t = parse_payload(payload(1, [{"sym": "SOLUSD", "w": 0.6}, {"sym": "BTCUSD", "w": 0.4004}]), cfg)
    assert t.weights["BTC"] == pytest.approx(0.4004)


def test_negative_weight_rejected(cfg):
    with pytest.raises(PayloadError):
        parse_payload(payload(1, [{"sym": "SOLUSD", "w": 1.5}, {"sym": "BTCUSD", "w": -0.5}]), cfg)


def test_too_many_legs(cfg):
    legs = [{"sym": s, "w": 0.2} for s in ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD"]]
    with pytest.raises(PayloadError, match="too many legs"):
        parse_payload(payload(1, legs), cfg)


def test_duplicate_symbol(cfg):
    with pytest.raises(PayloadError, match="duplicate"):
        parse_payload(payload(1, [{"sym": "SOLUSD", "w": 0.5}, {"sym": "SOLUSD", "w": 0.5}]), cfg)


def test_wrong_strategy(cfg):
    p = payload(1, [{"sym": "BTCUSD", "w": 1.0}])
    p["strategy"] = "OTHER"
    with pytest.raises(PayloadError, match="strategy"):
        parse_payload(p, cfg)


def test_wrong_version(cfg):
    p = payload(1, [{"sym": "BTCUSD", "w": 1.0}])
    p["v"] = 2
    with pytest.raises(PayloadError, match="version"):
        parse_payload(p, cfg)


def test_garbage_json(cfg):
    with pytest.raises(PayloadError, match="JSON"):
        parse_payload(b"not json at all", cfg)


def test_bad_seq(cfg):
    p = payload(1, [{"sym": "BTCUSD", "w": 1.0}])
    p["seq"] = "17233"
    with pytest.raises(PayloadError, match="seq"):
        parse_payload(p, cfg)


def test_target_json_roundtrip(cfg):
    from app.models import Target

    t = parse_payload(payload(9, [{"sym": "SOLUSD", "w": 0.8}, {"sym": "GOLD", "w": 0.2}]), cfg)
    t2 = Target.from_json(t.to_json())
    assert t2 == t
