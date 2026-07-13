from __future__ import annotations

import argparse
import glob
import hashlib
import json
import random
import statistics
import time
from dataclasses import asdict
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

from binance_sma_bot.strategy import Candle
from research_portfolio_backtest import (
    MAJOR_USDT_UNIVERSE,
    PortfolioConfig,
    PortfolioResult,
    run_portfolio,
)


DEVELOPMENT_FOLDS = (
    ("2019-01-01", "2021-01-01"),
    ("2020-01-01", "2022-01-01"),
    ("2021-01-01", "2023-01-01"),
    ("2022-01-01", "2024-01-01"),
)
VALIDATION_FOLD = ("2023-01-01", "2025-01-01")
HOLDOUT_FOLD = ("2024-01-01", "2026-01-01")


def parse_utc(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def load_assets(data_dir: Path) -> dict[str, list[Candle]]:
    assets: dict[str, list[Candle]] = {}
    for ccxt_symbol in MAJOR_USDT_UNIVERSE:
        api_symbol = ccxt_symbol.replace("/", "")
        rows: list[list[object]] = []
        for filename in sorted(glob.glob(str(data_dir / f"{api_symbol}-1d-*.json"))):
            with Path(filename).open(encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, list):
                rows.extend(payload)
        by_timestamp = {int(row[0]): row for row in rows}
        if not by_timestamp:
            raise FileNotFoundError(f"missing daily data for {api_symbol}")
        assets[ccxt_symbol] = [
            Candle(
                timestamp_ms=timestamp,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for timestamp, row in sorted(by_timestamp.items())
        ]
    return assets


def candidate_space(seed: int, samples: int) -> list[PortfolioConfig]:
    candidates = [
        PortfolioConfig(
            total_exposure=exposure,
            momentum_days=momentum,
            daily_fast_period=fast,
            daily_slow_period=slow,
            slow_slope_days=slope,
            cooldown_days=cooldown,
            profit_lock_fraction=lock,
        )
        for exposure, momentum, fast, slow, slope, cooldown, lock in product(
            (0.15, 0.20, 0.25, 0.30),
            (60, 90, 120),
            (40, 50, 60),
            (180, 200, 240),
            (10, 20, 40),
            (15, 30, 60),
            (0.25, 0.50, 0.75),
        )
    ]
    candidates.extend(
        PortfolioConfig(
            total_exposure=exposure,
            momentum_days=90,
            daily_fast_period=fast,
            daily_slow_period=slow,
            slow_slope_days=slope,
            cooldown_days=cooldown,
            profit_lock_fraction=lock,
            trend_model="ema",
            allocation_model=allocation,
        )
        for exposure, fast, slow, slope, cooldown, lock, allocation in product(
            (0.20, 0.25, 0.30),
            (30, 40, 50),
            (180, 240),
            (10, 20),
            (15, 30),
            (0.25, 0.50),
            ("equal", "inverse_volatility"),
        )
    )
    candidates.extend(
        PortfolioConfig(
            total_exposure=exposure,
            momentum_days=90,
            daily_fast_period=50,
            daily_slow_period=slow,
            slow_slope_days=slope,
            cooldown_days=cooldown,
            profit_lock_fraction=lock,
            trend_model="breakout",
            breakout_days=breakout,
            breakout_exit_days=exit_days,
            allocation_model=allocation,
        )
        for exposure, slow, slope, breakout, exit_days, cooldown, lock, allocation in product(
            (0.20, 0.25, 0.30),
            (180, 240),
            (10, 20),
            (60, 120, 180),
            (20, 40),
            (15, 30),
            (0.25, 0.50),
            ("equal", "inverse_volatility"),
        )
        if exit_days < breakout
    )
    baseline = PortfolioConfig()
    candidates = [candidate for candidate in candidates if candidate != baseline]
    random.Random(seed).shuffle(candidates)
    return [baseline, *candidates][:samples]


def result_dict(result: PortfolioResult) -> dict[str, object]:
    return asdict(result)


def evaluate_fold(
    assets: dict[str, list[Candle]],
    config: PortfolioConfig,
    fold: tuple[str, str],
) -> PortfolioResult:
    return run_portfolio(assets, parse_utc(fold[0]), parse_utc(fold[1]), config)


def robust_score(results: list[PortfolioResult]) -> float:
    returns = [result.return_pct for result in results]
    worst_drawdown = max(result.max_drawdown_pct for result in results)
    negative_folds = sum(result.return_pct < 0 for result in results)
    halted_folds = sum(result.halted for result in results)
    return (
        statistics.median(returns)
        - 0.75 * worst_drawdown
        - 10.0 * negative_folds
        - 25.0 * halted_folds
    )


def run_search(data_dir: Path, samples: int, seed: int, output: Path) -> dict[str, object]:
    assets = load_assets(data_dir)
    ranked: list[tuple[float, PortfolioConfig, list[PortfolioResult]]] = []
    for config in candidate_space(seed, samples):
        development = [evaluate_fold(assets, config, fold) for fold in DEVELOPMENT_FOLDS]
        ranked.append((robust_score(development), config, development))
    ranked.sort(key=lambda item: item[0], reverse=True)

    finalists: list[dict[str, object]] = []
    for score, config, development in ranked[:15]:
        validation = evaluate_fold(assets, config, VALIDATION_FOLD)
        holdout = evaluate_fold(assets, config, HOLDOUT_FOLD)
        stressed = evaluate_fold(
            assets,
            PortfolioConfig(**(asdict(config) | {"fee_rate": 0.002, "slippage_bps": 10.0})),
            HOLDOUT_FOLD,
        )
        passed = all(
            result.return_pct > 0
            and result.max_drawdown_pct < config.global_hard_drawdown_pct
            and not result.halted
            for result in (validation, holdout, stressed)
        )
        finalists.append(
            {
                "development_score": score,
                "config": asdict(config),
                "development": [result_dict(result) for result in development],
                "validation": result_dict(validation),
                "holdout": result_dict(holdout),
                "double_cost_holdout": result_dict(stressed),
                "promotion_status": "paper_candidate" if passed else "rejected",
            }
        )

    promoted = next(
        (candidate for candidate in finalists if candidate["promotion_status"] == "paper_candidate"),
        None,
    )
    report: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "sample_count": samples,
        "selection_policy": "rank development only; validation and holdout are pass/fail gates",
        "development_folds": DEVELOPMENT_FOLDS,
        "validation_fold": VALIDATION_FOLD,
        "holdout_fold": HOLDOUT_FOLD,
        "promoted": promoted,
        "finalists": finalists,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def data_fingerprint(data_dir: Path) -> str:
    digest = hashlib.sha256()
    for filename in sorted(data_dir.glob("*USDT-1d-*.json")):
        digest.update(filename.name.encode())
        digest.update(str(filename.stat().st_size).encode())
        digest.update(str(filename.stat().st_mtime_ns).encode())
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Walk-forward major-coin optimizer")
    parser.add_argument("--data-dir", type=Path, default=Path("data/research"))
    parser.add_argument("--samples", type=int, default=3780)
    parser.add_argument("--seed", type=int, default=20260713)
    parser.add_argument("--output", type=Path, default=Path("data/optimizer-results.json"))
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval-hours", type=float, default=24.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.samples < 1:
        raise SystemExit("--samples must be positive")
    previous_fingerprint: str | None = None
    while True:
        fingerprint = data_fingerprint(args.data_dir)
        if fingerprint != previous_fingerprint:
            report = run_search(args.data_dir, args.samples, args.seed, args.output)
            promoted = report["promoted"]
            if promoted is None:
                print("검증 관문을 모두 통과한 후보가 없습니다.")
            else:
                print(json.dumps(promoted, indent=2, ensure_ascii=False))
            previous_fingerprint = fingerprint
        elif args.watch:
            print("새 일봉 데이터가 없어 최적화를 건너뜁니다.")
        if not args.watch:
            return 0
        time.sleep(max(60.0, args.interval_hours * 3600))


if __name__ == "__main__":
    raise SystemExit(main())
