"""Trade execution engine – routes to live or paper backend."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

from bot.config import TradingConfig
from bot.edge_detector import EdgeSignal
from bot.polymarket_client import OrderResult, PolymarketClient
from bot.position_sizer import SizeResult
from bot.telegram_notifier import TelegramNotifier

log = logging.getLogger("polybot.executor")


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """Immutable record of a completed (or attempted) trade."""

    timestamp: float
    condition_id: str
    asset: str
    direction: str
    entry_price: float
    size_usd: float
    edge: float
    kelly_half: float
    order_id: str | None
    success: bool
    latency_ms: float
    paper: bool
    error: str | None = None


class TradeBackend(Protocol):
    """Abstraction over live vs. paper execution."""

    async def execute(
        self, signal: EdgeSignal, size: SizeResult
    ) -> TradeRecord: ...


class LiveBackend:
    """Sends real orders to Polymarket via the CLOB client."""

    def __init__(
        self, client: PolymarketClient, notifier: TelegramNotifier
    ) -> None:
        self._client = client
        self._notifier = notifier

    async def execute(self, signal: EdgeSignal, size: SizeResult) -> TradeRecord:
        t0 = time.monotonic()
        result: OrderResult = await self._client.place_order(
            token_id=signal.token_id,
            side="BUY",
            price=signal.entry_price,
            size=size.size_usd,
        )
        latency_ms = (time.monotonic() - t0) * 1000

        rec = TradeRecord(
            timestamp=time.time(),
            condition_id=signal.condition_id,
            asset=signal.asset,
            direction=signal.direction,
            entry_price=signal.entry_price,
            size_usd=size.size_usd,
            edge=signal.edge,
            kelly_half=size.kelly_half,
            order_id=result.order_id,
            success=result.success,
            latency_ms=latency_ms,
            paper=False,
            error=result.error,
        )
        await self._log_and_alert(rec)
        return rec

    async def _log_and_alert(self, rec: TradeRecord) -> None:
        status = "FILLED" if rec.success else "FAILED"
        msg = (
            f"{status} | {rec.asset} {rec.direction} | "
            f"${rec.size_usd:.2f} @ {rec.entry_price:.4f} | "
            f"edge {rec.edge * 100:+.2f}% | "
            f"latency {rec.latency_ms:.0f}ms"
        )
        log.info("LIVE TRADE: %s", msg)
        await self._notifier.alert(
            f"LIVE {status}",
            msg + (f"\nError: {rec.error}" if rec.error else ""),
        )


class PaperBackend:
    """Simulates execution – no real orders, logs to paper_trader."""

    def __init__(self, notifier: TelegramNotifier) -> None:
        self._notifier = notifier
        self._seq = 0

    async def execute(self, signal: EdgeSignal, size: SizeResult) -> TradeRecord:
        t0 = time.monotonic()
        self._seq += 1
        # Simulate ~5 ms fill latency
        latency_ms = (time.monotonic() - t0) * 1000 + 5.0

        rec = TradeRecord(
            timestamp=time.time(),
            condition_id=signal.condition_id,
            asset=signal.asset,
            direction=signal.direction,
            entry_price=signal.entry_price,
            size_usd=size.size_usd,
            edge=signal.edge,
            kelly_half=size.kelly_half,
            order_id=f"PAPER-{self._seq:06d}",
            success=True,
            latency_ms=latency_ms,
            paper=True,
        )
        await self._log_and_alert(rec)
        return rec

    async def _log_and_alert(self, rec: TradeRecord) -> None:
        msg = (
            f"PAPER | {rec.asset} {rec.direction} | "
            f"${rec.size_usd:.2f} @ {rec.entry_price:.4f} | "
            f"edge {rec.edge * 100:+.2f}% | "
            f"latency {rec.latency_ms:.0f}ms"
        )
        log.info(msg)
        await self._notifier.alert("PAPER TRADE", msg)


class Executor:
    """Top-level executor that enforces latency budget & delegates to backend."""

    def __init__(
        self,
        cfg: TradingConfig,
        backend: TradeBackend,
        notifier: TelegramNotifier,
    ) -> None:
        self._cfg = cfg
        self._backend = backend
        self._notifier = notifier

    async def execute(self, signal: EdgeSignal, size: SizeResult) -> TradeRecord:
        t0 = time.monotonic()
        rec = await self._backend.execute(signal, size)
        total_ms = (time.monotonic() - t0) * 1000

        if total_ms > self._cfg.target_execution_ms:
            log.warning(
                "Execution exceeded target: %.0fms > %dms",
                total_ms,
                self._cfg.target_execution_ms,
            )
        return rec
