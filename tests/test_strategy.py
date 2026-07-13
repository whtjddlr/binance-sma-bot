import pytest

from binance_sma_bot.strategy import Candle, Signal, decide


def candles(closes: list[float]) -> list[Candle]:
    return [Candle(i * 3_600_000, value, value, value, value, 1.0) for i, value in enumerate(closes)]


def test_golden_cross() -> None:
    result = decide(candles([3, 2, 1, 4]), fast_period=2, slow_period=3)
    assert result.signal is Signal.BUY


def test_death_cross() -> None:
    result = decide(candles([1, 2, 3, 0]), fast_period=2, slow_period=3)
    assert result.signal is Signal.SELL


def test_equal_previous_smas_can_cross() -> None:
    result = decide(candles([2, 2, 2, 3]), fast_period=2, slow_period=3)
    assert result.previous_fast_sma == result.previous_slow_sma
    assert result.signal is Signal.BUY


def test_no_cross() -> None:
    result = decide(candles([1, 2, 3, 4]), fast_period=2, slow_period=3)
    assert result.signal is Signal.HOLD


def test_requires_previous_and_current_sma() -> None:
    with pytest.raises(ValueError, match="At least 4"):
        decide(candles([1, 2, 3]), fast_period=2, slow_period=3)

