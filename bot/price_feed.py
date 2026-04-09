"""Binance WebSocket price feed with auto-reconnect and staleness detection."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Coroutine, Any

import orjson
import websockets
import websockets.exceptions

from bot.config import TradingConfig

log = logging.getLogger("polybot.price_feed")


@dataclass
class PriceUpdate:
    symbol: str          # "BTCUSDT" or "ETHUSDT"
    price: float
    timestamp: float     # exchange epoch seconds
    local_ts: float      # time.monotonic() when received


@dataclass
class PriceFeed:
    """Manages a Binance combined-stream WebSocket for BTC/ETH tickers."""

    cfg: TradingConfig
    _prices: dict[str, PriceUpdate] = field(default_factory=dict)
    _running: bool = False
    _on_price: Callable[[PriceUpdate], Coroutine[Any, Any, None]] | None = None
    _task: asyncio.Task[None] | None = None
    _stale: bool = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_callback(self, cb: Callable[[PriceUpdate], Coroutine[Any, Any, None]]) -> None:
        self._on_price = cb

    @property
    def is_stale(self) -> bool:
        """True when we have no recent data for ANY tracked symbol."""
        if not self._prices:
            return True
        now = time.monotonic()
        return any(
            (now - p.local_ts) > self.cfg.max_price_age_seconds
            for p in self._prices.values()
        )

    def latest(self, symbol: str) -> PriceUpdate | None:
        p = self._prices.get(symbol)
        if p is None:
            return None
        if (time.monotonic() - p.local_ts) > self.cfg.max_price_age_seconds:
            return None  # stale – treat as unavailable
        return p

    async def run_forever(self) -> None:
        """Awaitable entry point – use in asyncio.gather() for proper lifecycle."""
        self._running = True
        await self._run()

    async def start(self) -> None:
        """Fire-and-forget variant (creates a background task)."""
        self._running = True
        self._task = asyncio.create_task(self._run(), name="price_feed")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_url(self) -> str:
        streams = "btcusdt@ticker/ethusdt@ticker"
        return f"{self.cfg.binance_ws_url}/stream?streams={streams}"

    async def _run(self) -> None:
        retries = 0
        while self._running and retries <= self.cfg.ws_max_reconnect_retries:
            try:
                await self._connect()
                retries = 0  # reset on clean exit
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
                OSError,
            ) as exc:
                retries += 1
                backoff = min(2 ** retries, 60)
                log.warning(
                    "WS disconnected (%s), reconnect %d/%d in %ds",
                    exc,
                    retries,
                    self.cfg.ws_max_reconnect_retries,
                    backoff,
                )
                self._stale = True
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                return

        if retries > self.cfg.ws_max_reconnect_retries:
            log.error("Exhausted %d reconnect retries – price feed dead", retries)
            self._stale = True

    async def _connect(self) -> None:
        url = self._build_url()
        log.info("Connecting to Binance WS: %s", url)
        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            log.info("Binance WS connected")
            self._stale = False
            async for raw in ws:
                if not self._running:
                    return
                self._handle_message(raw)

    def _handle_message(self, raw: bytes | str) -> None:
        try:
            msg = orjson.loads(raw)
        except orjson.JSONDecodeError:
            log.debug("Non-JSON WS message: %s", raw[:200])
            return

        data = msg.get("data")
        if data is None:
            return

        symbol: str = data.get("s", "")
        if symbol not in ("BTCUSDT", "ETHUSDT"):
            return

        try:
            price = float(data["c"])  # last price
            exchange_ts = float(data["E"]) / 1000.0  # event time ms → s
        except (KeyError, ValueError):
            return

        now_mono = time.monotonic()
        now_wall = time.time()

        # Reject stale exchange data (>10 s old)
        if abs(now_wall - exchange_ts) > self.cfg.max_price_age_seconds:
            log.debug("Rejected stale price for %s (age %.1fs)", symbol, now_wall - exchange_ts)
            return

        update = PriceUpdate(
            symbol=symbol,
            price=price,
            timestamp=exchange_ts,
            local_ts=now_mono,
        )
        self._prices[symbol] = update

        if self._on_price is not None:
            asyncio.get_running_loop().create_task(self._on_price(update))
