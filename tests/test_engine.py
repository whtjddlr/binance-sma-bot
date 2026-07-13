from pathlib import Path

import pytest

from binance_sma_bot.config import Settings, TradingMode
from binance_sma_bot.engine import TradingEngine
from binance_sma_bot.exchange import Execution
from binance_sma_bot.state import BotState, StateStore
from binance_sma_bot.strategy import Candle


def make_candles(closes: list[float], start: int) -> list[Candle]:
    return [
        Candle(start + i * 3_600_000, value, value, value, value, 1.0)
        for i, value in enumerate(closes)
    ]


class FakeExchange:
    timeframe_ms = 3_600_000

    def __init__(self, rows: list[Candle]):
        self.rows = rows
        self.last_server_time_ms = rows[-1].timestamp_ms + self.timeframe_ms + 1_000
        self.execution = Execution("exchange-1", "closed", 0.25, 40.0, 10.0, 0.0, 0.0)
        self.order_exists = True

    def set_rows(self, rows: list[Candle]) -> None:
        self.rows = rows
        self.last_server_time_ms = rows[-1].timestamp_ms + self.timeframe_ms + 1_000

    def fetch_closed_candles(self, limit: int) -> list[Candle]:
        return self.rows[-limit:]

    def fetch_order_by_client_id(self, client_order_id: str) -> Execution:
        return self.execution

    def is_dust_quantity(self, quantity: float, reference_price: float) -> bool:
        return quantity < 0.001 or quantity * reference_price < 5.0

    def verify_order_exists(self, client_order_id: str) -> None:
        if not self.order_exists:
            raise LookupError(client_order_id)


def settings(path: Path) -> Settings:
    return Settings(
        mode=TradingMode.PAPER,
        fast_period=2,
        slow_period=3,
        quote_amount=10.0,
        paper_starting_quote=100.0,
        paper_fee_rate=0.0,
        max_signal_age_seconds=600,
        state_file=path,
    )


def test_warmup_buy_duplicate_and_sell(tmp_path) -> None:
    cfg = settings(tmp_path / "state.json")
    first = make_candles([10, 10, 10, 10], 0)
    exchange = FakeExchange(first)
    store = StateStore(cfg.state_file)
    engine = TradingEngine(cfg, exchange, store)  # type: ignore[arg-type]

    assert engine.run_once().action == "warmup"

    buy_rows = make_candles([30, 20, 10, 40], 3_600_000)
    exchange.set_rows(buy_rows)
    assert engine.run_once().action == "paper_buy"
    assert engine.run_once().action == "duplicate"

    state = store.load_or_create(
        "paper", "BTC/USDT", cfg.strategy_key, cfg.account_fingerprint, 100.0
    )
    assert state.position_qty == 0.25
    assert state.paper_quote_balance == 90.0

    sell_rows = make_candles([10, 30, 40, 5], 2 * 3_600_000)
    exchange.set_rows(sell_rows)
    assert engine.run_once().action == "paper_sell"
    state = store.load_or_create(
        "paper", "BTC/USDT", cfg.strategy_key, cfg.account_fingerprint, 100.0
    )
    assert state.position_qty == 0
    assert state.paper_quote_balance == 91.25
    assert state.realized_pnl_today == -8.75


def test_stale_signal_is_skipped(tmp_path) -> None:
    cfg = settings(tmp_path / "state.json")
    rows = make_candles([10, 10, 10, 10], 0)
    exchange = FakeExchange(rows)
    store = StateStore(cfg.state_file)
    engine = TradingEngine(cfg, exchange, store)  # type: ignore[arg-type]
    engine.run_once()

    buy_rows = make_candles([30, 20, 10, 40], 3_600_000)
    exchange.set_rows(buy_rows)
    exchange.last_server_time_ms += (cfg.max_signal_age_seconds + 1) * 1000
    assert engine.run_once().action == "stale"
    state = store.load_or_create(
        "paper", "BTC/USDT", cfg.strategy_key, cfg.account_fingerprint, 100.0
    )
    assert not state.has_position


def test_pending_buy_is_reconciled_without_resubmission(tmp_path) -> None:
    cfg = settings(tmp_path / "state.json")
    rows = make_candles([10, 10, 10, 10], 0)
    exchange = FakeExchange(rows)
    store = StateStore(cfg.state_file)
    state = store.load_or_create(
        "paper", "BTC/USDT", cfg.strategy_key, cfg.account_fingerprint, 100.0
    )
    state.pending_client_order_id = "sma-123-b"
    state.pending_side = "buy"
    state.pending_candle_ms = 123
    store.save(state)

    engine = TradingEngine(cfg, exchange, store)  # type: ignore[arg-type]
    assert engine.run_once().action == "reconciled_buy"
    state = store.load_or_create(
        "paper", "BTC/USDT", cfg.strategy_key, cfg.account_fingerprint, 100.0
    )
    assert state.position_qty == 0.25
    assert state.pending_client_order_id is None


def test_partial_sell_keeps_remaining_bot_ledger() -> None:
    state = BotState.new("testnet", "BTC/USDT", "strategy", "account", 0)
    state.position_qty = 1.0
    state.entry_price = 100.0
    state.entry_cost = 100.0
    execution = Execution("order-1", "closed", 0.4, 125.0, 50.0, 0.0, 0.0)

    pnl, remaining = TradingEngine._apply_sell_execution(state, execution)  # noqa: SLF001

    assert pnl == 10.0
    assert remaining == 0.6
    assert state.position_qty == 0.6
    assert state.entry_cost == 60.0
    assert state.entry_price == 100.0


