"""Main bot orchestrator – wires all components together."""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import random
import time

from bot.config import Credentials, TradingConfig
from bot.edge_detector import EdgeDetector, EdgeSignal
from bot.executor import (
    Executor,
    LiveBackend,
    PaperBackend,
    TradeRecord,
)
from bot.logger import setup_logging
from bot.paper_trader import PaperLedger
from bot.polymarket_client import PolymarketClient
from bot.position_sizer import PositionSizer, SizeResult
from bot.price_feed import PriceFeed, PriceUpdate
from bot.risk_manager import RiskManager
from bot.telegram_notifier import TelegramNotifier

log = logging.getLogger("polybot.core")


class ArbitrageBot:
    """Top-level async orchestrator."""

    def __init__(self, *, live: bool = False) -> None:
        self._live = live
        self._cfg = TradingConfig()
        self._creds = Credentials.from_env()
        self._logger = setup_logging(self._cfg)

        # Components
        self._notifier = TelegramNotifier(self._creds)
        self._feed = PriceFeed(cfg=self._cfg)
        self._poly = PolymarketClient(self._creds, self._cfg)
        self._detector = EdgeDetector(self._cfg, self._feed)
        self._sizer = PositionSizer(self._cfg)

        # Deferred until we know initial balance
        self._risk: RiskManager | None = None
        self._executor: Executor | None = None
        self._ledger: PaperLedger | None = None

        # Runtime
        self._portfolio_usd: float = 0.0
        self._initial_paper_balance: float = 10_000.0
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._scan_interval = 2.0  # seconds between edge scans

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        mode = "LIVE" if self._live else "PAPER"
        log.info("===== Polymarket Latency Arbitrage Bot [%s MODE] =====", mode)

        # Initialise portfolio balance
        self._portfolio_usd = await self._poly.get_usdc_balance()
        if self._portfolio_usd <= 0 and self._live:
            log.warning("Zero USDC balance – falling back to $10,000 paper balance")
            self._portfolio_usd = self._initial_paper_balance

        if not self._live:
            if self._portfolio_usd <= 0:
                self._portfolio_usd = self._initial_paper_balance

        log.info("Starting portfolio: $%.2f", self._portfolio_usd)

        # Wire remaining components
        self._risk = RiskManager(self._cfg, self._notifier, self._portfolio_usd)

        if self._live:
            backend: LiveBackend | PaperBackend = LiveBackend(self._poly, self._notifier)
        else:
            backend = PaperBackend(self._notifier)
        self._executor = Executor(self._cfg, backend, self._notifier)

        if not self._live:
            self._ledger = PaperLedger(self._cfg.paper_db_path)
            await self._ledger.open()
            await self._ledger.snapshot_balance(self._portfolio_usd, "bot_start")

        # Set up price feed callback
        self._feed.set_callback(self._on_price_update)

        # Startup alert
        await self._notifier.alert(
            f"Bot Started [{mode}]",
            f"Balance: ${self._portfolio_usd:,.2f}\n"
            f"Markets: BTC/ETH, 5m & 15m\n"
            f"Min edge: {self._cfg.min_execution_edge * 100:.0f}%",
        )

        self._running = True
        self._shutdown_event.clear()

        # Launch concurrent tasks – create explicitly so we can cancel on error
        tasks = [
            asyncio.create_task(self._feed.run_forever(), name="price_feed"),
            asyncio.create_task(self._market_refresh_loop(), name="market_refresh"),
            asyncio.create_task(self._scan_loop(), name="scan"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._daily_reset_loop(), name="daily_reset"),
        ]

        try:
            # If any task raises, cancel the rest and propagate
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            # Check for exceptions in completed tasks
            for t in done:
                if t.exception() is not None:
                    log.error("Task %s failed: %s", t.get_name(), t.exception())
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        self._shutdown_event.set()
        await self._feed.stop()
        if self._ledger:
            await self._ledger.close()
        await self._poly.close()
        await self._notifier.alert("Bot Stopped", self._risk.summary() if self._risk else "")
        await self._notifier.close()
        log.info("Bot shut down cleanly")

    # ------------------------------------------------------------------
    # Interruptible sleep helper
    # ------------------------------------------------------------------

    async def _sleep(self, seconds: float) -> None:
        """Sleep that wakes immediately on shutdown signal."""
        try:
            await asyncio.wait_for(
                self._shutdown_event.wait(), timeout=seconds
            )
        except asyncio.TimeoutError:
            pass  # Normal: timeout expired before shutdown

    # ------------------------------------------------------------------
    # Price callback
    # ------------------------------------------------------------------

    async def _on_price_update(self, update: PriceUpdate) -> None:
        self._detector.record_tick(update.symbol, update.price, update.local_ts)

    # ------------------------------------------------------------------
    # Market refresh (every 60 s)
    # ------------------------------------------------------------------

    async def _market_refresh_loop(self) -> None:
        while self._running:
            try:
                await self._poly.fetch_markets()
            except Exception:
                log.exception("Market refresh error")
                await self._notifier.alert(
                    "ERROR", "Market refresh failed - see logs."
                )
            await self._sleep(60)

    # ------------------------------------------------------------------
    # Core edge-scan loop
    # ------------------------------------------------------------------

    async def _scan_loop(self) -> None:
        # Wait a bit for initial data
        await self._sleep(5)

        while self._running:
            try:
                await self._scan_once()
            except Exception:
                log.exception("Scan loop error")
                await self._notifier.alert(
                    "ERROR", "Scan loop exception - see logs."
                )
            await self._sleep(self._scan_interval)

    async def _scan_once(self) -> None:
        assert self._risk is not None
        assert self._executor is not None

        # Check if price feed is stale → pause trading
        if self._feed.is_stale:
            log.debug("Price data stale – skipping scan")
            return

        # Check risk limits
        allowed, reason = await self._risk.can_trade()
        if not allowed:
            log.debug("Trading blocked: %s", reason)
            return

        # Scan for edges
        markets = self._poly.markets
        if not markets:
            return

        signals = self._detector.scan(markets)
        if not signals:
            return

        # Execute the best signal
        best = signals[0]
        log.info(
            "Edge detected: %s %s edge=%.2f%% on %s",
            best.asset,
            best.direction,
            best.abs_edge * 100,
            best.condition_id[:12],
        )

        # Recalculate portfolio value before sizing
        fresh_balance = await self._poly.get_usdc_balance()
        if fresh_balance > 0:
            self._portfolio_usd = fresh_balance
        elif not self._live and self._ledger:
            pnl = await self._ledger.total_pnl()
            self._portfolio_usd = self._initial_paper_balance + pnl

        size = self._sizer.calculate(best, self._portfolio_usd)
        if size.size_usd <= 0:
            log.debug("Sizer returned zero: %s", size.reason)
            return

        # Execute
        rec: TradeRecord = await self._executor.execute(best, size)

        # Record in ledger and settle immediately for paper mode
        trade_id: int | None = None
        if self._ledger and rec.paper:
            trade_id = await self._ledger.record_trade(rec)

        # Compute and record PnL
        if rec.success:
            simulated_pnl = self._simulate_pnl(best, size)
            new_balance = self._portfolio_usd + simulated_pnl
            await self._risk.record_trade(simulated_pnl, new_balance)
            self._portfolio_usd = new_balance

            # Settle paper trade in the ledger
            if self._ledger and trade_id is not None:
                await self._ledger.settle_trade(trade_id, simulated_pnl)
                await self._ledger.snapshot_balance(new_balance, "post_trade")

    @staticmethod
    def _simulate_pnl(signal: EdgeSignal, size: SizeResult) -> float:
        """
        Quick PnL simulation for paper/risk tracking.

        Uses the model probability to determine win/loss.
        Win: (1 / entry_price - 1) * size   (binary pays $1)
        Loss: -size
        """
        if random.random() < signal.model_prob:
            return size.size_usd * (1.0 / signal.entry_price - 1.0)
        return -size.size_usd

    # ------------------------------------------------------------------
    # Heartbeat (hourly)
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await self._sleep(self._cfg.heartbeat_interval_seconds)
            if not self._running:
                break
            try:
                await self._send_heartbeat()
            except Exception:
                log.exception("Heartbeat error")

    async def _send_heartbeat(self) -> None:
        assert self._risk is not None
        state = self._risk.state
        mode = "LIVE" if self._live else "PAPER"
        msg = (
            f"Mode: {mode}\n"
            f"Balance: ${state.current_balance:,.2f}\n"
            f"Today's trades: {state.trades_today}\n"
            f"Win rate: {self._risk.win_rate() * 100:.0f}%\n"
            f"Day P&L: ${self._risk.daily_pnl():+,.2f}\n"
            f"Price stale: {self._feed.is_stale}\n"
            f"Markets tracked: {len(self._poly.markets)}"
        )
        await self._notifier.alert("Hourly Heartbeat", msg)
        log.info("Heartbeat sent")

    # ------------------------------------------------------------------
    # Daily reset (at UTC midnight)
    # ------------------------------------------------------------------

    async def _daily_reset_loop(self) -> None:
        """Reset risk counters at the start of each UTC day."""
        while self._running:
            now = _dt.datetime.now(_dt.timezone.utc)
            tomorrow = (now + _dt.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            seconds_until = (tomorrow - now).total_seconds()
            await self._sleep(seconds_until)

            if not self._running or self._risk is None:
                break

            fresh = await self._poly.get_usdc_balance()
            if fresh > 0:
                self._portfolio_usd = fresh
            await self._risk.reset_daily(self._portfolio_usd)
            await self._notifier.alert(
                "Daily Reset",
                f"Risk counters reset. New day-start balance: ${self._portfolio_usd:,.2f}",
            )
            log.info("Daily reset complete")
