from datetime import datetime, timezone

import pytest

from binance_sma_bot.history import (
    BinanceHistory,
    HistoricalDataError,
    default_backtest_bounds,
    parse_utc_bound,
    timeframe_to_milliseconds,
)


MINUTE_MS = 60_000


def rows(count: int) -> list[list[float]]:
    return [
        [index * MINUTE_MS, 1.0, 1.0, 1.0, 1.0, 1.0]
        for index in range(count)
    ]


class FakeHistoryExchange:
    def __init__(self, dataset: list[list[float]], server_time_ms: int):
        self.dataset = dataset
        self.server_time_ms = server_time_ms
        self.calls: list[tuple[int, int]] = []

    def load_markets(self) -> None:
        pass

    def market(self, symbol: str) -> dict[str, object]:
        return {"spot": True, "active": True}

    def parse_timeframe(self, timeframe: str) -> int:
        return 60

    def fetch_time(self) -> int:
        return self.server_time_ms

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        limit: int,
    ) -> list[list[float]]:
        self.calls.append((since, limit))
        return [row for row in self.dataset if int(row[0]) >= since][:limit]


def client_with(fake: object) -> BinanceHistory:
    client = object.__new__(BinanceHistory)
    client.exchange = fake
    return client


def test_manual_pagination_uses_1000_and_next_open_time() -> None:
    dataset = rows(1_505)
    fake = FakeHistoryExchange(dataset, 1_505 * MINUTE_MS)
    candles = client_with(fake).fetch_closed_candles(
        "BTC/USDT", "1m", 0, 1_505 * MINUTE_MS, max_candles=2_000
    )
    assert len(candles) == 1_505
    assert fake.calls == [(0, 1000), (1_000 * MINUTE_MS, 1000)]


class ShortPageExchange(FakeHistoryExchange):
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        limit: int,
    ) -> list[list[float]]:
        self.calls.append((since, limit))
        eligible = [row for row in self.dataset if int(row[0]) >= since]
        return eligible[:2]


def test_short_page_does_not_end_pagination_early() -> None:
    fake = ShortPageExchange(rows(5), 5 * MINUTE_MS)
    candles = client_with(fake).fetch_closed_candles(
        "BTC/USDT", "1m", 0, 5 * MINUTE_MS, max_candles=10
    )
    assert len(candles) == 5
    assert len(fake.calls) == 3


class EarlyEmptyExchange(FakeHistoryExchange):
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        limit: int,
    ) -> list[list[float]]:
        self.calls.append((since, limit))
        if len(self.calls) == 1:
            return self.dataset[:2]
        return []


def test_empty_page_before_end_is_rejected() -> None:
    fake = EarlyEmptyExchange(rows(5), 5 * MINUTE_MS)
    with pytest.raises(HistoricalDataError, match="빈 페이지"):
        client_with(fake).fetch_closed_candles(
            "BTC/USDT", "1m", 0, 5 * MINUTE_MS, max_candles=10
        )


def test_leading_boundary_gap_is_rejected() -> None:
    fake = FakeHistoryExchange(rows(5)[2:], 5 * MINUTE_MS)
    with pytest.raises(HistoricalDataError, match="시작 경계"):
        client_with(fake).fetch_closed_candles(
            "BTC/USDT", "1m", 0, 5 * MINUTE_MS, max_candles=10
        )


def test_gap_is_rejected() -> None:
    dataset = rows(4)
    del dataset[2]
    fake = FakeHistoryExchange(dataset, 4 * MINUTE_MS)
    with pytest.raises(HistoricalDataError, match="누락"):
        client_with(fake).fetch_closed_candles(
            "BTC/USDT", "1m", 0, 4 * MINUTE_MS, max_candles=10
        )


def test_current_incomplete_candle_is_excluded() -> None:
    fake = FakeHistoryExchange(rows(5), 4 * MINUTE_MS + 30_000)
    candles = client_with(fake).fetch_closed_candles(
        "BTC/USDT", "1m", 0, 5 * MINUTE_MS, max_candles=4
    )
    assert [candle.timestamp_ms for candle in candles] == [
        0,
        MINUTE_MS,
        2 * MINUTE_MS,
        3 * MINUTE_MS,
    ]


def test_iso_bounds_are_utc_and_naive_datetime_is_rejected() -> None:
    assert parse_utc_bound("1970-01-01") == 0
    assert parse_utc_bound("1970-01-01", inclusive_date_end=True) == 86_400_000
    assert parse_utc_bound("1970-01-01T09:00:00+09:00") == 0
    assert parse_utc_bound("1970-01-01T00:00:00Z") == 0
    with pytest.raises(ValueError, match="시간대 없는"):
        parse_utc_bound("2024-01-01T09:00:00")


def test_default_bounds_cover_365_days() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    since, until = default_backtest_bounds(now)
    assert until - since == 365 * 86_400_000
    assert timeframe_to_milliseconds("1h") == 3_600_000
