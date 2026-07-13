from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from math import isfinite
from pathlib import Path
from typing import Sequence

from .strategy import Candle, Signal, decide


@dataclass(frozen=True)
class BacktestConfig:
    fast_period: int
    slow_period: int
    starting_balance: float
    quote_amount: float
    fee_rate: float = 0.001
    slippage_bps: float = 5.0
    close_stop_loss_pct: float = 0.0
    max_daily_loss: float = 0.0
    trade_start_ms: int | None = None

    def validate(self) -> None:
        values = {
            "starting_balance": self.starting_balance,
            "quote_amount": self.quote_amount,
            "fee_rate": self.fee_rate,
            "slippage_bps": self.slippage_bps,
            "close_stop_loss_pct": self.close_stop_loss_pct,
            "max_daily_loss": self.max_daily_loss,
        }
        if not all(isfinite(value) for value in values.values()):
            raise ValueError("백테스트 숫자 설정은 모두 유한해야 합니다.")
        if self.fast_period < 2 or self.slow_period <= self.fast_period:
            raise ValueError("SMA 기간은 2 <= fast < slow 조건을 만족해야 합니다.")
        if self.starting_balance <= 0 or self.quote_amount <= 0:
            raise ValueError("초기자산과 주문금액은 0보다 커야 합니다.")
        if not 0 <= self.fee_rate < 0.1:
            raise ValueError("수수료율은 0 이상 0.1 미만이어야 합니다.")
        if not 0 <= self.slippage_bps < 10_000:
            raise ValueError("슬리피지는 0 이상 10,000 bps 미만이어야 합니다.")
        if not 0 <= self.close_stop_loss_pct < 1:
            raise ValueError("종가 손절률은 0 이상 1 미만이어야 합니다.")
        if self.max_daily_loss < 0:
            raise ValueError("일일 손실 한도는 0 이상이어야 합니다.")
        if self.trade_start_ms is not None and self.trade_start_ms < 0:
            raise ValueError("거래 시작 시각은 0 이상 epoch milliseconds여야 합니다.")


@dataclass(frozen=True)
class BacktestTrade:
    entry_signal_ms: int
    entry_time_ms: int
    entry_price: float
    exit_signal_ms: int
    exit_time_ms: int
    exit_price: float
    quantity: float
    entry_cost: float
    net_proceeds: float
    pnl: float
    return_pct: float
    exit_reason: str


@dataclass(frozen=True)
class EquityPoint:
    timestamp_ms: int
    equity: float


@dataclass(frozen=True)
class BacktestResult:
    start_time_ms: int
    end_time_ms: int
    candle_count: int
    warmup_candle_count: int
    starting_balance: float
    ending_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    closed_trade_count: int
    winning_trade_count: int
    win_rate_pct: float | None
    total_fees: float
    realized_pnl: float
    unrealized_pnl: float
    open_position_quantity: float
    open_position_value: float
    skipped_buy_count: int
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[EquityPoint, ...]


@dataclass
class _Position:
    quantity: float
    entry_price: float
    entry_cost: float
    entry_signal_ms: int
    entry_time_ms: int


@dataclass(frozen=True)
class _PendingAction:
    side: Signal
    signal_timestamp_ms: int
    reason: str


