import pytest

from binance_sma_bot.config import Settings, TradingMode


def test_safe_defaults() -> None:
    settings = Settings()
    settings.validate()
    assert settings.mode is TradingMode.PAPER
    assert settings.strategy_key == "sma-v1|BTC/USDT|1h|20|50"


def test_slow_period_must_exceed_fast() -> None:
    with pytest.raises(ValueError, match="SLOW_PERIOD"):
        Settings(fast_period=20, slow_period=20).validate()


def test_slow_period_respects_binance_kline_limit() -> None:
    with pytest.raises(ValueError, match="1,000개"):
        Settings(fast_period=20, slow_period=999).validate()


def test_only_usdt_quote_is_supported() -> None:
    with pytest.raises(ValueError, match="USDT 마켓"):
        Settings(symbol="ETH/BTC").validate()


def test_unknown_timeframe_is_rejected() -> None:
    with pytest.raises(ValueError, match="TIMEFRAME"):
        Settings(timeframe="banana").validate()


def test_non_finite_money_is_rejected() -> None:
    with pytest.raises(ValueError, match="유한한"):
        Settings(quote_amount=float("nan")).validate()


def test_testnet_requires_keys() -> None:
    with pytest.raises(ValueError, match="API_KEY"):
        Settings(mode=TradingMode.TESTNET).validate()


def test_live_mode_is_not_available() -> None:
    with pytest.raises(ValueError):
        TradingMode("live")


def test_testnet_account_fingerprint_changes_with_key() -> None:
    first = Settings(mode=TradingMode.TESTNET, api_key="key-a", api_secret="secret")
    second = Settings(mode=TradingMode.TESTNET, api_key="key-b", api_secret="secret")
    assert first.account_fingerprint != second.account_fingerprint


def test_secret_not_in_repr() -> None:
    settings = Settings(api_key="visible-no", api_secret="super-secret")
    assert "super-secret" not in repr(settings)
    assert "visible-no" not in repr(settings)
