from __future__ import annotations

import os
from hashlib import sha256
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from pathlib import Path

from dotenv import load_dotenv


SUPPORTED_TIMEFRAMES = {
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
}


class TradingMode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else float(raw)


@dataclass(frozen=True)
class Settings:
    mode: TradingMode = TradingMode.PAPER
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    fast_period: int = 20
    slow_period: int = 50
    quote_amount: float = 25.0
    poll_seconds: int = 30
    max_signal_age_seconds: int = 600
    paper_starting_quote: float = 1000.0
    paper_fee_rate: float = 0.001
    max_daily_loss_usdt: float = 10.0
    close_stop_loss_pct: float = 0.0
    state_file: Path = Path("./data/state.json")
    log_level: str = "INFO"
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = ".env",
        *,
        require_credentials: bool = True,
    ) -> "Settings":
        if env_file is not None:
            load_dotenv(dotenv_path=env_file, override=False)
        raw_mode = os.getenv("TRADING_MODE", "paper").strip().lower()
        try:
            mode = TradingMode(raw_mode)
        except ValueError as exc:
            raise ValueError("TRADING_MODE는 paper 또는 testnet이어야 합니다.") from exc

        settings = cls(
            mode=mode,
            symbol=os.getenv("SYMBOL", "BTC/USDT").strip().upper(),
            timeframe=os.getenv("TIMEFRAME", "1h").strip(),
            fast_period=_env_int("FAST_PERIOD", 20),
            slow_period=_env_int("SLOW_PERIOD", 50),
            quote_amount=_env_float("QUOTE_AMOUNT", 25.0),
            poll_seconds=_env_int("POLL_SECONDS", 30),
            max_signal_age_seconds=_env_int("MAX_SIGNAL_AGE_SECONDS", 600),
            paper_starting_quote=_env_float("PAPER_STARTING_QUOTE", 1000.0),
            paper_fee_rate=_env_float("PAPER_FEE_RATE", 0.001),
            max_daily_loss_usdt=_env_float("MAX_DAILY_LOSS_USDT", 10.0),
            close_stop_loss_pct=_env_float("CLOSE_STOP_LOSS_PCT", 0.0),
            state_file=Path(os.getenv("STATE_FILE", "./data/state.json")),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            api_key=os.getenv("BINANCE_API_KEY", "").strip(),
            api_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
        )
        settings.validate(require_credentials=require_credentials)
        return settings

    def validate(self, *, require_credentials: bool = True) -> None:
        numeric_values = {
            "QUOTE_AMOUNT": self.quote_amount,
            "PAPER_STARTING_QUOTE": self.paper_starting_quote,
            "PAPER_FEE_RATE": self.paper_fee_rate,
            "MAX_DAILY_LOSS_USDT": self.max_daily_loss_usdt,
            "CLOSE_STOP_LOSS_PCT": self.close_stop_loss_pct,
        }
        for name, value in numeric_values.items():
            if not isfinite(value):
                raise ValueError(f"{name}는 유한한 숫자여야 합니다.")
        if "/" not in self.symbol or len(self.symbol.split("/")) != 2:
            raise ValueError("SYMBOL은 BTC/USDT 형식이어야 합니다.")
        if self.quote_asset != "USDT":
            raise ValueError("이 버전은 안전한 손익 계산을 위해 USDT 마켓만 지원합니다.")
        if self.timeframe not in SUPPORTED_TIMEFRAMES:
            allowed = ", ".join(sorted(SUPPORTED_TIMEFRAMES))
            raise ValueError(f"지원하지 않는 TIMEFRAME입니다. 허용값: {allowed}")
        if self.fast_period < 2:
            raise ValueError("FAST_PERIOD는 2 이상이어야 합니다.")
        if self.slow_period <= self.fast_period:
            raise ValueError("SLOW_PERIOD는 FAST_PERIOD보다 커야 합니다.")
        if self.slow_period > 998:
            raise ValueError("SLOW_PERIOD는 Binance Kline 1,000개 제한 때문에 998 이하여야 합니다.")
        if self.quote_amount <= 0:
            raise ValueError("QUOTE_AMOUNT는 0보다 커야 합니다.")
        if self.poll_seconds < 5:
            raise ValueError("POLL_SECONDS는 API 과호출 방지를 위해 5 이상이어야 합니다.")
        if self.max_signal_age_seconds < 0:
            raise ValueError("MAX_SIGNAL_AGE_SECONDS는 0 이상이어야 합니다.")
        if self.paper_starting_quote <= 0:
            raise ValueError("PAPER_STARTING_QUOTE는 0보다 커야 합니다.")
        if not 0 <= self.paper_fee_rate < 0.1:
            raise ValueError("PAPER_FEE_RATE는 0 이상 0.1 미만이어야 합니다.")
        if self.max_daily_loss_usdt < 0:
            raise ValueError("MAX_DAILY_LOSS_USDT는 0 이상이어야 합니다.")
        if not 0 <= self.close_stop_loss_pct < 1:
            raise ValueError("CLOSE_STOP_LOSS_PCT는 0 이상 1 미만이어야 합니다.")
        if (
            require_credentials
            and self.mode is not TradingMode.PAPER
            and (not self.api_key or not self.api_secret)
        ):
            raise ValueError(f"{self.mode.value} 모드에는 BINANCE_API_KEY/SECRET이 필요합니다.")

    @property
    def base_asset(self) -> str:
        return self.symbol.split("/", maxsplit=1)[0]

    @property
    def quote_asset(self) -> str:
        return self.symbol.split("/", maxsplit=1)[1]

    @property
    def strategy_key(self) -> str:
        return (
            f"sma-v1|{self.symbol}|{self.timeframe}|"
            f"{self.fast_period}|{self.slow_period}"
        )

    @property
    def account_fingerprint(self) -> str:
        if self.mode is TradingMode.PAPER:
            return "paper-local"
        return sha256(self.api_key.encode("utf-8")).hexdigest()[:16]
