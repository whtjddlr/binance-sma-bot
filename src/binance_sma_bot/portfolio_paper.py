from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from dotenv import load_dotenv

from .strategy import Candle


DAY_MS = 86_400_000
PORTFOLIO_STATE_VERSION = 1
STRATEGY_VERSION = "ema-inverse-volatility-v1"
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

FAST_PERIOD = 50
SLOW_PERIOD = 180
SLOW_SLOPE_DAYS = 20
MOMENTUM_DAYS = 90
VOLATILITY_DAYS = 30
TOP_K = len(MAJOR_USDT_UNIVERSE)
TOTAL_EXPOSURE = 0.25
MAX_ASSET_WEIGHT = 0.10
COOLDOWN_DAYS = 15
PROFIT_LOCK_FRACTION = 0.25
GLOBAL_HARD_DRAWDOWN_PCT = 20.0
REQUIRED_CANDLES = max(
    SLOW_PERIOD + SLOW_SLOPE_DAYS,
    MOMENTUM_DAYS + 1,
    VOLATILITY_DAYS + 1,
)


class PortfolioDataError(RuntimeError):
    pass


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else int(raw)


@dataclass(frozen=True)
class PortfolioPaperSettings:
    state_file: Path = Path("./data/portfolio-paper-state.json")
    starting_quote: float = 1_000.0
    fee_rate: float = 0.001
    slippage_bps: float = 5.0
    poll_seconds: int = 3_600
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env.portfolio") -> "PortfolioPaperSettings":
        if env_file is not None:
            load_dotenv(dotenv_path=env_file, override=False)
        settings = cls(
            state_file=Path(
                os.getenv(
                    "PORTFOLIO_STATE_FILE",
                    "./data/portfolio-paper-state.json",
                )
            ),
            starting_quote=_env_float("PORTFOLIO_STARTING_QUOTE", 1_000.0),
            fee_rate=_env_float("PORTFOLIO_FEE_RATE", 0.001),
            slippage_bps=_env_float("PORTFOLIO_SLIPPAGE_BPS", 5.0),
            poll_seconds=_env_int("PORTFOLIO_POLL_SECONDS", 3_600),
            log_level=os.getenv("PORTFOLIO_LOG_LEVEL", "INFO").strip().upper(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        values = (self.starting_quote, self.fee_rate, self.slippage_bps)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("portfolio paper 숫자 설정은 유한해야 합니다.")
        if self.starting_quote <= 0:
            raise ValueError("PORTFOLIO_STARTING_QUOTE는 0보다 커야 합니다.")
        if not 0 <= self.fee_rate < 0.1:
            raise ValueError("PORTFOLIO_FEE_RATE는 0 이상 0.1 미만이어야 합니다.")
        if not 0 <= self.slippage_bps < 10_000:
            raise ValueError("PORTFOLIO_SLIPPAGE_BPS는 0 이상 10,000 미만이어야 합니다.")
        if self.poll_seconds < 300:
            raise ValueError("PORTFOLIO_POLL_SECONDS는 API 보호를 위해 300 이상이어야 합니다.")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("지원하지 않는 PORTFOLIO_LOG_LEVEL입니다.")

    @property
    def strategy_key(self) -> str:
        return (
            f"{STRATEGY_VERSION}|50/180|slope=20|mom=90|vol=30|"
            f"exposure=0.25|max=0.10|fee={self.fee_rate}|slip={self.slippage_bps}"
        )


@dataclass(frozen=True)
class PortfolioSnapshot:
    candle_timestamp_ms: int
    candles: dict[str, list[Candle]]
    prices: dict[str, float]


class CurlJsonClient:
    """Small read-only HTTP adapter for Binance's public market-data mirror."""

    base_url = "https://data-api.binance.vision"

    def get_json(self, path: str, params: dict[str, str | int] | None = None) -> Any:
        query = "" if not params else f"?{urlencode(params)}"
        url = f"{self.base_url}{path}{query}"
        try:
            completed = subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--location",
                    "--max-time",
                    "30",
                    "--retry",
                    "2",
                    "--retry-all-errors",
                    url,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return json.loads(completed.stdout)
        except FileNotFoundError as exc:
            raise PortfolioDataError("curl 실행 파일이 필요합니다.") from exc
        except subprocess.TimeoutExpired as exc:
            raise PortfolioDataError("Binance 공개 시세 조회 시간이 초과됐습니다.") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "HTTP request failed").strip()
            raise PortfolioDataError(f"Binance 공개 시세 HTTP 오류: {detail}") from exc
        except json.JSONDecodeError as exc:
            raise PortfolioDataError("Binance 공개 시세가 유효한 JSON이 아닙니다.") from exc


class BinancePortfolioMarket:
    def __init__(self, client: CurlJsonClient | None = None) -> None:
        self.client = client or CurlJsonClient()

    def fetch_snapshot(self) -> PortfolioSnapshot:
        try:
            server_payload = self.client.get_json("/api/v3/time")
            if not isinstance(server_payload, dict):
                raise PortfolioDataError("Binance 서버 시각 응답 형식이 잘못됐습니다.")
            server_time_ms = int(server_payload["serverTime"])
            candles: dict[str, list[Candle]] = {}
            latest_timestamps: set[int] = set()
            for symbol in MAJOR_USDT_UNIVERSE:
                rows = self.client.get_json(
                    "/api/v3/klines",
                    {
                        "symbol": symbol.replace("/", ""),
                        "interval": "1d",
                        "limit": 1000,
                    },
                )
                if not isinstance(rows, list):
                    raise PortfolioDataError(f"{symbol} 일봉 응답 형식이 잘못됐습니다.")
                closed = [row for row in rows if int(row[0]) + DAY_MS <= server_time_ms]
                if len(closed) < REQUIRED_CANDLES:
                    raise PortfolioDataError(
                        f"{symbol}의 확정 일봉이 부족합니다: {len(closed)}/{REQUIRED_CANDLES}"
                    )
                converted = [
                    Candle(
                        timestamp_ms=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                    for row in closed
                ]
                _validate_daily_candles(symbol, converted)
                candles[symbol] = converted
                latest_timestamps.add(converted[-1].timestamp_ms)
            if len(latest_timestamps) != 1:
                raise PortfolioDataError("메이저 코인의 최신 확정 일봉 시각이 서로 다릅니다.")

            ticker_rows = self.client.get_json("/api/v3/ticker/price")
            if not isinstance(ticker_rows, list):
                raise PortfolioDataError("Binance 가격 응답 형식이 잘못됐습니다.")
            ticker_by_symbol = {
                str(row.get("symbol")): row.get("price")
                for row in ticker_rows
                if isinstance(row, dict)
            }
            prices: dict[str, float] = {}
            for symbol in MAJOR_USDT_UNIVERSE:
                raw_price = ticker_by_symbol.get(symbol.replace("/", ""))
                price = float(raw_price) if raw_price is not None else 0.0
                if not math.isfinite(price) or price <= 0:
                    raise PortfolioDataError(f"{symbol}의 현재 가격이 유효하지 않습니다.")
                prices[symbol] = price
        except PortfolioDataError:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PortfolioDataError(f"Binance 공개 시세 조회에 실패했습니다: {exc}") from exc

        return PortfolioSnapshot(latest_timestamps.pop(), candles, prices)


def _validate_daily_candles(symbol: str, candles: list[Candle]) -> None:
    previous: int | None = None
    for candle in candles:
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if not all(math.isfinite(value) for value in values) or candle.close <= 0:
            raise PortfolioDataError(f"{symbol} 일봉에 유효하지 않은 값이 있습니다.")
        if previous is not None and candle.timestamp_ms - previous != DAY_MS:
            raise PortfolioDataError(f"{symbol} 일봉에 누락·중복·역순 구간이 있습니다.")
        previous = candle.timestamp_ms


@dataclass(frozen=True)
class PortfolioDecision:
    candle_timestamp_ms: int
    selected: list[str]
    eligible: list[str]
    target_weights: dict[str, float]
    month_end: bool
    rebalance: bool


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _inverse_volatility_weights(
    selected: list[str], candles: dict[str, list[Candle]]
) -> dict[str, float]:
    inverse: dict[str, float] = {}
    for symbol in selected:
        closes = [candle.close for candle in candles[symbol]]
        returns = [
            math.log(closes[index] / closes[index - 1])
            for index in range(len(closes) - VOLATILITY_DAYS, len(closes))
        ]
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / len(returns)
        volatility = math.sqrt(variance)
        if volatility > 0:
            inverse[symbol] = 1 / volatility
    total = sum(inverse.values())
    if total <= 0:
        return {}
    return {
        symbol: min(TOTAL_EXPOSURE * value / total, MAX_ASSET_WEIGHT)
        for symbol, value in inverse.items()
    }


def decide_portfolio(
    candles: dict[str, list[Candle]],
    currently_selected: list[str],
) -> PortfolioDecision:
    if set(candles) != set(MAJOR_USDT_UNIVERSE):
        raise ValueError("메이저 코인 유니버스가 전략 설정과 다릅니다.")
    timestamps = {series[-1].timestamp_ms for series in candles.values() if series}
    if len(timestamps) != 1:
        raise ValueError("최신 확정 일봉 시각이 서로 다릅니다.")
    timestamp = timestamps.pop()
    eligible: list[tuple[float, str]] = []
    stay_eligible: set[str] = set()
    for symbol in MAJOR_USDT_UNIVERSE:
        series = candles[symbol]
        if len(series) < REQUIRED_CANDLES:
            raise ValueError(f"{symbol}의 전략 계산용 일봉이 부족합니다.")
        closes = [candle.close for candle in series]
        ema_fast = _ema(closes, FAST_PERIOD)
        ema_slow = _ema(closes, SLOW_PERIOD)
        previous_index = len(closes) - 1 - SLOW_SLOPE_DAYS
        trend = (
            closes[-1] > ema_slow[-1]
            and ema_fast[-1] > ema_slow[-1]
            and ema_slow[-1] > ema_slow[previous_index]
        )
        if trend:
            stay_eligible.add(symbol)
            momentum = closes[-1] / closes[-1 - MOMENTUM_DAYS] - 1
            eligible.append((momentum, symbol))

    eligible.sort(reverse=True)
    filtered = [symbol for symbol in currently_selected if symbol in stay_eligible]
    rebalance = filtered != currently_selected
    next_day = timestamp + DAY_MS
    month_end = (
        datetime.fromtimestamp(timestamp / 1000, timezone.utc).month
        != datetime.fromtimestamp(next_day / 1000, timezone.utc).month
    )
    selected = filtered
    if month_end:
        selected = [symbol for _, symbol in eligible[:TOP_K]]
        rebalance = True
    return PortfolioDecision(
        candle_timestamp_ms=timestamp,
        selected=selected,
        eligible=[symbol for _, symbol in eligible],
        target_weights=_inverse_volatility_weights(selected, candles),
        month_end=month_end,
        rebalance=rebalance,
    )


@dataclass(frozen=True)
class PaperTrade:
    candle_timestamp_ms: int
    side: str
    symbol: str
    quantity: float
    execution_price: float
    fee_usdt: float
    reason: str


@dataclass
class PortfolioPaperState:
    version: int
    strategy_key: str
    universe: list[str]
    starting_equity: float
    cash: float
    quantities: dict[str, float]
    selected: list[str]
    last_processed_candle_ms: int | None
    last_prices: dict[str, float]
    last_equity: float
    global_peak: float
    cycle_anchor: float
    cycle_peak: float
    max_drawdown_pct: float
    pause_until_ms: int
    reset_cycle_after_exit: bool
    halted: bool
    total_fees: float
    rebalance_count: int
    risk_stops: int
    recent_trades: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def new(cls, settings: PortfolioPaperSettings) -> "PortfolioPaperState":
        quantities = {symbol: 0.0 for symbol in MAJOR_USDT_UNIVERSE}
        return cls(
            version=PORTFOLIO_STATE_VERSION,
            strategy_key=settings.strategy_key,
            universe=list(MAJOR_USDT_UNIVERSE),
            starting_equity=settings.starting_quote,
            cash=settings.starting_quote,
            quantities=quantities,
            selected=[],
            last_processed_candle_ms=None,
            last_prices={},
            last_equity=settings.starting_quote,
            global_peak=settings.starting_quote,
            cycle_anchor=settings.starting_quote,
            cycle_peak=settings.starting_quote,
            max_drawdown_pct=0.0,
            pause_until_ms=0,
            reset_cycle_after_exit=False,
            halted=False,
            total_fees=0.0,
            rebalance_count=0,
            risk_stops=0,
        )


class PortfolioStateStore:
    def __init__(self, path: Path):
        self.path = path

    def load_or_create(self, settings: PortfolioPaperSettings) -> PortfolioPaperState:
        if not self.path.exists():
            state = PortfolioPaperState.new(settings)
            self.save(state)
            return state
        return self.load(settings)

    def load(self, settings: PortfolioPaperSettings) -> PortfolioPaperState:
        if not self.path.exists():
            raise RuntimeError("portfolio paper 상태가 없습니다. 먼저 once를 실행하세요.")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            state = PortfolioPaperState(**payload)
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            raise RuntimeError(f"portfolio paper 상태 파일을 읽을 수 없습니다: {exc}") from exc
        self._validate(state, settings)
        return state

    @staticmethod
    def _validate(state: PortfolioPaperState, settings: PortfolioPaperSettings) -> None:
        if state.version != PORTFOLIO_STATE_VERSION:
            raise RuntimeError(f"지원하지 않는 portfolio 상태 버전: {state.version}")
        if state.strategy_key != settings.strategy_key:
            raise RuntimeError("portfolio 상태 파일의 전략/비용 설정이 현재 설정과 다릅니다.")
        if state.universe != list(MAJOR_USDT_UNIVERSE):
            raise RuntimeError("portfolio 상태 파일의 코인 유니버스가 다릅니다.")
        if not math.isclose(state.starting_equity, settings.starting_quote):
            raise RuntimeError("portfolio 시작자금이 기존 상태 파일과 다릅니다.")
        if set(state.quantities) != set(MAJOR_USDT_UNIVERSE):
            raise RuntimeError("portfolio 상태 파일의 수량 필드가 손상됐습니다.")
        numeric = (
            state.cash,
            state.last_equity,
            state.global_peak,
            state.cycle_anchor,
            state.cycle_peak,
            state.max_drawdown_pct,
            state.total_fees,
            *state.quantities.values(),
            *state.last_prices.values(),
        )
        if not all(math.isfinite(float(value)) for value in numeric):
            raise RuntimeError("portfolio 상태 파일에 유효하지 않은 숫자가 있습니다.")
        if state.cash < -1e-8 or any(quantity < -1e-12 for quantity in state.quantities.values()):
            raise RuntimeError("portfolio 상태 파일에 음수 잔액이 있습니다.")

    def save(self, state: PortfolioPaperState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(asdict(state), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        try:
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


@dataclass(frozen=True)
class PaperRunResult:
    action: str
    message: str
    candle_timestamp_ms: int
    equity: float
    selected: list[str]
    trades: list[PaperTrade]


class PortfolioPaperEngine:
    def __init__(
        self,
        settings: PortfolioPaperSettings,
        market: BinancePortfolioMarket,
        store: PortfolioStateStore,
    ) -> None:
        self.settings = settings
        self.market = market
        self.store = store

    @staticmethod
    def equity(state: PortfolioPaperState, prices: dict[str, float]) -> float:
        return state.cash + sum(
            state.quantities[symbol] * prices[symbol]
            for symbol in MAJOR_USDT_UNIVERSE
        )

    def _rebalance(
        self,
        state: PortfolioPaperState,
        target_weights: dict[str, float],
        prices: dict[str, float],
        candle_timestamp_ms: int,
        reason: str,
    ) -> list[PaperTrade]:
        equity = self.equity(state, prices)
        targets = {
            symbol: equity * target_weights.get(symbol, 0.0)
            for symbol in MAJOR_USDT_UNIVERSE
        }
        slippage = self.settings.slippage_bps / 10_000
        trades: list[PaperTrade] = []
        for symbol in MAJOR_USDT_UNIVERSE:
            quantity = state.quantities[symbol]
            current_value = quantity * prices[symbol]
            if quantity <= 0 or current_value <= targets[symbol] + 0.01:
                continue
            sell_quantity = min(
                quantity,
                (current_value - targets[symbol]) / prices[symbol],
            )
            execution = prices[symbol] * (1 - slippage)
            gross = sell_quantity * execution
            fee = gross * self.settings.fee_rate
            state.cash += gross - fee
            state.quantities[symbol] = max(0.0, quantity - sell_quantity)
            state.total_fees += fee
            trades.append(
                PaperTrade(
                    candle_timestamp_ms,
                    "sell",
                    symbol,
                    sell_quantity,
                    execution,
                    fee,
                    reason,
                )
            )

        for symbol in MAJOR_USDT_UNIVERSE:
            current_value = state.quantities[symbol] * prices[symbol]
            if current_value >= targets[symbol] - 0.01:
                continue
            desired_quote = targets[symbol] - current_value
            total_cost = desired_quote * (1 + self.settings.fee_rate)
            if total_cost > state.cash:
                desired_quote = state.cash / (1 + self.settings.fee_rate)
                total_cost = state.cash
            if desired_quote <= 0.01:
                continue
            execution = prices[symbol] * (1 + slippage)
            bought_quantity = desired_quote / execution
            fee = total_cost - desired_quote
            state.quantities[symbol] += bought_quantity
            state.cash -= total_cost
            state.total_fees += fee
            trades.append(
                PaperTrade(
                    candle_timestamp_ms,
                    "buy",
                    symbol,
                    bought_quantity,
                    execution,
                    fee,
                    reason,
                )
            )

        state.rebalance_count += 1
        state.recent_trades.extend(asdict(trade) for trade in trades)
        state.recent_trades = state.recent_trades[-200:]
        return trades

    def _update_drawdown(self, state: PortfolioPaperState, equity: float) -> None:
        state.global_peak = max(state.global_peak, equity)
        state.cycle_peak = max(state.cycle_peak, equity)
        if state.global_peak > 0:
            drawdown = (state.global_peak - equity) / state.global_peak * 100
            state.max_drawdown_pct = max(state.max_drawdown_pct, drawdown)

    def run_once(self) -> PaperRunResult:
        snapshot = self.market.fetch_snapshot()
        state = self.store.load_or_create(self.settings)
        timestamp = snapshot.candle_timestamp_ms

        if state.last_processed_candle_ms is None:
            state.last_processed_candle_ms = timestamp
            state.last_prices = snapshot.prices
            state.last_equity = self.equity(state, snapshot.prices)
            state.global_peak = max(state.global_peak, state.last_equity)
            state.cycle_peak = max(state.cycle_peak, state.last_equity)
            self.store.save(state)
            return PaperRunResult(
                "initialized",
                "최신 확정 일봉을 기준점으로 저장했습니다. 과거 신호로 진입하지 않습니다.",
                timestamp,
                state.last_equity,
                list(state.selected),
                [],
            )
        if timestamp < state.last_processed_candle_ms:
            raise RuntimeError("Binance 최신 일봉이 상태 파일보다 과거입니다.")
        if timestamp == state.last_processed_candle_ms:
            return PaperRunResult(
                "no_new_candle",
                "새로 마감된 일봉이 없어 가상 체결을 만들지 않았습니다.",
                timestamp,
                self.equity(state, snapshot.prices),
                list(state.selected),
                [],
            )
        if timestamp - state.last_processed_candle_ms != DAY_MS:
            raise RuntimeError(
                "한 개 이상의 일봉 처리를 놓쳤습니다. 상태와 시세를 확인한 뒤 "
                "새 상태 파일로 다시 시작하세요. 과거 체결을 현재 가격으로 재현하지 않습니다."
            )

        state.last_prices = snapshot.prices
        equity_before = self.equity(state, snapshot.prices)
        self._update_drawdown(state, equity_before)
        target_weights: dict[str, float] | None = None
        reason = "daily_signal"
        action = "processed"

        if state.halted:
            state.selected = []
            if any(quantity > 1e-12 for quantity in state.quantities.values()):
                target_weights = {}
                reason = "global_halt"
                action = "risk_halt"
        elif state.max_drawdown_pct >= GLOBAL_HARD_DRAWDOWN_PCT:
            state.halted = True
            state.selected = []
            target_weights = {}
            reason = "global_halt"
            action = "risk_halt"
        elif timestamp < state.pause_until_ms:
            state.selected = []
            if any(quantity > 1e-12 for quantity in state.quantities.values()):
                target_weights = {}
                reason = "cooldown"
                action = "risk_stop"
        else:
            floor = state.cycle_anchor * 0.9
            if state.cycle_peak >= state.cycle_anchor * 1.1:
                floor = max(
                    floor,
                    state.cycle_anchor
                    + PROFIT_LOCK_FRACTION * (state.cycle_peak - state.cycle_anchor),
                )
            if equity_before <= floor:
                state.selected = []
                state.pause_until_ms = timestamp + COOLDOWN_DAYS * DAY_MS
                state.reset_cycle_after_exit = True
                state.risk_stops += 1
                target_weights = {}
                reason = "cycle_floor"
                action = "risk_stop"
            else:
                decision = decide_portfolio(snapshot.candles, state.selected)
                state.selected = decision.selected
                if decision.rebalance:
                    target_weights = decision.target_weights
                    reason = "month_end" if decision.month_end else "trend_exit"
                    action = "rebalanced"

        trades: list[PaperTrade] = []
        if target_weights is not None:
            trades = self._rebalance(
                state,
                target_weights,
                snapshot.prices,
                timestamp,
                reason,
            )
        if state.reset_cycle_after_exit and all(
            quantity <= 1e-12 for quantity in state.quantities.values()
        ):
            state.cycle_anchor = state.cash
            state.cycle_peak = state.cash
            state.reset_cycle_after_exit = False

        state.last_processed_candle_ms = timestamp
        state.last_equity = self.equity(state, snapshot.prices)
        self._update_drawdown(state, state.last_equity)
        self.store.save(state)
        message = (
            f"equity={state.last_equity:,.2f} USDT, selected="
            f"{','.join(state.selected) if state.selected else 'cash'}, trades={len(trades)}"
        )
        return PaperRunResult(
            action,
            message,
            timestamp,
            state.last_equity,
            list(state.selected),
            trades,
        )


def format_utc(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return "N/A"
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()


def state_summary(state: PortfolioPaperState) -> dict[str, Any]:
    positions = {
        symbol: {
            "quantity": state.quantities[symbol],
            "last_price": state.last_prices.get(symbol),
            "value_usdt": state.quantities[symbol] * state.last_prices.get(symbol, 0.0),
        }
        for symbol in MAJOR_USDT_UNIVERSE
        if state.quantities[symbol] > 1e-12
    }
    return {
        "mode": "paper-only",
        "strategy": state.strategy_key,
        "last_candle_utc": format_utc(state.last_processed_candle_ms),
        "starting_equity": state.starting_equity,
        "equity": state.last_equity,
        "return_pct": (state.last_equity / state.starting_equity - 1) * 100,
        "cash": state.cash,
        "selected": state.selected,
        "positions": positions,
        "max_drawdown_pct": state.max_drawdown_pct,
        "pause_until_utc": format_utc(state.pause_until_ms) if state.pause_until_ms else "N/A",
        "halted": state.halted,
        "total_fees": state.total_fees,
        "rebalance_count": state.rebalance_count,
        "risk_stops": state.risk_stops,
        "recent_trade_count": len(state.recent_trades),
    }
