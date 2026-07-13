from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from binance_sma_bot.portfolio_paper import (
    DAY_MS,
    MAJOR_USDT_UNIVERSE,
    MAX_ASSET_WEIGHT,
    REQUIRED_CANDLES,
    BinancePortfolioMarket,
    PortfolioPaperEngine,
    PortfolioPaperSettings,
    PortfolioSnapshot,
    PortfolioStateStore,
    decide_portfolio,
)
from binance_sma_bot.strategy import Candle


def make_candles(last_day: datetime, symbol_index: int) -> list[Candle]:
    start = last_day - timedelta(days=REQUIRED_CANDLES + 39)
    candles: list[Candle] = []
    for index in range(REQUIRED_CANDLES + 40):
        variation = 1 + 0.002 * (symbol_index + 1) * math.sin(index / 3)
        close = (100 + symbol_index * 10) * (1.003**index) * variation
        candles.append(
            Candle(
                timestamp_ms=int((start + timedelta(days=index)).timestamp() * 1000),
                open=close * 0.999,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=10_000.0,
            )
        )
    return candles


def make_snapshot(last_day: datetime, *, price_multiplier: float = 1.0) -> PortfolioSnapshot:
    candles = {
        symbol: make_candles(last_day, index)
        for index, symbol in enumerate(MAJOR_USDT_UNIVERSE)
    }
    prices = {
        symbol: candles[symbol][-1].close * price_multiplier
        for symbol in MAJOR_USDT_UNIVERSE
    }
    return PortfolioSnapshot(
        int(last_day.timestamp() * 1000),
        candles,
        prices,
    )


class FakeMarket:
    def __init__(self, snapshots: list[PortfolioSnapshot]):
        self.snapshots = snapshots
        self.index = 0

    def fetch_snapshot(self) -> PortfolioSnapshot:
        snapshot = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return snapshot


def settings(tmp_path, **overrides) -> PortfolioPaperSettings:
    values = {
        "state_file": tmp_path / "portfolio.json",
        "starting_quote": 1_000.0,
        "fee_rate": 0.001,
        "slippage_bps": 5.0,
        "poll_seconds": 300,
        "log_level": "INFO",
    }
    values.update(overrides)
    return PortfolioPaperSettings(**values)


def test_first_run_is_baseline_then_month_end_rebalances(tmp_path) -> None:
    jan_30 = datetime(2026, 1, 30, tzinfo=timezone.utc)
    jan_31 = jan_30 + timedelta(days=1)
    cfg = settings(tmp_path)
    store = PortfolioStateStore(cfg.state_file)
    market = FakeMarket([make_snapshot(jan_30), make_snapshot(jan_31)])
    engine = PortfolioPaperEngine(cfg, market, store)  # type: ignore[arg-type]

    first = engine.run_once()
    assert first.action == "initialized"
    assert not first.trades
    assert store.load(cfg).cash == 1_000.0

    second = engine.run_once()
    state = store.load(cfg)
    assert second.action == "rebalanced"
    assert second.trades
    assert state.selected
    assert state.rebalance_count == 1
    invested = sum(
        state.quantities[symbol] * state.last_prices[symbol]
        for symbol in MAJOR_USDT_UNIVERSE
    )
    assert 0.15 < invested / state.last_equity <= 0.251


def test_same_closed_candle_is_idempotent(tmp_path) -> None:
    jan_30 = datetime(2026, 1, 30, tzinfo=timezone.utc)
    jan_31 = jan_30 + timedelta(days=1)
    second = make_snapshot(jan_31)
    cfg = settings(tmp_path)
    store = PortfolioStateStore(cfg.state_file)
    engine = PortfolioPaperEngine(
        cfg,
        FakeMarket([make_snapshot(jan_30), second, second]),  # type: ignore[arg-type]
        store,
    )
    engine.run_once()
    engine.run_once()
    result = engine.run_once()

    assert result.action == "no_new_candle"
    assert not result.trades
    assert store.load(cfg).rebalance_count == 1


