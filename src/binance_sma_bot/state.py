from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_VERSION = 4


def utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class BotState:
    version: int
    mode: str
    symbol: str
    strategy_key: str
    account_fingerprint: str
    last_processed_candle_ms: int | None
    position_qty: float
    entry_price: float | None
    entry_cost: float
    dust_qty: float
    dust_cost: float
    paper_quote_balance: float
    paper_base_balance: float
    realized_pnl_today: float
    pnl_date_utc: str
    pending_client_order_id: str | None
    pending_side: str | None
    pending_candle_ms: int | None
    last_order_id: str | None
    last_client_order_id: str | None

    @classmethod
    def new(
        cls,
        mode: str,
        symbol: str,
        strategy_key: str,
        account_fingerprint: str,
        paper_starting_quote: float,
    ) -> "BotState":
        return cls(
            version=STATE_VERSION,
            mode=mode,
            symbol=symbol,
            strategy_key=strategy_key,
            account_fingerprint=account_fingerprint,
            last_processed_candle_ms=None,
            position_qty=0.0,
            entry_price=None,
            entry_cost=0.0,
            dust_qty=0.0,
            dust_cost=0.0,
            paper_quote_balance=paper_starting_quote,
            paper_base_balance=0.0,
            realized_pnl_today=0.0,
            pnl_date_utc=utc_date(),
            pending_client_order_id=None,
            pending_side=None,
            pending_candle_ms=None,
            last_order_id=None,
            last_client_order_id=None,
        )

    def roll_pnl_day(self) -> None:
        today = utc_date()
        if self.pnl_date_utc != today:
            self.pnl_date_utc = today
            self.realized_pnl_today = 0.0

    @property
    def has_position(self) -> bool:
        return self.position_qty > 0


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load_or_create(
        self,
        mode: str,
        symbol: str,
        strategy_key: str,
        account_fingerprint: str,
        paper_starting_quote: float,
    ) -> BotState:
        if not self.path.exists():
            state = BotState.new(
                mode,
                symbol,
                strategy_key,
                account_fingerprint,
                paper_starting_quote,
            )
            self.save(state)
            return state

        payload: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        version = payload.get("version")
        if version != STATE_VERSION:
            raise RuntimeError(f"지원하지 않는 상태 파일 버전: {version}")
        state = BotState(**payload)
        if (
            state.mode != mode
            or state.symbol != symbol
            or state.strategy_key != strategy_key
            or state.account_fingerprint != account_fingerprint
        ):
            raise RuntimeError(
                "기존 상태 파일의 mode/symbol/전략/API 계정이 현재 설정과 다릅니다. "
                "다른 STATE_FILE을 사용하거나 기존 포지션을 먼저 확인하세요."
            )
        state.roll_pnl_day()
        return state

    def save(self, state: BotState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(asdict(state), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)
        # Persist the directory entry too where the platform permits directory fsync.
        try:
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