def run_backtest(candles: Sequence[Candle], config: BacktestConfig) -> BacktestResult:
    """Run a long-only close-signal/next-open execution backtest.

    A decision made with candle N's close is executed at candle N+1's open.
    This intentionally prevents look-ahead bias.
    """
    config.validate()
    if len(candles) < config.slow_period + 2:
        raise ValueError(
            f"백테스트에는 최소 {config.slow_period + 2}개의 확정봉이 필요합니다."
        )
    _validate_candles(candles)

    trade_start_ms = config.trade_start_ms
    active_start_index = config.slow_period
    if trade_start_ms is not None:
        while (
            active_start_index < len(candles)
            and candles[active_start_index].timestamp_ms < trade_start_ms
        ):
            active_start_index += 1
    if active_start_index + 1 >= len(candles):
        raise ValueError("워밍업 이후 백테스트 거래 구간에 최소 2개의 확정봉이 필요합니다.")

    cash = config.starting_balance
    position: _Position | None = None
    pending: _PendingAction | None = None
    trades: list[BacktestTrade] = []
    equity_curve: list[EquityPoint] = []
    total_fees = 0.0
    skipped_buys = 0
    realized_pnl_by_day: dict[str, float] = {}
    slippage = config.slippage_bps / 10_000

    for index in range(active_start_index, len(candles)):
        candle = candles[index]

        if pending is not None:
            if pending.side is Signal.BUY and position is None:
                fee = config.quote_amount * config.fee_rate
                total_cost = config.quote_amount + fee
                if cash >= total_cost:
                    execution_price = candle.open * (1 + slippage)
                    if execution_price <= 0:
                        raise ValueError("매수 체결가격은 0보다 커야 합니다.")
                    quantity = config.quote_amount / execution_price
                    cash -= total_cost
                    total_fees += fee
                    position = _Position(
                        quantity=quantity,
                        entry_price=execution_price,
                        entry_cost=total_cost,
                        entry_signal_ms=pending.signal_timestamp_ms,
                        entry_time_ms=candle.timestamp_ms,
                    )
                else:
                    skipped_buys += 1
            elif pending.side is Signal.SELL and position is not None:
                execution_price = candle.open * (1 - slippage)
                if execution_price <= 0:
                    raise ValueError("매도 체결가격은 0보다 커야 합니다.")
                gross = position.quantity * execution_price
                fee = gross * config.fee_rate
                net = gross - fee
                pnl = net - position.entry_cost
                cash += net
                total_fees += fee
                day_key = _utc_day(candle.timestamp_ms)
                realized_pnl_by_day[day_key] = realized_pnl_by_day.get(day_key, 0.0) + pnl
                trades.append(
                    BacktestTrade(
                        entry_signal_ms=position.entry_signal_ms,
                        entry_time_ms=position.entry_time_ms,
                        entry_price=position.entry_price,
                        exit_signal_ms=pending.signal_timestamp_ms,
                        exit_time_ms=candle.timestamp_ms,
                        exit_price=execution_price,
                        quantity=position.quantity,
                        entry_cost=position.entry_cost,
                        net_proceeds=net,
                        pnl=pnl,
                        return_pct=(pnl / position.entry_cost) * 100,
                        exit_reason=pending.reason,
                    )
                )
                position = None
            pending = None

        decision = decide(
            candles[index - config.slow_period : index + 1],
            config.fast_period,
            config.slow_period,
        )

        if position is not None:
            if (
                config.close_stop_loss_pct > 0
                and candle.close
                <= position.entry_price * (1 - config.close_stop_loss_pct)
            ):
                pending = _PendingAction(Signal.SELL, candle.timestamp_ms, "close_stop")
            elif decision.current_fast_sma < decision.current_slow_sma:
                pending = _PendingAction(Signal.SELL, candle.timestamp_ms, "bearish_regime")
        elif decision.signal is Signal.BUY:
            day_loss = realized_pnl_by_day.get(_utc_day(candle.timestamp_ms), 0.0)
            if config.max_daily_loss > 0 and day_loss <= -config.max_daily_loss:
                skipped_buys += 1
            else:
                pending = _PendingAction(Signal.BUY, candle.timestamp_ms, "golden_cross")

        equity = cash + (0.0 if position is None else position.quantity * candle.close)
        equity_curve.append(EquityPoint(candle.timestamp_ms, equity))

    last_close = candles[-1].close
    open_value = 0.0 if position is None else position.quantity * last_close
    ending_equity = cash + open_value
    max_drawdown = _max_drawdown(equity_curve, config.starting_balance)
    winners = sum(1 for trade in trades if trade.pnl > 0)
    win_rate = (winners / len(trades) * 100) if trades else None
    realized_pnl = sum(trade.pnl for trade in trades)
    unrealized_pnl = 0.0 if position is None else open_value - position.entry_cost

    return BacktestResult(
        start_time_ms=candles[active_start_index].timestamp_ms,
        end_time_ms=candles[-1].timestamp_ms,
        candle_count=len(candles) - active_start_index,
        warmup_candle_count=active_start_index,
        starting_balance=config.starting_balance,
        ending_equity=ending_equity,
        total_return_pct=((ending_equity / config.starting_balance) - 1) * 100,
        max_drawdown_pct=max_drawdown,
        closed_trade_count=len(trades),
        winning_trade_count=winners,
        win_rate_pct=win_rate,
        total_fees=total_fees,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        open_position_quantity=0.0 if position is None else position.quantity,
        open_position_value=open_value,
        skipped_buy_count=skipped_buys,
        trades=tuple(trades),
        equity_curve=tuple(equity_curve),
    )


def write_trades_csv(path: Path, trades: Sequence[BacktestTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(trades[0]).keys()) if trades else [
        "entry_signal_ms",
        "entry_time_ms",
        "entry_price",
        "exit_signal_ms",
        "exit_time_ms",
        "exit_price",
        "quantity",
        "entry_cost",
        "net_proceeds",
        "pnl",
        "return_pct",
        "exit_reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(trade) for trade in trades)


def _validate_candles(candles: Sequence[Candle]) -> None:
    previous = -1
    expected_interval: int | None = None
    for candle in candles:
        if candle.timestamp_ms <= previous:
            raise ValueError("캔들 시간은 중복 없이 오름차순이어야 합니다.")
        prices = (candle.open, candle.high, candle.low, candle.close)
        if not all(isfinite(value) for value in prices):
            raise ValueError("OHLC 가격은 모두 유한해야 합니다.")
        if min(prices) <= 0:
            raise ValueError("OHLC 가격은 모두 0보다 커야 합니다.")
        if previous >= 0:
            interval = candle.timestamp_ms - previous
            if expected_interval is None:
                expected_interval = interval
            elif interval != expected_interval:
                raise ValueError("캔들 간격이 일정하지 않습니다.")
        previous = candle.timestamp_ms


def _max_drawdown(
    equity_curve: Sequence[EquityPoint],
    starting_balance: float,
) -> float:
    peak = starting_balance
    max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point.equity)
        if peak > 0:
            drawdown = ((peak - point.equity) / peak) * 100
            max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def _utc_day(timestamp_ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()
