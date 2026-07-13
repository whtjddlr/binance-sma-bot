from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from binance_sma_bot.strategy import Candle

DAY_MS = 86_400_000

MAJOR_USDT_UNIVERSE = (
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "ADA/USDT",
    "DOGE/USDT",
    "LINK/USDT",
)


@dataclass(frozen=True)
class PortfolioConfig:
    top_k: int = len(MAJOR_USDT_UNIVERSE)
    total_exposure: float = 0.25
    momentum_days: int = 90
    daily_fast_period: int = 50
    daily_slow_period: int = 200
    slow_slope_days: int = 20
    fee_rate: float = 0.001
    slippage_bps: float = 5.0
    cooldown_days: int = 30
    profit_lock_fraction: float = 0.5
    global_hard_drawdown_pct: float = 20.0
    trend_model: str = "sma"
    breakout_days: int = 120
    breakout_exit_days: int = 40
    allocation_model: str = "equal"
    volatility_days: int = 30
    max_asset_weight: float = 0.10

    def validate(self) -> None:
        if self.top_k < 1:
            raise ValueError("top_k must be positive")
        if not 0 < self.total_exposure <= 1:
            raise ValueError("total_exposure must be in (0, 1]")
        if not 2 <= self.daily_fast_period < self.daily_slow_period:
            raise ValueError("daily periods must satisfy 2 <= fast < slow")
        if self.momentum_days < 2 or self.slow_slope_days < 1:
            raise ValueError("momentum and slope periods must be positive")
        if not 0 <= self.fee_rate < 0.1 or not 0 <= self.slippage_bps < 10_000:
            raise ValueError("invalid trading costs")
        if self.cooldown_days < 1:
            raise ValueError("cooldown_days must be positive")
        if not 0 <= self.profit_lock_fraction <= 1:
            raise ValueError("profit_lock_fraction must be in [0, 1]")
        if not 0 < self.global_hard_drawdown_pct < 100:
            raise ValueError("global drawdown limit must be in (0, 100)")
        if self.trend_model not in {"sma", "ema", "breakout"}:
            raise ValueError("unsupported trend_model")
        if not 2 <= self.breakout_exit_days < self.breakout_days:
            raise ValueError("breakout periods must satisfy 2 <= exit < entry")
        if self.allocation_model not in {"equal", "inverse_volatility"}:
            raise ValueError("unsupported allocation_model")
        if self.volatility_days < 3:
            raise ValueError("volatility_days must be at least 3")
        if not 0 < self.max_asset_weight <= 1:
            raise ValueError("max_asset_weight must be in (0, 1]")


@dataclass(frozen=True)
class PortfolioResult:
    ending_equity: float
    return_pct: float
    max_drawdown_pct: float
    fees: float
    rebalance_count: int
    risk_stops: int
    halted: bool


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _rolling_previous_extreme(
    values: list[float], period: int, *, maximum: bool
) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    queue: deque[int] = deque()
    for index, value in enumerate(values):
        while queue and queue[0] < index - period:
            queue.popleft()
        if index >= period and queue:
            result[index] = values[queue[0]]
        while queue and (
            values[queue[-1]] <= value if maximum else values[queue[-1]] >= value
        ):
            queue.pop()
        queue.append(index)
    return result


