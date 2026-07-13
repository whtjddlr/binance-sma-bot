import math

import pytest

from binance_sma_bot.backtest import BacktestConfig, run_backtest
from binance_sma_bot.strategy import Candle


HOUR_MS = 3_600_000


def make_candles(closes: list[float], opens: list[float] | None = None) -> list[Candle]:
    actual_opens = closes if opens is None else opens
    return [
        Candle(
            timestamp_ms=index * HOUR_MS,
            open=open_price,
            high=max(open_price, close),
            low=min(open_price, close),
            close=close,
            volume=1.0,
        )
        for index, (open_price, close) in enumerate(zip(actual_opens, closes))
    ]


def config(**overrides: float | int | None) -> BacktestConfig:
    values: dict[str, float | int | None] = {
        "fast_period": 2,
        "slow_period": 3,
        "starting_balance": 1_000.0,
        "quote_amount": 100.0,
        "fee_rate": 0.0,
        "slippage_bps": 0.0,
        "close_stop_loss_pct": 0.0,
        "max_daily_loss": 0.0,
    }
    values.update(overrides)
    return BacktestConfig(**values)  # type: ignore[arg-type]


def test_signal_uses_next_open_with_fees_and_slippage() -> None:
    candles = make_candles(
        closes=[3, 2, 1, 1, 4, 4, 1, 1],
        opens=[3, 2, 1, 1, 4, 10, 1, 12],
    )
    result = run_backtest(
        candles,
        config(fee_rate=0.01, slippage_bps=200),
    )

    assert result.closed_trade_count == 1
    trade = result.trades[0]
    assert trade.entry_signal_ms == 4 * HOUR_MS
    assert trade.entry_time_ms == 5 * HOUR_MS
    assert trade.entry_price == pytest.approx(10.2)
    assert trade.quantity == pytest.approx(9.80392156862745)
    assert trade.exit_signal_ms == 6 * HOUR_MS
    assert trade.exit_time_ms == 7 * HOUR_MS
    assert trade.exit_price == pytest.approx(11.76)
    assert trade.pnl == pytest.approx(13.141176470588232)
    assert result.ending_equity == pytest.approx(1_013.1411764705883)
    assert result.total_fees == pytest.approx(2.152941176470588)
    assert result.max_drawdown_pct == pytest.approx(9.11960784313725)


def test_last_candle_signal_is_not_filled() -> None:
    candles = make_candles([3, 2, 1, 1, 4])
    result = run_backtest(candles, config())
    assert result.closed_trade_count == 0
    assert result.open_position_quantity == 0
    assert result.win_rate_pct is None


def test_prestart_warmup_allows_signal_on_requested_start() -> None:
    candles = make_candles(
        closes=[3, 2, 1, 1, 4, 4],
        opens=[3, 2, 1, 1, 4, 10],
    )
    result = run_backtest(candles, config(trade_start_ms=4 * HOUR_MS))
    assert result.start_time_ms == 4 * HOUR_MS
    assert result.warmup_candle_count == 4
    assert result.open_position_quantity == pytest.approx(10.0)


def test_open_position_is_marked_to_market_without_forced_sale() -> None:
    candles = make_candles(
        closes=[3, 2, 1, 1, 4, 5],
        opens=[3, 2, 1, 1, 4, 10],
    )
    result = run_backtest(candles, config())
    assert result.closed_trade_count == 0
    assert result.open_position_quantity == pytest.approx(10.0)
    assert result.open_position_value == pytest.approx(50.0)
    assert result.unrealized_pnl == pytest.approx(-50.0)
    assert result.ending_equity == pytest.approx(950.0)


def test_close_stop_executes_at_following_open() -> None:
    candles = make_candles(
        closes=[3, 2, 1, 1, 4, 5, 7],
        opens=[3, 2, 1, 1, 4, 10, 8],
    )
    result = run_backtest(candles, config(close_stop_loss_pct=0.1))
    assert result.closed_trade_count == 1
    trade = result.trades[0]
    assert trade.exit_reason == "close_stop"
    assert trade.exit_price == 8
    assert trade.exit_time_ms == 6 * HOUR_MS


def test_non_finite_or_irregular_candles_are_rejected() -> None:
    invalid = make_candles([3, 2, 1, 1, 4, 5])
    invalid[2] = Candle(2 * HOUR_MS, math.nan, 1, 1, 1, 1)
    with pytest.raises(ValueError, match="유한"):
        run_backtest(invalid, config())

    irregular = make_candles([3, 2, 1, 1, 4, 5])
    irregular[-1] = Candle(10 * HOUR_MS, 5, 5, 5, 5, 1)
    with pytest.raises(ValueError, match="간격"):
        run_backtest(irregular, config())
