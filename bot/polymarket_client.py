"""Async wrapper around Polymarket's CLOB API for market data and order placement."""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any

import aiohttp
import orjson

from bot.config import Credentials, TradingConfig

log = logging.getLogger("polybot.polymarket")

# Polymarket CLOB REST base
_CLOB_BASE = "https://clob.polymarket.com"


@dataclass
class PolyMarket:
    """Slim representation of a Polymarket binary market."""

    condition_id: str
    question: str
    token_id_yes: str
    token_id_no: str
    best_bid_yes: float
    best_ask_yes: float
    best_bid_no: float
    best_ask_no: float
    liquidity_usd: float
    asset: str           # "BTC" or "ETH"
    duration_minutes: int
    updated_at: float    # monotonic


@dataclass
class OrderResult:
    success: bool
    order_id: str | None = None
    filled_price: float | None = None
    filled_size: float | None = None
    error: str | None = None


class PolymarketClient:
    """Async Polymarket CLOB client."""

    def __init__(self, creds: Credentials, cfg: TradingConfig) -> None:
        self._api_key = creds.poly_api_key
        self._private_key = creds.poly_private_key
        self._rpc_url = creds.alchemy_rpc_url
        self._cfg = cfg
        self._session: aiohttp.ClientSession | None = None
        self._markets: dict[str, PolyMarket] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            self._session = aiohttp.ClientSession(
                base_url=_CLOB_BASE,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def fetch_markets(self) -> list[PolyMarket]:
        """Fetch and filter BTC/ETH short-duration markets from CLOB."""
        session = await self._ensure_session()
        markets: list[PolyMarket] = []

        try:
            async with session.get("/markets", params={"limit": "100"}) as resp:
                if resp.status != 200:
                    log.warning("Market fetch failed: %s", resp.status)
                    return []
                payload = await resp.json()
        except Exception:
            log.exception("Error fetching markets")
            return []

        now = time.monotonic()

        for m in payload if isinstance(payload, list) else payload.get("data", []):
            asset = self._extract_asset(m)
            if asset is None:
                continue
            duration = self._extract_duration(m)
            if duration not in self._cfg.allowed_durations_minutes:
                continue

            tokens = m.get("tokens", [])
            if len(tokens) < 2:
                continue

            liquidity = float(m.get("volume", 0) or 0)
            if liquidity < self._cfg.min_market_liquidity_usd:
                continue

            try:
                pm = PolyMarket(
                    condition_id=m["condition_id"],
                    question=m.get("question", ""),
                    token_id_yes=tokens[0].get("token_id", ""),
                    token_id_no=tokens[1].get("token_id", ""),
                    best_bid_yes=float(tokens[0].get("price", 0)),
                    best_ask_yes=1.0 - float(tokens[1].get("price", 0)),
                    best_bid_no=float(tokens[1].get("price", 0)),
                    best_ask_no=1.0 - float(tokens[0].get("price", 0)),
                    liquidity_usd=liquidity,
                    asset=asset,
                    duration_minutes=duration,
                    updated_at=now,
                )
            except (KeyError, ValueError):
                continue

            markets.append(pm)

        # Enforce max monitored markets (sort by liquidity descending)
        markets.sort(key=lambda x: x.liquidity_usd, reverse=True)
        markets = markets[: self._cfg.max_monitored_markets]

        async with self._lock:
            self._markets = {pm.condition_id: pm for pm in markets}

        log.info("Tracking %d Polymarket markets", len(markets))
        return markets

    @property
    def markets(self) -> dict[str, PolyMarket]:
        return dict(self._markets)

    # ------------------------------------------------------------------
    # Order book refresh (per-market)
    # ------------------------------------------------------------------

    async def refresh_book(self, condition_id: str) -> PolyMarket | None:
        """Fetch latest order-book snapshot for a single market."""
        session = await self._ensure_session()
        try:
            async with session.get(
                f"/book", params={"token_id": self._markets[condition_id].token_id_yes}
            ) as resp:
                if resp.status != 200:
                    return None
                book = await resp.json()
        except Exception:
            log.exception("Error refreshing book for %s", condition_id)
            return None

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        async with self._lock:
            mkt = self._markets.get(condition_id)
            if mkt is None:
                return None
            updates: dict[str, Any] = {"updated_at": time.monotonic()}
            if bids:
                updates["best_bid_yes"] = float(bids[0].get("price", mkt.best_bid_yes))
            if asks:
                updates["best_ask_yes"] = float(asks[0].get("price", mkt.best_ask_yes))
            mkt = replace(mkt, **updates)
            self._markets[condition_id] = mkt
        return mkt

    # ------------------------------------------------------------------
    # Order placement (live mode)
    # ------------------------------------------------------------------

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> OrderResult:
        """Place a limit order on the CLOB. Returns OrderResult."""
        session = await self._ensure_session()
        body: dict[str, Any] = {
            "tokenID": token_id,
            "side": side.upper(),
            "price": str(round(price, 4)),
            "size": str(round(size, 2)),
        }
        try:
            async with session.post("/order", json=body) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("orderID"):
                    return OrderResult(
                        success=True,
                        order_id=data["orderID"],
                        filled_price=float(data.get("filledPrice", price)),
                        filled_size=float(data.get("filledSize", size)),
                    )
                return OrderResult(
                    success=False,
                    error=data.get("error", f"HTTP {resp.status}"),
                )
        except Exception as exc:
            return OrderResult(success=False, error=str(exc))

    # ------------------------------------------------------------------
    # Portfolio value via Alchemy RPC
    # ------------------------------------------------------------------

    async def get_usdc_balance(self) -> float:
        """Query USDC balance on Polygon via Alchemy RPC."""
        # USDC on Polygon PoS
        usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        # balanceOf(address) selector
        selector = "0x70a08231"

        # Derive address from private key (first 20 bytes of keccak not done
        # here – for production the web3 library handles it).  We keep a
        # lightweight RPC call approach.
        try:
            from web3 import Web3

            w3 = Web3()
            acct = w3.eth.account.from_key(self._private_key)
            address = acct.address
        except Exception:
            log.exception("Cannot derive address from private key")
            return 0.0

        padded = "0x" + address[2:].lower().zfill(64)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {"to": usdc_contract, "data": selector + padded[2:]},
                "latest",
            ],
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as s:
                async with s.post(self._rpc_url, json=payload) as resp:
                    result = await resp.json()
                    hex_val = result.get("result", "0x0")
                    return int(hex_val, 16) / 1e6  # USDC has 6 decimals
        except Exception:
            log.exception("USDC balance RPC failed")
            return 0.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_asset(market: dict) -> str | None:
        question = (market.get("question") or "").upper()
        tags = [t.upper() for t in (market.get("tags") or [])]
        for asset in ("BTC", "ETH", "BITCOIN", "ETHEREUM"):
            if asset in question or asset in tags:
                return "BTC" if asset in ("BTC", "BITCOIN") else "ETH"
        return None

    @staticmethod
    def _extract_duration(market: dict) -> int:
        """Return estimated market duration in minutes (heuristic)."""
        desc = (market.get("description") or market.get("question") or "").lower()
        if "5 min" in desc or "5-min" in desc:
            return 5
        if "15 min" in desc or "15-min" in desc:
            return 15
        end_date = market.get("end_date_iso")
        if end_date:
            try:
                end = _dt.datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                now = _dt.datetime.now(_dt.timezone.utc)
                delta = (end - now).total_seconds() / 60
                if delta <= 7:
                    return 5
                if delta <= 20:
                    return 15
            except Exception:
                pass
        return 0