def run_portfolio(
    assets: dict[str, list[Candle]],
    start_ms: int,
    end_ms: int,
    config: PortfolioConfig,
) -> PortfolioResult:
    config.validate()
    if start_ms >= end_ms:
        raise ValueError("start_ms must be earlier than end_ms")
    if not assets:
        raise ValueError("at least one asset is required")

    normalized = {
        symbol: sorted(candles, key=lambda candle: candle.timestamp_ms)
        for symbol, candles in assets.items()
    }
    rows = {
        symbol: {c.timestamp_ms: c for c in candles}
        for symbol, candles in normalized.items()
    }
    indices = {
        symbol: {c.timestamp_ms: index for index, c in enumerate(candles)}
        for symbol, candles in normalized.items()
    }
    closes_by_symbol = {
        symbol: [c.close for c in candles] for symbol, candles in normalized.items()
    }
    prefix_by_symbol: dict[str, list[float]] = {}
    for symbol, closes in closes_by_symbol.items():
        prefix = [0.0]
        for close in closes:
            prefix.append(prefix[-1] + close)
        prefix_by_symbol[symbol] = prefix
    ema_fast_by_symbol = {
        symbol: _ema(closes, config.daily_fast_period)
        for symbol, closes in closes_by_symbol.items()
    }
    ema_slow_by_symbol = {
        symbol: _ema(closes, config.daily_slow_period)
        for symbol, closes in closes_by_symbol.items()
    }
    breakout_high_by_symbol = {
        symbol: _rolling_previous_extreme(
            [c.high for c in candles], config.breakout_days, maximum=True
        )
        for symbol, candles in normalized.items()
    }
    breakout_low_by_symbol = {
        symbol: _rolling_previous_extreme(
            [c.low for c in candles], config.breakout_exit_days, maximum=False
        )
        for symbol, candles in normalized.items()
    }
    calendar = sorted(
        timestamp
        for timestamp in set().union(*(set(asset_rows) for asset_rows in rows.values()))
        if start_ms <= timestamp < end_ms
    )

    cash = 1_000.0
    quantities = {symbol: 0.0 for symbol in assets}
    target_weights: dict[str, float] | None = None
    selected: list[str] = []
    fees = 0.0
    rebalances = 0
    risk_stops = 0
    halted = False
    global_peak = 1_000.0
    cycle_anchor = 1_000.0
    cycle_peak = 1_000.0
    max_drawdown = 0.0
    pause_until = 0
    reset_cycle_after_exit = False
    slippage = config.slippage_bps / 10_000

    def price(symbol: str, timestamp: int, field: str) -> float | None:
        candle = rows[symbol].get(timestamp)
        return None if candle is None else float(getattr(candle, field))

    def equity_at(timestamp: int, field: str) -> float:
        value = cash
        for symbol, quantity in quantities.items():
            current = price(symbol, timestamp, field)
            if current is not None:
                value += quantity * current
        return value

    def weights_for(symbols: list[str], timestamp: int) -> dict[str, float]:
        if not symbols:
            return {}
        if config.allocation_model == "equal":
            weight = min(config.total_exposure / config.top_k, config.max_asset_weight)
            return {symbol: weight for symbol in symbols}
        inverse_volatility: dict[str, float] = {}
        for symbol in symbols:
            index = indices[symbol].get(timestamp)
            if index is None or index < config.volatility_days:
                continue
            closes = closes_by_symbol[symbol]
            returns = [
                math.log(closes[offset] / closes[offset - 1])
                for offset in range(index - config.volatility_days + 1, index + 1)
            ]
            mean = sum(returns) / len(returns)
            variance = sum((value - mean) ** 2 for value in returns) / len(returns)
            volatility = math.sqrt(variance)
            if volatility > 0:
                inverse_volatility[symbol] = 1 / volatility
        total_inverse = sum(inverse_volatility.values())
        if total_inverse <= 0:
            return {}
        return {
            symbol: min(
                config.total_exposure * inverse / total_inverse,
                config.max_asset_weight,
            )
            for symbol, inverse in inverse_volatility.items()
        }

    for offset, timestamp in enumerate(calendar):
        if target_weights is not None:
            open_equity = equity_at(timestamp, "open")
            targets = {
                symbol: open_equity * target_weights.get(symbol, 0.0)
                for symbol in assets
            }
            for symbol, quantity in quantities.items():
                open_price = price(symbol, timestamp, "open")
                if open_price is None or quantity <= 0:
                    continue
                current_value = quantity * open_price
                if current_value <= targets[symbol]:
                    continue
                sell_quantity = min(
                    quantity,
                    (current_value - targets[symbol]) / open_price,
                )
                execution = open_price * (1 - slippage)
                gross = sell_quantity * execution
                fee = gross * config.fee_rate
                cash += gross - fee
                quantities[symbol] -= sell_quantity
                fees += fee
            for symbol in assets:
                open_price = price(symbol, timestamp, "open")
                if open_price is None:
                    continue
                current_value = quantities[symbol] * open_price
                if current_value >= targets[symbol]:
                    continue
                desired_quote = targets[symbol] - current_value
                execution = open_price * (1 + slippage)
                total_cost = desired_quote * (1 + config.fee_rate)
                if total_cost > cash:
                    desired_quote = cash / (1 + config.fee_rate)
                    total_cost = cash
                quantities[symbol] += desired_quote / execution
                cash -= total_cost
                fees += total_cost - desired_quote
            target_weights = None
            rebalances += 1

        if reset_cycle_after_exit and all(q <= 1e-12 for q in quantities.values()):
            cycle_anchor = cash
            cycle_peak = cash
            reset_cycle_after_exit = False

        close_equity = equity_at(timestamp, "close")
        global_peak = max(global_peak, close_equity)
        cycle_peak = max(cycle_peak, close_equity)
        max_drawdown = max(
            max_drawdown,
            (global_peak - close_equity) / global_peak * 100,
        )
        if max_drawdown >= config.global_hard_drawdown_pct and not halted:
            halted = True
            target_weights = {}
            selected = []
        elif not halted and timestamp >= pause_until:
            floor = cycle_anchor * 0.9
            if cycle_peak >= cycle_anchor * 1.1:
                floor = max(
                    floor,
                    cycle_anchor
                    + config.profit_lock_fraction * (cycle_peak - cycle_anchor),
                )
            if close_equity <= floor:
                target_weights = {}
                selected = []
                pause_until = timestamp + config.cooldown_days * DAY_MS
                reset_cycle_after_exit = True
                risk_stops += 1

        next_timestamp = calendar[offset + 1] if offset + 1 < len(calendar) else None
        month_end = (
            next_timestamp is not None
            and datetime.fromtimestamp(timestamp / 1000, timezone.utc).month
            != datetime.fromtimestamp(next_timestamp / 1000, timezone.utc).month
        )
        if halted or timestamp < pause_until:
            continue

        eligible: list[tuple[float, str]] = []
        stay_eligible: set[str] = set()
        for symbol, candles in normalized.items():
            index = indices[symbol].get(timestamp)
            required = max(
                config.daily_slow_period - 1 + config.slow_slope_days,
                config.momentum_days,
                config.breakout_days,
                config.breakout_exit_days,
            )
            if index is None or index < required:
                continue
            closes = closes_by_symbol[symbol]
            prefix = prefix_by_symbol[symbol]
            slow = (
                prefix[index + 1] - prefix[index + 1 - config.daily_slow_period]
            ) / config.daily_slow_period
            fast = (
                prefix[index + 1] - prefix[index + 1 - config.daily_fast_period]
            ) / config.daily_fast_period
            previous_end = index - config.slow_slope_days
            previous_slow = (
                prefix[previous_end + 1]
                - prefix[previous_end + 1 - config.daily_slow_period]
            ) / config.daily_slow_period
            if config.trend_model == "sma":
                entry_condition = (
                    closes[index] > slow and fast > slow and slow > previous_slow
                )
                stay_condition = entry_condition
            elif config.trend_model == "ema":
                ema_fast = ema_fast_by_symbol[symbol][index]
                ema_slow = ema_slow_by_symbol[symbol][index]
                previous_ema_slow = ema_slow_by_symbol[symbol][previous_end]
                entry_condition = (
                    closes[index] > ema_slow
                    and ema_fast > ema_slow
                    and ema_slow > previous_ema_slow
                )
                stay_condition = entry_condition
            else:
                breakout_high = breakout_high_by_symbol[symbol][index]
                breakout_low = breakout_low_by_symbol[symbol][index]
                trend_condition = closes[index] > slow and slow > previous_slow
                entry_condition = (
                    trend_condition
                    and breakout_high is not None
                    and closes[index] > breakout_high
                )
                stay_condition = (
                    trend_condition
                    and breakout_low is not None
                    and closes[index] > breakout_low
                )
            if stay_condition:
                stay_eligible.add(symbol)
            if entry_condition:
                momentum = closes[index] / closes[index - config.momentum_days] - 1
                eligible.append((momentum, symbol))

        eligible.sort(reverse=True)
        filtered = [symbol for symbol in selected if symbol in stay_eligible]
        if filtered != selected:
            selected = filtered
            target_weights = weights_for(selected, timestamp)
        if month_end:
            selected = [symbol for _, symbol in eligible[: config.top_k]]
            target_weights = weights_for(selected, timestamp)

    ending = cash if not calendar else equity_at(calendar[-1], "close")
    return PortfolioResult(
        ending_equity=ending,
        return_pct=(ending / 1_000.0 - 1) * 100,
        max_drawdown_pct=max_drawdown,
        fees=fees,
        rebalance_count=rebalances,
        risk_stops=risk_stops,
        halted=halted,
    )
