import ccxt
import pytest

import binance_sma_bot.exchange as exchange_module
from binance_sma_bot.config import Settings, TradingMode
from binance_sma_bot.exchange import BinanceSpot, Execution


def test_execution_subtracts_base_and_quote_fees() -> None:
    adapter = object.__new__(BinanceSpot)
    adapter.settings = Settings()
    execution = adapter._parse_execution(  # noqa: SLF001
        {
            "id": "1",
            "status": "closed",
            "filled": 0.01,
            "average": 50_000,
            "cost": 500,
            "fees": [
                {"currency": "BTC", "cost": 0.00001},
                {"currency": "USDT", "cost": 0.5},
            ],
        }
    )
    assert execution.net_bought_quantity == 0.00999
    assert execution.net_quote_proceeds == 499.5


class FakeTradesExchange:
    def fetch_my_trades(self, symbol: str, params: dict[str, str]) -> list[dict[str, object]]:
        assert params == {"orderId": "123"}
        return [
            {
                "order": "123",
                "fee": {"currency": "BTC", "cost": 0.00001},
            }
        ]


def test_missing_query_order_fee_is_enriched_from_trades() -> None:
    adapter = object.__new__(BinanceSpot)
    adapter.settings = Settings()
    adapter.exchange = FakeTradesExchange()
    execution = adapter._with_trade_fees(  # noqa: SLF001
        Execution("123", "closed", 0.01, 50_000.0, 500.0, 0.0, 0.0)
    )
    assert execution.base_fee == 0.00001
    assert execution.net_bought_quantity == 0.00999


def test_terminal_partial_fill_fee_is_also_enriched() -> None:
    adapter = object.__new__(BinanceSpot)
    adapter.settings = Settings()
    adapter.exchange = FakeTradesExchange()
    execution = adapter._with_trade_fees(  # noqa: SLF001
        Execution("123", "canceled", 0.01, 50_000.0, 500.0, 0.0, 0.0)
    )
    assert execution.net_bought_quantity == 0.00999


def test_market_lot_size_is_combined_with_regular_lot_size() -> None:
    minimum, maximum = BinanceSpot._market_quantity_bounds(  # noqa: SLF001
        {
            "limits": {
                "amount": {"min": 0.001, "max": 100.0},
                "market": {"min": 0.01, "max": 50.0},
            }
        }
    )
    assert minimum == 0.01
    assert maximum == 50.0


class FakeGapExchange:
    def fetch_time(self) -> int:
        return 20_000_000

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        return [
            [0, 1, 1, 1, 1, 1],
            [3_600_000, 1, 1, 1, 1, 1],
            [10_800_000, 1, 1, 1, 1, 1],
        ]


def test_candle_gap_stops_strategy_evaluation() -> None:
    adapter = object.__new__(BinanceSpot)
    adapter.settings = Settings()
    adapter.exchange = FakeGapExchange()
    adapter._markets_loaded = True  # noqa: SLF001
    adapter.timeframe_ms = 3_600_000
    adapter.last_server_time_ms = None
    with pytest.raises(RuntimeError, match="누락"):
        adapter.fetch_closed_candles(10)


class FailingFeeExchange:
    def fetch_my_trades(self, symbol: str, params: dict[str, str]) -> list[dict[str, object]]:
        raise ccxt.AuthenticationError("denied")


def test_fee_lookup_failure_is_fail_closed() -> None:
    adapter = object.__new__(BinanceSpot)
    adapter.settings = Settings()
    adapter.exchange = FailingFeeExchange()
    with pytest.raises(RuntimeError, match="수수료"):
        adapter._with_trade_fees(  # noqa: SLF001
            Execution("123", "closed", 0.01, 50_000.0, 500.0, 0.0, 0.0)
        )


class FakeCostOrderExchange:
    def __init__(self) -> None:
        self.args: tuple[object, ...] | None = None

    def cost_to_precision(self, symbol: str, cost: float) -> str:
        return "25.00"

    def create_market_buy_order_with_cost(
        self,
        symbol: str,
        cost: float,
        params: dict[str, str],
    ) -> dict[str, object]:
        self.args = (symbol, cost, params)
        return {
            "id": "1",
            "status": "closed",
            "filled": 0.001,
            "average": 25_000,
            "cost": 25.0,
            "fees": [{"currency": "BTC", "cost": 0.000001}],
        }


def test_cost_buy_and_client_order_id_contract() -> None:
    adapter = object.__new__(BinanceSpot)
    adapter.settings = Settings()
    adapter._markets_loaded = True  # noqa: SLF001
    fake = FakeCostOrderExchange()
    adapter.exchange = fake
    adapter.create_market_buy(25.0, "sma-client")
    assert fake.args == ("BTC/USDT", 25.0, {"newClientOrderId": "sma-client"})


class FakeOrderQueryExchange:
    def __init__(self) -> None:
        self.args: tuple[object, ...] | None = None

    def fetch_order(
        self,
        order_id: str,
        symbol: str,
        params: dict[str, str],
    ) -> dict[str, object]:
        self.args = (order_id, symbol, params)
        return {
            "id": "7",
            "status": "closed",
            "filled": 0.01,
            "average": 50_000,
            "cost": 500.0,
            "fees": [{"currency": "BTC", "cost": 0.00001}],
        }


def test_orig_client_order_id_query_contract() -> None:
    adapter = object.__new__(BinanceSpot)
    adapter.settings = Settings()
    adapter._markets_loaded = True  # noqa: SLF001
    fake = FakeOrderQueryExchange()
    adapter.exchange = fake
    adapter.fetch_order_by_client_id("sma-client")
    assert fake.args == (
        "sma-client",
        "BTC/USDT",
        {"origClientOrderId": "sma-client"},
    )


def test_sandbox_mode_is_selected_before_timeframe_parsing(monkeypatch) -> None:
    events: list[str] = []

    class FakeBinance:
        def __init__(self, config: dict[str, object]):
            events.append("constructed")

        def set_sandbox_mode(self, enabled: bool) -> None:
            assert enabled is True
            events.append("sandbox")

        def parse_timeframe(self, timeframe: str) -> int:
            events.append("parse")
            return 3_600

    monkeypatch.setattr(exchange_module.ccxt, "binance", FakeBinance)
    BinanceSpot(
        Settings(
            mode=TradingMode.TESTNET,
            api_key="test-key",
            api_secret="test-secret",
        )
    )
    assert events == ["constructed", "sandbox", "parse"]
