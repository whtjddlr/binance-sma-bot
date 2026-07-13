from pathlib import Path

import binance_sma_bot.cli as cli
from binance_sma_bot.strategy import Candle


class FakeHistory:
    def fetch_closed_candles(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        until_ms: int,
        max_candles: int,
    ) -> list[Candle]:
        closes = [3, 2, 1, 1, 4, 4, 1, 1, 1, 1]
        return [
            Candle(index * 60_000, close, close, close, close, 1.0)
            for index, close in enumerate(closes)
        ]


def test_backtest_does_not_require_testnet_keys_or_touch_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    state_path = tmp_path / "must-not-exist.json"
    monkeypatch.setenv("TRADING_MODE", "testnet")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.setenv("TIMEFRAME", "1m")
    monkeypatch.setenv("FAST_PERIOD", "2")
    monkeypatch.setenv("SLOW_PERIOD", "3")
    monkeypatch.setenv("STATE_FILE", str(state_path))
    monkeypatch.setattr(cli, "BinanceHistory", FakeHistory)

    result = cli.main(
        [
            "--env-file",
            str(tmp_path / "missing.env"),
            "backtest",
            "--since",
            "1970-01-01",
            "--until",
            "1970-01-01",
            "--max-candles",
            "100",
        ]
    )

    assert result == 0
    assert not state_path.exists()
    assert "최종평가액" in capsys.readouterr().out