def test_terminal_partial_fill_is_accepted_immediately() -> None:
    TradingEngine._require_terminal_fill(  # noqa: SLF001
        Execution("partial", "canceled", 0.4, 100.0, 40.0, 0.0, 0.0)
    )
    with pytest.raises(RuntimeError, match="아직 종결되지"):
        TradingEngine._require_terminal_fill(  # noqa: SLF001
            Execution("open", "open", 0.4, 100.0, 40.0, 0.0, 0.0)
        )


def test_terminal_partial_buy_is_reconciled(tmp_path) -> None:
    cfg = settings(tmp_path / "state.json")
    exchange = FakeExchange(make_candles([10, 10, 10, 10], 0))
    exchange.execution = Execution("partial-buy", "canceled", 0.2, 50.0, 10.0, 0.0, 0.0)
    store = StateStore(cfg.state_file)
    state = store.load_or_create(
        "paper", "BTC/USDT", cfg.strategy_key, cfg.account_fingerprint, 100.0
    )
    state.pending_client_order_id = "partial-buy-client"
    state.pending_side = "buy"
    state.pending_candle_ms = 123
    store.save(state)

    engine = TradingEngine(cfg, exchange, store)  # type: ignore[arg-type]
    assert engine.run_once().action == "reconciled_partial_buy"
    state = store.load_or_create(
        "paper", "BTC/USDT", cfg.strategy_key, cfg.account_fingerprint, 100.0
    )
    assert state.position_qty == 0.2
    assert state.entry_cost == 10.0
    assert state.pending_client_order_id is None


def test_terminal_partial_sell_keeps_remainder(tmp_path) -> None:
    cfg = settings(tmp_path / "state.json")
    exchange = FakeExchange(make_candles([10, 10, 10, 10], 0))
    exchange.execution = Execution("partial-sell", "expired", 0.4, 125.0, 50.0, 0.0, 0.0)
    store = StateStore(cfg.state_file)
    state = store.load_or_create(
        "paper", "BTC/USDT", cfg.strategy_key, cfg.account_fingerprint, 100.0
    )
    state.position_qty = 1.0
    state.entry_price = 100.0
    state.entry_cost = 100.0
    state.pending_client_order_id = "partial-sell-client"
    state.pending_side = "sell"
    state.pending_candle_ms = 123
    store.save(state)

    engine = TradingEngine(cfg, exchange, store)  # type: ignore[arg-type]
    assert engine.run_once().action == "reconciled_partial_sell"
    state = store.load_or_create(
        "paper", "BTC/USDT", cfg.strategy_key, cfg.account_fingerprint, 100.0
    )
    assert state.position_qty == 0.6
    assert state.entry_cost == 60.0


def test_bearish_regime_exits_existing_position_even_when_signal_is_old(tmp_path) -> None:
    cfg = settings(tmp_path / "state.json")
    rows = make_candles([40, 30, 20, 10], 3_600_000)
    exchange = FakeExchange(rows)
    exchange.last_server_time_ms += (cfg.max_signal_age_seconds + 1) * 1000
    store = StateStore(cfg.state_file)
    state = store.load_or_create(
        "paper", "BTC/USDT", cfg.strategy_key, cfg.account_fingerprint, 100.0
    )
    state.last_processed_candle_ms = 0
    state.position_qty = 1.0
    state.entry_price = 10.0
    state.entry_cost = 10.0
    state.paper_base_balance = 1.0
    state.paper_quote_balance = 90.0
    store.save(state)

    engine = TradingEngine(cfg, exchange, store)  # type: ignore[arg-type]
    assert engine.run_once().action == "paper_sell"
    state = store.load_or_create(
        "paper", "BTC/USDT", cfg.strategy_key, cfg.account_fingerprint, 100.0
    )
    assert not state.has_position


def test_unsellable_precision_remainder_moves_to_dust(tmp_path) -> None:
    cfg = settings(tmp_path / "state.json")
    exchange = FakeExchange(make_candles([10, 10, 10, 10], 0))
    store = StateStore(cfg.state_file)
    state = BotState.new("paper", "BTC/USDT", cfg.strategy_key, cfg.account_fingerprint, 100)
    state.position_qty = 0.0001
    state.entry_price = 10_000.0
    state.entry_cost = 1.0
    engine = TradingEngine(cfg, exchange, store)  # type: ignore[arg-type]

    moved = engine._move_unsellable_remainder_to_dust(state, 10_000.0)  # noqa: SLF001

    assert moved == 0.0001
    assert not state.has_position
    assert state.dust_qty == 0.0001
    assert state.dust_cost == 1.0


def test_missing_origin_order_blocks_position_sale(tmp_path) -> None:
    cfg = settings(tmp_path / "state.json")
    exchange = FakeExchange(make_candles([10, 10, 10, 10], 0))
    exchange.order_exists = False
    store = StateStore(cfg.state_file)
    state = BotState.new("testnet", "BTC/USDT", "strategy", "account", 0)
    state.position_qty = 0.1
    state.entry_price = 100.0
    state.entry_cost = 10.0
    state.last_client_order_id = "missing-after-reset"
    engine = TradingEngine(cfg, exchange, store)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="Testnet 초기화"):
        engine._verify_position_origin(state)  # noqa: SLF001
