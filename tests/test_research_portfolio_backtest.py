from datetime import datetime, timedelta, timezone

from binance_sma_bot.strategy import Candle
from research_optimizer import candidate_space
from research_portfolio_backtest import PortfolioConfig, run_portfolio


def make_daily(*, rising: bool) -> list[Candle]:
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    candles: list[Candle] = []
    for index in range(320):
        close = 100.0 + index if rising else 500.0 - index
        candles.append(
            Candle(
                timestamp_ms=int((start + timedelta(days=index)).timestamp() * 1000),
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=1_000.0,
            )
        )
    return candles


def make_breakout_daily() -> list[Candle]:
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    candles: list[Candle] = []
    for index in range(320):
        close = 100.0 * (1.01**index)
        candles.append(
            Candle(
                timestamp_ms=int((start + timedelta(days=index)).timestamp() * 1000),
                open=close,
                high=close * 1.005,
                low=close * 0.995,
                close=close,
                volume=1_000.0,
            )
        )
    return candles


def test_portfolio_enters_only_eligible_uptrend() -> None:
    rising = make_daily(rising=True)
    falling = make_daily(rising=False)
    result = run_portfolio(
        {"UP": rising, "DOWN": falling},
        rising[220].timestamp_ms,
        rising[-1].timestamp_ms + 86_400_000,
        PortfolioConfig(top_k=2, total_exposure=0.25),
    )
    assert result.ending_equity > 1_000.0
    assert result.rebalance_count > 0
    assert not result.halted


def test_portfolio_stays_in_cash_without_eligible_asset() -> None:
    falling = make_daily(rising=False)
    result = run_portfolio(
        {"DOWN": falling},
        falling[220].timestamp_ms,
        falling[-1].timestamp_ms + 86_400_000,
        PortfolioConfig(),
    )
    assert result.ending_equity == 1_000.0
    assert result.fees == 0.0


def test_ema_inverse_volatility_model_trades_uptrend() -> None:
    rising = make_daily(rising=True)
    result = run_portfolio(
        {"UP": rising},
        rising[220].timestamp_ms,
        rising[-1].timestamp_ms + 86_400_000,
        PortfolioConfig(
            top_k=1,
            total_exposure=0.25,
            trend_model="ema",
            allocation_model="inverse_volatility",
        ),
    )
    assert result.ending_equity > 1_000.0
    assert result.rebalance_count > 0


def test_breakout_model_trades_new_highs() -> None:
    rising = make_breakout_daily()
    result = run_portfolio(
        {"UP": rising},
        rising[220].timestamp_ms,
        rising[-1].timestamp_ms + 86_400_000,
        PortfolioConfig(
            top_k=1,
            total_exposure=0.25,
            trend_model="breakout",
            breakout_days=60,
            breakout_exit_days=20,
        ),
    )
    assert result.ending_equity > 1_000.0
    assert result.rebalance_count > 0


def test_candidate_space_is_deterministic_and_includes_baseline() -> None:
    first = candidate_space(7, 10)
    second = candidate_space(7, 10)
    assert first == second
    assert first[0] == PortfolioConfig()
