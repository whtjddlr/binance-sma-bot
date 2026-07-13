from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from math import ceil

import ccxt

from .strategy import Candle


class HistoricalDataError(RuntimeError):
    pass


_TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 3 * 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "6h": 6 * 60 * 60,
    "8h": 8 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
    "3d": 3 * 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
}


class BinanceHistory:
    def __init__(self) -> None:
        self.exchange = ccxt.binance(
            {
                "enableRateLimit": True,
                "timeout": 10_000,
                "options": {"defaultType": "spot"},
            }
        )

    def fetch_closed_candles(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        until_ms: int,
        max_candles: int = 50_000,
    ) -> list[Candle]:
        if since_ms >= until_ms:
            raise ValueError("시작 시각은 종료 시각보다 빨라야 합니다.")
        if max_candles < 2:
            raise ValueError("MAX_CANDLES는 2 이상이어야 합니다.")

        try:
            self.exchange.load_markets()
            market = self.exchange.market(symbol)
            if not market.get("spot") or market.get("active") is False:
                raise HistoricalDataError("활성 Binance Spot 심볼만 지원합니다.")
            timeframe_ms = int(self.exchange.parse_timeframe(timeframe) * 1000)
            server_time_ms = int(self.exchange.fetch_time())
        except ccxt.BaseError as exc:
            raise HistoricalDataError(f"Binance 과거 데이터 준비에 실패했습니다: {exc}") from exc

        effective_until = min(until_ms, server_time_ms)
        if effective_until <= since_ms:
            raise HistoricalDataError("요청 구간에 Binance 서버 기준 확정봉이 없습니다.")
        estimated = max(0, ceil((effective_until - since_ms) / timeframe_ms) - 1)
        if estimated > max_candles:
            raise HistoricalDataError(
                f"요청 구간은 약 {estimated:,}개 봉입니다. "
                f"--max-candles 값을 늘리거나 기간을 줄이세요."
            )

        candles: list[Candle] = []
        cursor = since_ms
        last_timestamp: int | None = None
        while cursor < effective_until:
            if len(candles) >= max_candles:
                raise HistoricalDataError("MAX_CANDLES 한도에 도달했습니다.")
            try:
                rows = self.exchange.fetch_ohlcv(
                    symbol,
                    timeframe=timeframe,
                    since=cursor,
                    limit=1000,
                )
            except ccxt.BaseError as exc:
                raise HistoricalDataError(f"Binance Kline 조회에 실패했습니다: {exc}") from exc
            if not rows:
                raise HistoricalDataError("요청 종료 전 Binance Kline 빈 페이지가 반환됐습니다.")

            newest_raw_timestamp = int(rows[-1][0])
            for row in rows:
                timestamp = int(row[0])
                if timestamp < since_ms or timestamp >= effective_until:
                    continue
                if timestamp + timeframe_ms > server_time_ms:
                    continue
                if last_timestamp is not None:
                    if timestamp == last_timestamp:
                        continue
                    if timestamp - last_timestamp != timeframe_ms:
                        raise HistoricalDataError("과거 캔들에 누락·중복·역순 구간이 있습니다.")
                candles.append(
                    Candle(
                        timestamp_ms=timestamp,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                )
                last_timestamp = timestamp
                if len(candles) > max_candles:
                    raise HistoricalDataError("MAX_CANDLES 한도에 도달했습니다.")

            next_cursor = newest_raw_timestamp + timeframe_ms
            if next_cursor <= cursor:
                raise HistoricalDataError("과거 데이터 페이지가 앞으로 진행되지 않습니다.")
            cursor = next_cursor

        if not candles:
            raise HistoricalDataError("요청 구간에서 확정된 Binance Kline을 찾지 못했습니다.")
        if candles[0].timestamp_ms - since_ms >= timeframe_ms:
            raise HistoricalDataError("과거 데이터 시작 경계에 한 봉 이상 누락이 있습니다.")
        if effective_until - (candles[-1].timestamp_ms + timeframe_ms) >= timeframe_ms:
            raise HistoricalDataError("과거 데이터 종료 경계에 한 봉 이상 누락이 있습니다.")
        return candles


def parse_utc_bound(value: str, *, inclusive_date_end: bool = False) -> int:
    text = value.strip()
    try:
        if "T" not in text and " " not in text:
            parsed_date = date.fromisoformat(text)
            parsed = datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
            if inclusive_date_end:
                parsed += timedelta(days=1)
        else:
            normalized = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"ISO 날짜/시각 형식이 아닙니다: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("시간대 없는 날짜-시간은 허용하지 않습니다. Z 또는 오프셋을 쓰세요.")
    parsed = parsed.astimezone(timezone.utc)
    return int(parsed.timestamp() * 1000)


def default_backtest_bounds(now: datetime | None = None) -> tuple[int, int]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    return (
        int((current - timedelta(days=365)).timestamp() * 1000),
        int(current.timestamp() * 1000),
    )


def timeframe_to_milliseconds(timeframe: str) -> int:
    try:
        return _TIMEFRAME_SECONDS[timeframe] * 1000
    except KeyError as exc:
        raise ValueError(f"지원하지 않는 고정 길이 TIMEFRAME입니다: {timeframe}") from exc
