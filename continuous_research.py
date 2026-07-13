from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

from refresh_major_daily import refresh
from research_optimizer import run_search


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily major-coin research loop")
    parser.add_argument("--data-dir", type=Path, default=Path("data/research"))
    parser.add_argument("--output", type=Path, default=Path("data/optimizer-results-live.json"))
    parser.add_argument("--samples", type=int, default=3780)
    parser.add_argument("--seed", type=int, default=20260713)
    parser.add_argument("--interval-hours", type=float, default=24.0)
    args = parser.parse_args()
    while True:
        try:
            if refresh(args.data_dir) or not args.output.exists():
                report = run_search(
                    args.data_dir,
                    args.samples,
                    args.seed,
                    args.output,
                )
                promoted = report["promoted"]
                status = "no candidate" if promoted is None else "paper candidate updated"
                print(f"{datetime.now(timezone.utc).isoformat()} {status}", flush=True)
            else:
                print(
                    f"{datetime.now(timezone.utc).isoformat()} no new candle; skipped",
                    flush=True,
                )
        except Exception as exc:  # keep the research service alive after transient failures
            print(f"{datetime.now(timezone.utc).isoformat()} error: {exc}", flush=True)
        time.sleep(max(300.0, args.interval_hours * 3600))


if __name__ == "__main__":
    raise SystemExit(main())
