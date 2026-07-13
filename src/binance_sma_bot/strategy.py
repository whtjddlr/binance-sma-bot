from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from statistics import fmean
from typing import Sequence


class Signal(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class StrategyDecision:
    signal: Signal
    candle_timestamp_ms: int
    close: float
    previous_fast_sma: float
    previous_slow_sma: float
    current_fast_sma: float
    current_slow_sma: float
    reason: str


def decide(candles: Sequence[Candle], fast_period: int, slow_period: int) -> StrategyDecision:
    """Evaluate an SMA crossover using closed candles only.

    The caller must remove the currently forming candle before calling this function.
    """
    if fast_period < 2 or slow_period <= fast_period:
        raise ValueError("SMA periods must satisfy 2 <= fast < slow")
    if len(candles) < slow_period + 1:
        raise ValueError(f"At least {slow_period + 1} closed candles are required")

    closes = [c.close for c in candles]
    previous_fast = fmean(closes[-fast_period - 1 : -1])
    current_fast = fmean(closes[-fast_period:])
    previous_slow = fmean(closes[-slow_period - 1 : -1])
    current_slow = fmean(closes[-slow_period:])

    if previous_fast <= previous_slow and current_fast > current_slow:
        signal = Signal.BUY
        reason = "fast SMA crossed above slow SMA"
    elif previous_fast >= previous_slow and current_fast < current_slow:
        signal = Signal.SELL
        reason = "fast SMA crossed below slow SMA"
    else:
        signal = Signal.HOLD
        reason = "no crossover"

    last = candles[-1]
    return StrategyDecision(
        signal=signal,
        candle_timestamp_ms=last.timestamp_ms,
        close=last.close,
        previous_fast_sma=previous_fast,
        previous_slow_sma=previous_slow,
        current_fast_sma=current_fast,
        current_slow_sma=current_slow,
        reason=reason,
    )

