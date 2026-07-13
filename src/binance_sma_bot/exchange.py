from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import ccxt

from .config import Settings, TradingMode
from .strategy import Candle


class TransientExchangeError(RuntimeError):
    """A network/rate-limit failure that is safe to retry after backoff."""


class DefiniteOrderRejected(RuntimeError):
    """A request that was definitely rejected before an order could be accepted."""


@dataclass(frozen=True)
class Execution:
    order_id: str
    status: str
    filled: float
    average: float
    cost: float
    base_fee: float
    quote_fee: float

    @property
    def net_bought_quantity(self) -> float:
        return max(0.0, self.filled - self.base_fee)

    @property
    def net_quote_proceeds(self) -> float:
        return max(0.0, self.cost - self.quote_fee)


class BinanceSpot:
    """Small ccxt adapter. All exchange-specific behavior stays in this file."""

    def __init__(self, settings: Settings):
        config: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": 10_000,
            "options": {
                "defaultType": "spot",
                "adjustForTimeDifference": True,
            },
        }
        if settings.mode is not TradingMode.PAPER:
            config["apiKey"] = settings.api_key
            config["secret"] = settings.api_secret

        self.settings = settings
        self.exchange = ccxt.binance(config)
        # ccxt requires sandbox mode to be selected before any other API call.
        if settings.mode is TradingMode.TESTNET:
            self.exchange.set_sandbox_mode(True)
        self._markets_loaded = False
        self.last_server_time_ms: int | None = None
        self.timeframe_ms = int(self.exchange.parse_timeframe(settings.timeframe) * 1000)

    def load_markets(self, reload: bool = False) -> None:
        if reload or not self._markets_loaded:
            try:
                self.exchange.load_markets(reload=reload)
            except ccxt.NetworkError as exc:
                raise TransientExchangeError("거래소 마켓 정보를 불러오지 못했습니다.") from exc
            market = self.exchange.market(self.settings.symbol)
            if not market.get("spot"):
                raise RuntimeError("이 프로그램은 Binance 현물(Spot) 심볼만 지원합니다.")
            if market.get("active") is False:
                raise RuntimeError("현재 비활성화된 거래 심볼입니다.")
            self._markets_loaded = True

    def fetch_closed_candles(self, limit: int) -> list[Candle]:
        self.load_markets()
        try:
            # Read server time first. If a candle closes during the following OHLCV request,
            # the older time keeps that potentially incomplete snapshot excluded until next poll.
            server_time_ms = self.exchange.fetch_time()
            rows = self.exchange.fetch_ohlcv(
                self.settings.symbol,
                timeframe=self.settings.timeframe,
                limit=limit,
            )
        except ccxt.NetworkError as exc:
            raise TransientExchangeError("캔들/서버 시각 조회에 실패했습니다.") from exc
        self.last_server_time_ms = server_time_ms
        closed = [row for row in rows if int(row[0]) + self.timeframe_ms <= server_time_ms]
        timestamps = [int(row[0]) for row in closed]
        if any(
            current - previous != self.timeframe_ms
            for previous, current in zip(timestamps, timestamps[1:])
        ):
            raise RuntimeError("캔들에 누락·중복·역순 구간이 있어 신규 판단을 중지합니다.")
        return [
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

    def validate_quote_order(self, quote_amount: float, reference_price: float) -> None:
        self.load_markets(reload=True)
        market = self.exchange.market(self.settings.symbol)
        min_cost = ((market.get("limits") or {}).get("cost") or {}).get("min")
        max_cost = ((market.get("limits") or {}).get("cost") or {}).get("max")
        if min_cost is not None and quote_amount < float(min_cost):
            raise RuntimeError(
                f"QUOTE_AMOUNT={quote_amount:g}가 현재 최소 주문금액 {float(min_cost):g}보다 작습니다."
            )
        if max_cost is not None and quote_amount > float(max_cost):
            raise RuntimeError("QUOTE_AMOUNT가 현재 최대 주문금액보다 큽니다.")
        estimated_amount = quote_amount / reference_price
        precise_amount = float(self.exchange.amount_to_precision(self.settings.symbol, estimated_amount))
        min_amount, max_amount = self._market_quantity_bounds(market)
        if precise_amount <= 0 or (min_amount is not None and precise_amount < float(min_amount)):
            raise RuntimeError("계산된 매수 수량이 현재 최소 주문수량보다 작습니다.")
        if max_amount is not None and precise_amount > max_amount:
            raise RuntimeError("계산된 매수 수량이 현재 최대 주문수량보다 큽니다.")

    def fetch_free_balance(self, asset: str) -> float:
        try:
            balance = self.exchange.fetch_balance()
        except ccxt.NetworkError as exc:
            raise TransientExchangeError("가용 잔액 조회에 실패했습니다.") from exc
        value = ((balance.get(asset) or {}).get("free"))
        return 0.0 if value is None else float(value)

    def create_market_buy(self, quote_amount: float, client_order_id: str) -> Execution:
        self.load_markets()
        precise_cost = float(self.exchange.cost_to_precision(self.settings.symbol, quote_amount))
        try:
            order = self.exchange.create_market_buy_order_with_cost(
                self.settings.symbol,
                precise_cost,
                {"newClientOrderId": client_order_id},
            )
        except ccxt.NetworkError as exc:
            raise TransientExchangeError("매수 주문 응답을 확인하지 못했습니다.") from exc
        except (
            ccxt.ArgumentsRequired,
            ccxt.AuthenticationError,
            ccxt.BadSymbol,
            ccxt.InsufficientFunds,
            ccxt.InvalidOrder,
            ccxt.NotSupported,
            ccxt.PermissionDenied,
        ) as exc:
            raise DefiniteOrderRejected(f"매수 주문이 거부되었습니다: {exc}") from exc
        return self._with_trade_fees(self._parse_execution(order))

    def prepare_market_sell_quantity(
        self,
        requested_quantity: float,
        reference_price: float,
    ) -> float:
        self.load_markets(reload=True)
        free_balance = self.fetch_free_balance(self.settings.base_asset)
        # Fail closed when the account no longer contains the bot's tracked quantity.
        # Silently selling min(tracked, free) would lose cost basis and could sell personal funds.
        precise_quantity = float(
            self.exchange.amount_to_precision(self.settings.symbol, requested_quantity)
        )
        precise_free = float(self.exchange.amount_to_precision(self.settings.symbol, free_balance))
        if precise_free < precise_quantity:
            raise RuntimeError(
                "봇 장부 수량보다 실제 가용 잔액이 적습니다. 자동 매도를 중지하고 "
                "입출금·수수료·수동거래 내역을 확인하세요."
            )
        market = self.exchange.market(self.settings.symbol)
        limits = market.get("limits") or {}
        min_amount, max_amount = self._market_quantity_bounds(market)
        min_cost = (limits.get("cost") or {}).get("min")
        max_cost = (limits.get("cost") or {}).get("max")
        if precise_quantity <= 0 or (
            min_amount is not None and precise_quantity < float(min_amount)
        ):
            raise RuntimeError("매도 가능 수량이 현재 최소 주문수량보다 작습니다(DUST).")
        if min_cost is not None and precise_quantity * reference_price < float(min_cost):
            raise RuntimeError("매도 가능 금액이 현재 최소 주문금액보다 작습니다(DUST).")
        if max_amount is not None and precise_quantity > float(max_amount):
            raise RuntimeError("매도 수량이 현재 최대 주문수량보다 큽니다.")
        if max_cost is not None and precise_quantity * reference_price > float(max_cost):
            raise RuntimeError("매도 금액이 현재 최대 주문금액보다 큽니다.")

        return precise_quantity

    def is_dust_quantity(self, quantity: float, reference_price: float) -> bool:
        self.load_markets()
        precise_quantity = float(
            self.exchange.amount_to_precision(self.settings.symbol, quantity)
        )
        market = self.exchange.market(self.settings.symbol)
        limits = market.get("limits") or {}
        min_amount, _ = self._market_quantity_bounds(market)
        min_cost = (limits.get("cost") or {}).get("min")
        if precise_quantity <= 0:
            return True
        if min_amount is not None and precise_quantity < float(min_amount):
            return True
        return min_cost is not None and precise_quantity * reference_price < float(min_cost)

    @staticmethod
    def _market_quantity_bounds(market: dict[str, Any]) -> tuple[float | None, float | None]:
        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        market_limits = limits.get("market") or {}
        minimums = [
            float(value)
            for value in (amount_limits.get("min"), market_limits.get("min"))
            if value is not None
        ]
        maximums = [
            float(value)
            for value in (amount_limits.get("max"), market_limits.get("max"))
            if value is not None
        ]
        minimum = max(minimums) if minimums else None
        maximum = min(maximums) if maximums else None
        return minimum, maximum

    def create_market_sell(self, quantity: float, client_order_id: str) -> Execution:
        try:
            order = self.exchange.create_market_sell_order(
                self.settings.symbol,
                quantity,
                {"newClientOrderId": client_order_id},
            )
        except ccxt.NetworkError as exc:
            raise TransientExchangeError("매도 주문 응답을 확인하지 못했습니다.") from exc
        except (
            ccxt.ArgumentsRequired,
            ccxt.AuthenticationError,
            ccxt.BadSymbol,
            ccxt.InsufficientFunds,
            ccxt.InvalidOrder,
            ccxt.NotSupported,
            ccxt.PermissionDenied,
        ) as exc:
            raise DefiniteOrderRejected(f"매도 주문이 거부되었습니다: {exc}") from exc
        return self._with_trade_fees(self._parse_execution(order))

    def fetch_order_by_client_id(self, client_order_id: str) -> Execution:
        self.load_markets()
        try:
            order = self.exchange.fetch_order(
                client_order_id,
                self.settings.symbol,
                {"origClientOrderId": client_order_id},
            )
        except ccxt.NetworkError as exc:
            raise TransientExchangeError("미확정 주문 조회에 실패했습니다.") from exc
        return self._with_trade_fees(self._parse_execution(order))

    def verify_order_exists(self, client_order_id: str) -> None:
        self.load_markets()
        try:
            self.exchange.fetch_order(
                client_order_id,
                self.settings.symbol,
                {"origClientOrderId": client_order_id},
            )
        except ccxt.NetworkError as exc:
            raise TransientExchangeError("포지션 원주문 확인에 실패했습니다.") from exc

    def _with_trade_fees(self, execution: Execution) -> Execution:
        terminal_statuses = {"closed", "canceled", "rejected", "expired"}
        if (
            execution.status not in terminal_statuses
            or execution.filled <= 0
            or execution.order_id == "unknown"
            or execution.base_fee > 0
            or execution.quote_fee > 0
        ):
            return execution
        try:
            trades = self.exchange.fetch_my_trades(
                self.settings.symbol,
                params={"orderId": execution.order_id},
            )
        except ccxt.NetworkError as exc:
            raise TransientExchangeError("체결 수수료 조회에 실패했습니다.") from exc
        except ccxt.BaseError as exc:
            raise RuntimeError(
                "체결 수수료를 확인하지 못해 장부 반영을 중지합니다. 자동 재주문하지 않습니다."
            ) from exc

        base_fee = 0.0
        quote_fee = 0.0
        for trade in trades:
            trade_order_id = trade.get("order")
            if trade_order_id is not None and str(trade_order_id) != execution.order_id:
                continue
            fee = trade.get("fee") or {}
            currency = fee.get("currency")
            amount = float(fee.get("cost") or 0.0)
            if currency == self.settings.base_asset:
                base_fee += amount
            elif currency == self.settings.quote_asset:
                quote_fee += amount
        return Execution(
            order_id=execution.order_id,
            status=execution.status,
            filled=execution.filled,
            average=execution.average,
            cost=execution.cost,
            base_fee=base_fee,
            quote_fee=quote_fee,
        )

    def _parse_execution(self, order: dict[str, Any]) -> Execution:
        filled = float(order.get("filled") or 0.0)
        cost = float(order.get("cost") or 0.0)
        average_raw = order.get("average")
        average = float(average_raw) if average_raw is not None else (cost / filled if filled else 0.0)
        base_fee = 0.0
        quote_fee = 0.0
        fees = order.get("fees") or ([] if order.get("fee") is None else [order["fee"]])
        for fee in fees:
            currency = fee.get("currency")
            amount = float(fee.get("cost") or 0.0)
            if currency == self.settings.base_asset:
                base_fee += amount
            elif currency == self.settings.quote_asset:
                quote_fee += amount

        return Execution(
            order_id=str(order.get("id") or "unknown"),
            status=str(order.get("status") or "unknown"),
            filled=filled,
            average=average,
            cost=cost,
            base_fee=base_fee,
            quote_fee=quote_fee,
        )