def test_cycle_loss_floor_exits_and_starts_cooldown(tmp_path) -> None:
    jan_30 = datetime(2026, 1, 30, tzinfo=timezone.utc)
    jan_31 = jan_30 + timedelta(days=1)
    feb_1 = jan_31 + timedelta(days=1)
    cfg = settings(tmp_path)
    store = PortfolioStateStore(cfg.state_file)
    engine = PortfolioPaperEngine(
        cfg,
        FakeMarket(
            [
                make_snapshot(jan_30),
                make_snapshot(jan_31),
                make_snapshot(feb_1, price_multiplier=0.5),
            ]
        ),  # type: ignore[arg-type]
        store,
    )
    engine.run_once()
    engine.run_once()
    result = engine.run_once()
    state = store.load(cfg)

    assert result.action == "risk_stop"
    assert any(trade.side == "sell" for trade in result.trades)
    assert all(quantity <= 1e-12 for quantity in state.quantities.values())
    assert state.risk_stops == 1
    assert state.pause_until_ms > result.candle_timestamp_ms
    assert not state.halted


def test_missed_daily_candle_fails_closed(tmp_path) -> None:
    jan_30 = datetime(2026, 1, 30, tzinfo=timezone.utc)
    feb_1 = jan_30 + timedelta(days=2)
    cfg = settings(tmp_path)
    engine = PortfolioPaperEngine(
        cfg,
        FakeMarket([make_snapshot(jan_30), make_snapshot(feb_1)]),  # type: ignore[arg-type]
        PortfolioStateStore(cfg.state_file),
    )
    engine.run_once()
    with pytest.raises(RuntimeError, match="처리를 놓쳤습니다"):
        engine.run_once()


def test_decision_caps_each_asset_weight() -> None:
    month_end = datetime(2026, 1, 31, tzinfo=timezone.utc)
    snapshot = make_snapshot(month_end)
    decision = decide_portfolio(snapshot.candles, [])

    assert decision.month_end
    assert decision.rebalance
    assert decision.selected
    assert sum(decision.target_weights.values()) <= 0.25 + 1e-12
    assert max(decision.target_weights.values()) <= MAX_ASSET_WEIGHT


def test_state_rejects_cost_setting_change(tmp_path) -> None:
    cfg = settings(tmp_path)
    store = PortfolioStateStore(cfg.state_file)
    store.load_or_create(cfg)
    changed = settings(tmp_path, fee_rate=0.002)
    with pytest.raises(RuntimeError, match="전략/비용"):
        store.load(changed)


class FakePublicClient:
    def __init__(self, rows: list[list[float]], server_time_ms: int):
        self.rows = rows
        self.server_time_ms = server_time_ms

    def get_json(self, path: str, params=None):
        if path == "/api/v3/time":
            return {"serverTime": self.server_time_ms}
        if path == "/api/v3/klines":
            assert params["interval"] == "1d"
            assert params["limit"] == 1000
            return self.rows
        if path == "/api/v3/ticker/price":
            return [
                {"symbol": symbol.replace("/", ""), "price": "123.45"}
                for symbol in MAJOR_USDT_UNIVERSE
            ]
        raise AssertionError(path)


def test_market_excludes_forming_daily_candle() -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(REQUIRED_CANDLES + 2):
        timestamp = int((start + timedelta(days=index)).timestamp() * 1000)
        rows.append([timestamp, 100, 101, 99, 100, 1_000])
    forming_open = int((start + timedelta(days=REQUIRED_CANDLES + 1)).timestamp() * 1000)
    market = BinancePortfolioMarket(FakePublicClient(rows, forming_open + DAY_MS // 2))

    snapshot = market.fetch_snapshot()

    assert snapshot.candle_timestamp_ms == forming_open - DAY_MS
    assert all(len(candles) == REQUIRED_CANDLES + 1 for candles in snapshot.candles.values())


def test_market_uses_public_data_only_endpoint() -> None:
    market = BinancePortfolioMarket()
    assert market.client.base_url == "https://data-api.binance.vision"
