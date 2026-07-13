import pytest

from binance_sma_bot.process_lock import AlreadyRunningError, ProcessLock


def test_second_process_lock_is_rejected(tmp_path) -> None:
    path = tmp_path / "bot.lock"
    with ProcessLock(path):
        with pytest.raises(AlreadyRunningError):
            with ProcessLock(path):
                pass
