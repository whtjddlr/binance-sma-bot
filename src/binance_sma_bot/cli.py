from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .backtest import BacktestConfig, run_backtest, write_trades_csv
from .config import Settings
from .engine import TradingEngine, format_candle_time
from .exchange import BinanceSpot, TransientExchangeError
from .history import (
    BinanceHistory,
    default_backtest_bounds,
    parse_utc_bound,
    timeframe_to_milliseconds,
)
from .process_lock import AlreadyRunningError, ProcessLock
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Binance Spot SMA crossover bot")
    parser.add_argument("--env-file", default=".env", help="환경설정 파일 경로 (기본: .env)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="설정만 검증하고 API 호출 없이 종료")
    subparsers.add_parser("once", help="최신 확정봉을 한 번 평가")
    subparsers.add_parser("run", help="POLL_SECONDS 간격으로 계속 실행")
    subparsers.add_parser("status", help="로컬 봇 상태 표시")
    backtest = subparsers.add_parser("backtest", help="Binance 과거 데이터로 전략 시뮬레이션")
    backtest.add_argument("--since", help="시작(UTC ISO). 생략하면 최근 365일")
    backtest.add_argument("--until", help="종료(UTC ISO). 날짜만 쓰면 그 날짜까지 포함")
    backtest.add_argument("--starting-balance", type=float, help="초기 USDT")
    backtest.add_argument("--quote-amount", type=float, help="매수 1회당 USDT")
    backtest.add_argument("--fee-rate", type=float, help="편도 수수료율(예: 0.001)")
    backtest.add_argument("--slippage-bps", type=float, default=5.0, help="편도 슬리피지 bps")
    backtest.add_argument(
        "--close-stop-loss-pct",
        type=float,
        help="확정봉 종가 손절률(예: 0.03, 0은 비활성)",
    )
    backtest.add_argument("--max-daily-loss", type=float, help="UTC 일일 실현손실 매수 차단")
    backtest.add_argument("--max-candles", type=int, default=50_000, help="거래 구간 최대 봉 수")
    backtest.add_argument("--trades-csv", help="완료 거래 CSV 저장 경로")
    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def _print_result(result: object) -> None:
    action = getattr(result, "action")
    message = getattr(result, "message")
    decision = getattr(result, "decision")
    print(f"[{action}] {message}")
    if decision is not None:
        print(
            "  candle_utc="
            f"{format_candle_time(decision.candle_timestamp_ms)} "
            f"close={decision.close:.4f} "
            f"SMA_fast={decision.current_fast_sma:.4f} "
            f"SMA_slow={decision.current_slow_sma:.4f} "
            f"signal={decision.signal.value}"
        )


def _status(settings: Settings, store: StateStore) -> int:
    state = store.load_or_create(
        settings.mode.value,
        settings.symbol,
        settings.strategy_key,
        settings.account_fingerprint,
        settings.paper_starting_quote,
    )
    safe = asdict(state)
    for key in sorted(safe):
        print(f"{key}: {safe[key]}")
    return 0


def _run_backtest(settings: Settings, args: argparse.Namespace) -> int:
    default_since, default_until = default_backtest_bounds()
    requested_since = (
        parse_utc_bound(args.since) if args.since is not None else default_since
    )
    requested_until = (
        parse_utc_bound(args.until, inclusive_date_end=True)
        if args.until is not None
        else default_until
    )
    if requested_since >= requested_until:
        raise ValueError("--since는 --until보다 빨라야 합니다.")

    timeframe_ms = timeframe_to_milliseconds(settings.timeframe)
    warmup_count = settings.slow_period + 1
    fetch_since = max(0, requested_since - warmup_count * timeframe_ms)
    history = BinanceHistory()
    candles = history.fetch_closed_candles(
        settings.symbol,
        settings.timeframe,
        fetch_since,
        requested_until,
        max_candles=args.max_candles + warmup_count,
    )
    result = run_backtest(
        candles,
        BacktestConfig(
            fast_period=settings.fast_period,
            slow_period=settings.slow_period,
            starting_balance=(
                settings.paper_starting_quote
                if args.starting_balance is None
                else args.starting_balance
            ),
            quote_amount=(settings.quote_amount if args.quote_amount is None else args.quote_amount),
            fee_rate=(settings.paper_fee_rate if args.fee_rate is None else args.fee_rate),
            slippage_bps=args.slippage_bps,
            close_stop_loss_pct=(
                settings.close_stop_loss_pct
                if args.close_stop_loss_pct is None
                else args.close_stop_loss_pct
            ),
            max_daily_loss=(
                settings.max_daily_loss_usdt
                if args.max_daily_loss is None
                else args.max_daily_loss
            ),
            trade_start_ms=requested_since,
        ),
    )

    win_rate = "N/A" if result.win_rate_pct is None else f"{result.win_rate_pct:.2f}%"
    print(f"기간(UTC): {format_candle_time(result.start_time_ms)} ~ {format_candle_time(result.end_time_ms)}")
    print(f"봉 수: {result.candle_count:,} (워밍업 {result.warmup_candle_count:,})")
    print(f"초기자산: {result.starting_balance:,.2f} USDT")
    print(f"최종평가액: {result.ending_equity:,.2f} USDT")
    print(f"총수익률: {result.total_return_pct:+.2f}%")
    print(f"최대낙폭(MDD): {result.max_drawdown_pct:.2f}%")
    print(
        f"완료 거래: {result.closed_trade_count} | 승리: {result.winning_trade_count} "
        f"| 승률: {win_rate}"
    )
    print(
        f"실현손익: {result.realized_pnl:+,.2f} | 미실현손익: "
        f"{result.unrealized_pnl:+,.2f} | 수수료: {result.total_fees:,.2f} USDT"
    )
    print(
        f"미청산 수량: {result.open_position_quantity:.8f} "
        f"(평가 {result.open_position_value:,.2f} USDT) | 차단/잔고부족 매수: "
        f"{result.skipped_buy_count}"
    )
    if args.trades_csv:
        output = Path(args.trades_csv).expanduser().resolve()
        write_trades_csv(output, result.trades)
        print(f"거래내역 CSV: {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(
            args.env_file,
            require_credentials=args.command != "backtest",
        )
        _configure_logging(settings.log_level)
        if args.command == "check":
            print(
                "설정 정상: "
                f"mode={settings.mode.value}, symbol={settings.symbol}, "
                f"timeframe={settings.timeframe}, SMA={settings.fast_period}/{settings.slow_period}"
            )
            return 0

        if args.command == "backtest":
            return _run_backtest(settings, args)

        store = StateStore(settings.state_file)
        if args.command == "status":
            return _status(settings, store)

        exchange = BinanceSpot(settings)
        engine = TradingEngine(settings, exchange, store)
        lock_path = settings.state_file.with_suffix(settings.state_file.suffix + ".lock")
        with ProcessLock(lock_path):
            if args.command == "once":
                _print_result(engine.run_once())
                return 0

            backoff_seconds = settings.poll_seconds
            while True:
                try:
                    _print_result(engine.run_once())
                    backoff_seconds = settings.poll_seconds
                    time.sleep(settings.poll_seconds)
                except TransientExchangeError as exc:
                    logging.getLogger(__name__).warning(
                        "%s %s초 후 재시도합니다.", exc, backoff_seconds
                    )
                    time.sleep(backoff_seconds)
                    backoff_seconds = min(300, backoff_seconds * 2)
    except KeyboardInterrupt:
        print("중지했습니다.")
        return 130
    except (ValueError, RuntimeError, AlreadyRunningError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        logging.getLogger(__name__).exception("예상하지 못한 오류")
        print(
            "거래소 요청 결과가 불명확할 수 있어 자동 재시도하지 않습니다. "
            f"상세: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
