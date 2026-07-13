from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .portfolio_paper import (
    MAJOR_USDT_UNIVERSE,
    BinancePortfolioMarket,
    PaperRunResult,
    PortfolioDataError,
    PortfolioPaperEngine,
    PortfolioPaperSettings,
    PortfolioStateStore,
    format_utc,
    state_summary,
)
from .process_lock import AlreadyRunningError, ProcessLock


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Optimized major-coin EMA portfolio paper trader"
    )
    parser.add_argument(
        "--env-file",
        default=".env.portfolio",
        help="portfolio 환경설정 파일 (기본: .env.portfolio)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="paper 전략과 설정만 확인")
    subparsers.add_parser("once", help="최신 확정 일봉을 한 번 처리")
    subparsers.add_parser("run", help="새 확정 일봉을 계속 감시")
    subparsers.add_parser("status", help="로컬 portfolio paper 상태 표시")
    return parser


def _print_result(result: PaperRunResult) -> None:
    print(f"[{result.action}] {result.message}")
    print(f"  candle_utc={format_utc(result.candle_timestamp_ms)}")
    for trade in result.trades:
        print(
            f"  {trade.side.upper()} {trade.symbol} qty={trade.quantity:.10f} "
            f"price={trade.execution_price:.8f} fee={trade.fee_usdt:.6f} "
            f"reason={trade.reason}"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = PortfolioPaperSettings.from_env(args.env_file)
        logging.basicConfig(
            level=getattr(logging, settings.log_level),
            format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        )
        if args.command == "check":
            print("설정 정상: mode=paper-only")
            print("strategy=EMA 50/180, slope 20d, inverse volatility 30d")
            print("exposure=25%, max_asset=10%, global_drawdown_halt=20%")
            print(f"universe={','.join(MAJOR_USDT_UNIVERSE)}")
            return 0

        store = PortfolioStateStore(settings.state_file)
        if args.command == "status":
            print(json.dumps(state_summary(store.load(settings)), ensure_ascii=False, indent=2))
            return 0

        market = BinancePortfolioMarket()
        engine = PortfolioPaperEngine(settings, market, store)
        lock_path = settings.state_file.with_suffix(settings.state_file.suffix + ".lock")
        with ProcessLock(lock_path):
            if args.command == "once":
                _print_result(engine.run_once())
                return 0

            backoff = settings.poll_seconds
            while True:
                try:
                    _print_result(engine.run_once())
                    backoff = settings.poll_seconds
                    time.sleep(settings.poll_seconds)
                except PortfolioDataError as exc:
                    logging.getLogger(__name__).warning(
                        "%s %s초 후 재시도합니다.", exc, backoff
                    )
                    time.sleep(backoff)
                    backoff = min(3_600, backoff * 2)
    except KeyboardInterrupt:
        print("중지했습니다.")
        return 130
    except (ValueError, RuntimeError, AlreadyRunningError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        logging.getLogger(__name__).exception("예상하지 못한 portfolio paper 오류")
        print(f"오류: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
