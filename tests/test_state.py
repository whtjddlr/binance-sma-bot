import json

import pytest

from binance_sma_bot.state import StateStore


def test_state_round_trip_and_strategy_binding(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    state = store.load_or_create("paper", "BTC/USDT", "strategy-a", "account-a", 1000.0)
    state.position_qty = 0.01
    store.save(state)

    loaded = store.load_or_create("paper", "BTC/USDT", "strategy-a", "account-a", 1000.0)
    assert loaded.position_qty == 0.01
    assert json.loads(path.read_text())["position_qty"] == 0.01

    with pytest.raises(RuntimeError, match="전략"):
        store.load_or_create("paper", "BTC/USDT", "strategy-b", "account-a", 1000.0)

    with pytest.raises(RuntimeError, match="API 계정"):
        store.load_or_create("paper", "BTC/USDT", "strategy-a", "account-b", 1000.0)
