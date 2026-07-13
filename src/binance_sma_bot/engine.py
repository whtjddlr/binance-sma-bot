from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

from .config import Settings, TradingMode
from .exchange import BinanceSpot, DefiniteOrderRejected, Execution, TransientExchangeError
from .state import BotState, StateStore
from .strategy import Signal, StrategyDecision, decide


logger = logging.getLogger(__name__)
TERMINAL_ORDER_STATUSES = {"closed", "canceled", "rejected", "expired"}


@dataclass(frozen=True)
class RunResult:
    action: str
    message: str
    decision: StrategyDecision | None = None


def _client_order_id(settings: Settings, candle_ms: int, side: str) -> str:
    identity = f"{settings.account_fingerprint}|{settings.strategy_key}"
    short_hash = sha256(identity.encode("utf-8")).hexdigest()[:8]
    return f"sma-{short_hash}-{candle_ms}-{side[0]}"


class TradingEngine:
    def __init__(self, settings: Settings, exchange: BinanceSpot, store: StateStore):
        self.settings = settings
        self.exchange = exchange
        self.store = store

    def run_once(self) -> RunResult:
        state = self.store.load_or_create(
            self.settings.mode.value,
            self.settings.symbol,
            self.settings.strategy_key,
            self.settings.account_fingerprint,
            self.settings.paper_starting_quote,
        )
        if state.pending_client_order_id:
            return self._reconcile_pending(state)

        candles = self.exchange.fetch_closed_candles(self.settings.slow_period + 2)
        if len(candles) < self.settings.slow_period + 1:
            raise RuntimeError(
                f"확정봉이 부족합니다: {len(candles)}개 "
                f"(필요: {self.settings.slow_period + 1}개)"
            )
        decision = decide(candles, self.settings.fast_period, self.settings.slow_period)

        if state.last_processed_candle_ms is None:
            # First start observes the current regime and waits for a new crossover.
            state.last_processed_candle_ms = decision.candle_timestamp_ms
            self.store.save(state)
            return RunResult("warmup", "현재 확정봉을 기준점으로 저장했습니다. 다음 교차부터 거래합니다.", decision)

        if decision.candle_timestamp_ms <= state.last_processed_candle_ms:
            return RunResult("duplicate", "이미 처리한 확정봉입니다.", decision)

        effective_signal = self._effective_signal(state, decision)
        # Staleness blocks new exposure. A SELL only reduces the bot's tracked exposure.
        if self._is_stale(decision) and effective_signal is not Signal.SELL:
            state.last_processed_candle_ms = decision.candle_timestamp_ms
            self.store.save(state)
            return RunResult("stale", "오래된 확정봉 신호여서 주문하지 않았습니다.", decision)

        if effective_signal is Signal.BUY and state.has_position:
            result = RunResult("hold", "이미 봇 포지션을 보유 중이므로 추가 매수하지 않습니다.", decision)
        elif effective_signal is Signal.SELL and not state.has_position:
            result = RunResult("hold", "봇이 보유한 수량이 없어 매도하지 않습니다.", decision)
        elif effective_signal is Signal.BUY and self._daily_loss_limit_reached(state):
            result = RunResult("blocked", "일일 손실 회로가 신규 매수를 차단했습니다.", decision)
        elif effective_signal is Signal.BUY:
            result = self._buy(state, decision)
        elif effective_signal is Signal.SELL:
            result = self._sell(state, decision)
        else:
            result = RunResult("hold", "이동평균 교차가 없습니다.", decision)

        state.last_processed_candle_ms = decision.candle_timestamp_ms
        self.store.save(state)
        return result

    def _is_stale(self, decision: StrategyDecision) -> bool:
        max_age = self.settings.max_signal_age_seconds
        server_time_ms = self.exchange.last_server_time_ms
        if max_age == 0 or server_time_ms is None:
            return False
        candle_close_ms = decision.candle_timestamp_ms + self.exchange.timeframe_ms
        return server_time_ms - candle_close_ms > max_age * 1000

    def _effective_signal(self, state: BotState, decision: StrategyDecision) -> Signal:
        if (
            self.settings.close_stop_loss_pct > 0
            and state.has_position
            and state.entry_price is not None
            and decision.close <= state.entry_price * (1 - self.settings.close_stop_loss_pct)
        ):
            logger.warning("로컬 손절 조건 도달: close=%s entry=%s", decision.close, state.entry_price)
            return Signal.SELL
        # Do not open a late position after downtime, but do reduce an existing position
        # when the latest confirmed SMA regime is already bearish.
        if state.has_position and decision.current_fast_sma < decision.current_slow_sma:
            return Signal.SELL
        return decision.signal

    def _daily_loss_limit_reached(self, state: BotState) -> bool:
        limit = self.settings.max_daily_loss_usdt
        return limit > 0 and state.realized_pnl_today <= -limit

    def _buy(self, state: BotState, decision: StrategyDecision) -> RunResult:
        client_id = _client_order_id(self.settings, decision.candle_timestamp_ms, "buy")
        if self.settings.mode is TradingMode.PAPER:
            total = self.settings.quote_amount * (1 + self.settings.paper_fee_rate)
            if state.paper_quote_balance < total:
                return RunResult("blocked", "페이퍼 USDT 잔액이 부족합니다.", decision)
            quantity = self.settings.quote_amount / decision.close
            state.paper_quote_balance -= total
            state.paper_base_balance += quantity
            state.position_qty = quantity
            state.entry_price = decision.close
            state.entry_cost = total
            state.last_order_id = client_id
            state.last_client_order_id = client_id
            return RunResult(
                "paper_buy",
                f"가상 매수 {quantity:.8f} {self.settings.base_asset} @ {decision.close:.2f}",
                decision,
            )

        self.exchange.validate_quote_order(self.settings.quote_amount, decision.close)
        free_quote = self.exchange.fetch_free_balance(self.settings.quote_asset)
        if free_quote < self.settings.quote_amount:
            return RunResult("blocked", f"{self.settings.quote_asset} 가용 잔액이 부족합니다.", decision)

        self._record_pending(state, client_id, "buy", decision.candle_timestamp_ms)
        try:
            execution = self.exchange.create_market_buy(self.settings.quote_amount, client_id)
        except DefiniteOrderRejected as exc:
            self._finish_rejected_intent(state, client_id, decision.candle_timestamp_ms)
            return RunResult("rejected", str(exc), decision)
        if execution.status in TERMINAL_ORDER_STATUSES and execution.filled == 0:
            self._clear_pending(state, execution.order_id)
            return RunResult("no_fill", "매수 주문이 체결 없이 종료되었습니다.", decision)
        self._require_terminal_fill(execution)
        quantity = execution.net_bought_quantity
        if quantity <= 0:
            raise RuntimeError("매수 체결 수량을 확인할 수 없어 자동 실행을 중지합니다.")
        state.position_qty = quantity
        state.entry_price = execution.average
        state.entry_cost = execution.cost + execution.quote_fee
        self._clear_pending(state, execution.order_id)
        action = (
            f"{self.settings.mode.value}_buy"
            if execution.status == "closed"
            else f"{self.settings.mode.value}_partial_buy"
        )
        return RunResult(
            action,
            f"{self.settings.mode.value} 매수 "
            f"{quantity:.8f} {self.settings.base_asset} @ {execution.average:.2f}",
            decision,
        )

    def _sell(self, state: BotState, decision: StrategyDecision) -> RunResult:
        client_id = _client_order_id(self.settings, decision.candle_timestamp_ms, "sell")
        old_quantity = state.position_qty
        old_cost = state.entry_cost
        if self.settings.mode is TradingMode.PAPER:
            gross = old_quantity * decision.close
            net = gross * (1 - self.settings.paper_fee_rate)
            pnl = net - old_cost
            state.paper_quote_balance += net
            state.paper_base_balance = max(0.0, state.paper_base_balance - old_quantity)
            self._close_position(state, pnl, client_id)
            return RunResult(
                "paper_sell",
                f"가상 매도 {old_quantity:.8f} {self.settings.base_asset}, 실현손익 {pnl:+.2f} USDT",
                decision,
            )

        self._verify_position_origin(state)
        quantity = self.exchange.prepare_market_sell_quantity(old_quantity, decision.close)
        self._record_pending(state, client_id, "sell", decision.candle_timestamp_ms)
        try:
            execution = self.exchange.create_market_sell(quantity, client_id)
        except DefiniteOrderRejected as exc:
            self._finish_rejected_intent(state, client_id, decision.candle_timestamp_ms)
            return RunResult("rejected", str(exc), decision)
        if execution.status in TERMINAL_ORDER_STATUSES and execution.filled == 0:
            self._clear_pending(state, execution.order_id)
            return RunResult("no_fill", "매도 주문이 체결 없이 종료되었습니다.", decision)
        self._require_terminal_fill(execution)
        pnl, _ = self._apply_sell_execution(state, execution)
        dust_moved = self._move_unsellable_remainder_to_dust(state, execution.average)
        remaining = state.position_qty
        self._clear_pending(state, execution.order_id)
        action = (
            f"{self.settings.mode.value}_sell"
            if execution.status == "closed"
            else f"{self.settings.mode.value}_partial_sell"
        )
        return RunResult(
            action,
            f"{self.settings.mode.value} 매도 {execution.filled:.8f} "
            f"{self.settings.base_asset}, 추정 손익 {pnl:+.2f} USDT, "
            f"장부 잔량 {remaining:.8f}, dust 이동 {dust_moved:.8f}",
            decision,
        )

    def _reconcile_pending(self, state: BotState) -> RunResult:
        client_id = state.pending_client_order_id
        side = state.pending_side
        candle_ms = state.pending_candle_ms
        if client_id is None or side not in {"buy", "sell"} or candle_ms is None:
            raise RuntimeError("pending 주문 상태가 손상되어 자동 실행을 중지합니다.")
        try:
            execution = self.exchange.fetch_order_by_client_id(client_id)
        except TransientExchangeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "미확정 주문을 Binance에서 확인하지 못했습니다. 자동 재주문하지 않습니다: "
                f"{client_id} ({type(exc).__name__})"
            ) from exc

        if execution.status in TERMINAL_ORDER_STATUSES and execution.filled == 0:
            self._clear_pending(state, execution.order_id)
            state.last_processed_candle_ms = candle_ms
            self.store.save(state)
            return RunResult("reconciled_rejected", "미확정 주문이 미체결 종료된 것을 확인했습니다.")
        if execution.status not in TERMINAL_ORDER_STATUSES or execution.filled <= 0:
            raise RuntimeError(
                "주문이 아직 미체결/부분체결 상태여서 자동 실행을 중지합니다: "
                f"order={execution.order_id}, status={execution.status}, filled={execution.filled}"
            )

        if side == "buy":
            if state.has_position:
                raise RuntimeError("미확정 매수와 기존 봇 포지션이 동시에 있어 자동 복구할 수 없습니다.")
            state.position_qty = execution.net_bought_quantity
            state.entry_price = execution.average
            state.entry_cost = execution.cost + execution.quote_fee
            action = "reconciled_buy" if execution.status == "closed" else "reconciled_partial_buy"
            message = (
                "Binance 조회로 이전 매수 주문의 체결을 확인하고 상태를 복구했습니다. "
                f"종결상태 {execution.status}"
            )
        else:
            if not state.has_position:
                raise RuntimeError("미확정 매도인데 봇 포지션이 없어 자동 복구할 수 없습니다.")
            pnl, _ = self._apply_sell_execution(state, execution)
            dust_moved = self._move_unsellable_remainder_to_dust(state, execution.average)
            remaining = state.position_qty
            action = (
                "reconciled_sell" if execution.status == "closed" else "reconciled_partial_sell"
            )
            message = (
                "Binance 조회로 이전 매도 주문의 체결을 확인하고 상태를 복구했습니다. "
                f"종결상태 {execution.status}, 추정 손익 {pnl:+.2f} USDT, "
                f"장부 잔량 {remaining:.8f}, dust 이동 {dust_moved:.8f}"
            )

        self._clear_pending(state, execution.order_id)
        state.last_processed_candle_ms = candle_ms
        self.store.save(state)
        return RunResult(action, message)

    def _record_pending(self, state: BotState, client_id: str, side: str, candle_ms: int) -> None:
        state.pending_client_order_id = client_id
        state.pending_side = side
        state.pending_candle_ms = candle_ms
        self.store.save(state)

    def _verify_position_origin(self, state: BotState) -> None:
        client_id = state.last_client_order_id
        if client_id is None:
            raise RuntimeError("봇 포지션의 원주문 ID가 없어 자동 매도를 중지합니다.")
        try:
            self.exchange.verify_order_exists(client_id)
        except TransientExchangeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "봇 포지션의 원주문을 Binance에서 찾지 못했습니다. "
                "Testnet 초기화·API 계정 변경·상태 불일치 가능성이 있어 매도를 중지합니다."
            ) from exc

    def _finish_rejected_intent(self, state: BotState, client_id: str, candle_ms: int) -> None:
        previous_client_order_id = state.last_client_order_id
        self._clear_pending(state, client_id)
        state.last_client_order_id = previous_client_order_id
        state.last_processed_candle_ms = candle_ms
        self.store.save(state)

    @staticmethod
    def _apply_sell_execution(state: BotState, execution: Execution) -> tuple[float, float]:
        old_quantity = state.position_qty
        if old_quantity <= 0 or execution.filled <= 0:
            raise RuntimeError("매도 체결과 장부 포지션을 대조할 수 없습니다.")
        sold_quantity = min(old_quantity, execution.filled)
        sold_ratio = sold_quantity / old_quantity
        proceeds_ratio = min(1.0, sold_quantity / execution.filled)
        allocated_cost = state.entry_cost * sold_ratio
        allocated_proceeds = execution.net_quote_proceeds * proceeds_ratio
        pnl = allocated_proceeds - allocated_cost

        state.realized_pnl_today += pnl
        state.position_qty = max(0.0, old_quantity - sold_quantity)
        state.entry_cost = max(0.0, state.entry_cost - allocated_cost)
        state.last_order_id = execution.order_id
        if state.position_qty == 0:
            state.entry_price = None
            state.entry_cost = 0.0
        return pnl, state.position_qty

    def _move_unsellable_remainder_to_dust(
        self,
        state: BotState,
        reference_price: float,
    ) -> float:
        if state.position_qty <= 0 or reference_price <= 0:
            return 0.0
        if not self.exchange.is_dust_quantity(state.position_qty, reference_price):
            return 0.0
        moved = state.position_qty
        state.dust_qty += state.position_qty
        state.dust_cost += state.entry_cost
        state.position_qty = 0.0
        state.entry_price = None
        state.entry_cost = 0.0
        return moved

    @staticmethod
    def _require_terminal_fill(execution: Execution) -> None:
        if execution.status not in TERMINAL_ORDER_STATUSES or execution.filled <= 0:
            raise RuntimeError(
                "주문이 아직 종결되지 않았습니다. 자동 재주문하지 않습니다. "
                f"order={execution.order_id}, status={execution.status}, filled={execution.filled}"
            )

    @staticmethod
    def _clear_pending(state: BotState, order_id: str) -> None:
        client_order_id = state.pending_client_order_id
        state.pending_client_order_id = None
        state.pending_side = None
        state.pending_candle_ms = None
        state.last_order_id = order_id
        if client_order_id is not None:
            state.last_client_order_id = client_order_id

    @staticmethod
    def _close_position(state: BotState, pnl: float, order_id: str) -> None:
        state.realized_pnl_today += pnl
        state.position_qty = 0.0
        state.entry_price = None
        state.entry_cost = 0.0
        state.last_order_id = order_id
        state.last_client_order_id = order_id


def format_candle_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
