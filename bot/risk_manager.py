"""Risk management: daily loss halt, drawdown halt, consecutive-loss pause."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto

from bot.config import TradingConfig
from bot.telegram_notifier import TelegramNotifier

log = logging.getLogger("polybot.risk")


class HaltReason(Enum):
    NONE = auto()
    DAILY_LOSS = auto()
    DRAWDOWN = auto()
    CONSECUTIVE_LOSSES = auto()


@dataclass
class RiskState:
    """Mutable state tracked by the risk manager."""

    # Set once at start of day (or bot start)
    day_start_balance: float = 0.0
    all_time_high: float = 0.0

    # Running counters
    current_balance: float = 0.0
    consecutive_losses: int = 0
    trades_today: int = 0
    wins_today: int = 0

    # Halt state
    halted: bool = False
    halt_reason: HaltReason = HaltReason.NONE
    halt_until: float | None = None  # monotonic time for temp pauses
    permanent_halt: bool = False


class RiskManager:
    """Enforces all risk limits.  Halts are sticky and require manual restart."""

    def __init__(
        self,
        cfg: TradingConfig,
        notifier: TelegramNotifier,
        initial_balance: float,
    ) -> None:
        self._cfg = cfg
        self._notifier = notifier
        self._state = RiskState(
            day_start_balance=initial_balance,
            all_time_high=initial_balance,
            current_balance=initial_balance,
        )
        self._lock = asyncio.Lock()

    @property
    def state(self) -> RiskState:
        return self._state

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def can_trade(self) -> tuple[bool, str]:
        """Return (allowed, reason).  Checks all kill-switch conditions."""
        s = self._state

        if s.permanent_halt:
            return False, f"PERMANENT HALT: {s.halt_reason.name}"

        if s.halted:
            if s.halt_until is not None and time.monotonic() >= s.halt_until:
                async with self._lock:
                    s.halted = False
                    s.halt_reason = HaltReason.NONE
                    s.halt_until = None
                    s.consecutive_losses = 0
                log.info("Temporary pause expired – trading resumed")
                await self._notifier.alert(
                    "RESUMED", "Consecutive-loss pause expired. Trading resumed."
                )
            else:
                remaining = ""
                if s.halt_until:
                    remaining = f" ({(s.halt_until - time.monotonic()) / 60:.0f}m left)"
                return False, f"HALTED: {s.halt_reason.name}{remaining}"

        return True, "ok"

    # ------------------------------------------------------------------
    # Update after each trade
    # ------------------------------------------------------------------

    async def record_trade(self, pnl: float, new_balance: float) -> None:
        """Call after every trade settles.  May trigger halts."""
        async with self._lock:
            s = self._state
            s.current_balance = new_balance
            s.trades_today += 1

            if pnl >= 0:
                s.wins_today += 1
                s.consecutive_losses = 0
            else:
                s.consecutive_losses += 1

            # Update all-time high
            if new_balance > s.all_time_high:
                s.all_time_high = new_balance

        await self._check_limits()

    async def update_balance(self, balance: float) -> None:
        """Refresh balance without recording a trade (e.g. periodic sync)."""
        async with self._lock:
            self._state.current_balance = balance
            if balance > self._state.all_time_high:
                self._state.all_time_high = balance
        await self._check_limits()

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    async def reset_daily(self, current_balance: float) -> None:
        async with self._lock:
            self._state.day_start_balance = current_balance
            self._state.current_balance = current_balance
            self._state.trades_today = 0
            self._state.wins_today = 0
            self._state.consecutive_losses = 0
            if not self._state.permanent_halt:
                self._state.halted = False
                self._state.halt_reason = HaltReason.NONE
                self._state.halt_until = None
        log.info("Daily risk counters reset. Start balance: $%.2f", current_balance)

    # ------------------------------------------------------------------
    # Internal checks
    # ------------------------------------------------------------------

    async def _check_limits(self) -> None:
        s = self._state

        # 1. Daily loss halt: -20 % of day-start balance
        if s.day_start_balance > 0:
            daily_pnl_pct = (s.current_balance - s.day_start_balance) / s.day_start_balance
            if daily_pnl_pct <= self._cfg.daily_loss_halt_pct:
                await self._trigger_halt(
                    HaltReason.DAILY_LOSS,
                    permanent=True,
                    detail=(
                        f"Daily P&L {daily_pnl_pct * 100:+.1f}% breached "
                        f"{self._cfg.daily_loss_halt_pct * 100:.0f}% limit. "
                        f"Balance: ${s.current_balance:,.2f}"
                    ),
                )
                return

        # 2. Drawdown halt: balance < 60 % of all-time high
        if s.all_time_high > 0:
            drawdown_floor = s.all_time_high * self._cfg.drawdown_permanent_halt_pct
            if s.current_balance < drawdown_floor:
                await self._trigger_halt(
                    HaltReason.DRAWDOWN,
                    permanent=True,
                    detail=(
                        f"Balance ${s.current_balance:,.2f} < 60% of ATH "
                        f"${s.all_time_high:,.2f} (floor ${drawdown_floor:,.2f}). "
                        "PERMANENT HALT."
                    ),
                )
                return

        # 3. Consecutive loss pause: 5 losses → 30 min cooldown
        if s.consecutive_losses >= self._cfg.consecutive_loss_pause_count:
            pause_sec = self._cfg.consecutive_loss_pause_minutes * 60
            await self._trigger_halt(
                HaltReason.CONSECUTIVE_LOSSES,
                permanent=False,
                pause_seconds=pause_sec,
                detail=(
                    f"{s.consecutive_losses} consecutive losses. "
                    f"Pausing {self._cfg.consecutive_loss_pause_minutes} min."
                ),
            )

    async def _trigger_halt(
        self,
        reason: HaltReason,
        *,
        permanent: bool,
        pause_seconds: float = 0,
        detail: str = "",
    ) -> None:
        async with self._lock:
            self._state.halted = True
            self._state.halt_reason = reason
            if permanent:
                self._state.permanent_halt = True
                self._state.halt_until = None
            else:
                self._state.halt_until = time.monotonic() + pause_seconds

        tag = "KILL SWITCH" if permanent else "PAUSE"
        log.critical("%s – %s: %s", tag, reason.name, detail)
        await self._notifier.alert(
            f"🚨 {tag}: {reason.name}",
            f"{detail}\n\n<i>Manual restart required.</i>" if permanent else detail,
        )

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------

    def win_rate(self) -> float:
        if self._state.trades_today == 0:
            return 0.0
        return self._state.wins_today / self._state.trades_today

    def daily_pnl(self) -> float:
        return self._state.current_balance - self._state.day_start_balance

    def summary(self) -> str:
        s = self._state
        return (
            f"Balance: ${s.current_balance:,.2f} | "
            f"Day P&L: ${self.daily_pnl():+,.2f} | "
            f"Trades: {s.trades_today} | "
            f"Win rate: {self.win_rate() * 100:.0f}% | "
            f"Consec losses: {s.consecutive_losses} | "
            f"ATH: ${s.all_time_high:,.2f} | "
            f"Halted: {s.halted}"
        )
