from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from research_portfolio_backtest import MAJOR_USDT_UNIVERSE


def refresh(data_dir: Path) -> bool:
    data_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    current_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    since_ms = int((current_day - timedelta(days=999)).timestamp() * 1000)
    until_ms = int(current_day.timestamp() * 1000) - 1
    changed = False
    for ccxt_symbol in MAJOR_USDT_UNIVERSE:
        api_symbol = ccxt_symbol.replace("/", "")
        url = (
            "https://data-api.binance.vision/api/v3/klines"
            f"?symbol={api_symbol}&interval=1d&startTime={since_ms}"
            f"&endTime={until_ms}&limit=1000"
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "45",
                    url,
                    "-o",
                    str(temporary_path),
                ],
                check=True,
            )
            payload = json.loads(temporary_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list) or not payload:
                raise RuntimeError(f"invalid Binance payload for {api_symbol}")
            destination = data_dir / f"{api_symbol}-1d-latest.json"
            new_bytes = temporary_path.read_bytes()
            old_bytes = destination.read_bytes() if destination.exists() else None
            if new_bytes != old_bytes:
                destination.write_bytes(new_bytes)
                changed = True
        finally:
            temporary_path.unlink(missing_ok=True)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh major-coin daily candles")
    parser.add_argument("--data-dir", type=Path, default=Path("data/research"))
    args = parser.parse_args()
    print("updated" if refresh(args.data_dir) else "unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
