import pytest

from app.ingest import ingest
from app.models import PayloadError

from conftest import payload


def test_accept_and_dedupe(cfg, db, state):
    status, t = ingest(payload(100, [{"sym": "SOLUSD", "w": 1.0}]), cfg, db, state)
    assert status == "accepted"
    assert state.get_target().weights == {"SOL": 1.0}

    status, _ = ingest(payload(100, [{"sym": "SOLUSD", "w": 1.0}]), cfg, db, state)
    assert status == "duplicate"


def test_out_of_order_heartbeat_never_regresses_target(cfg, db, state):
    ingest(payload(200, [{"sym": "BTCUSD", "w": 1.0}]), cfg, db, state)
    status, _ = ingest(payload(150, [{"sym": "SOLUSD", "w": 1.0}]), cfg, db, state)
    assert status == "accepted"                      # recorded as a heartbeat
    assert state.get_target().weights == {"BTC": 1.0}  # but target stays newest


def test_rejected_payload_keeps_previous_target(cfg, db, state):
    ingest(payload(300, [{"sym": "ETHUSD", "w": 1.0}]), cfg, db, state)
    with pytest.raises(PayloadError):
        ingest(payload(301, [{"sym": "NOPEUSD", "w": 1.0}]), cfg, db, state)
    assert state.get_target().weights == {"ETH": 1.0}
    assert state.get_target().seq == 300


def test_heartbeat_touched_on_accept(cfg, db, state):
    assert state.heartbeat_age_s() is None
    ingest(payload(400, [{"sym": "USD", "w": 1.0}]), cfg, db, state)
    assert state.heartbeat_age_s() < 5
