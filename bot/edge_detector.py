"""Latency-edge detection: compare Binance spot price to Polymarket implied price."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from bot.config import TradingConfig
from bot.polymarket_client import PolyMarket
from bot.price_feed import PriceFeed

log = logging.getLogger("polybot.edge")


@dataclass(frozen=True, slots=True)
class EdgeSignal:
    """Describes a detected trading opportunity."""

    condition_id: str
    asset: str
    direction: str              # "YES" or "NO"
    token_id: str
    implied_prob: float         # Polymarket implied probability
    model_prob: float           # our estimate from Binance price movement
    edge: float                 # model_prob - implied_prob (signed)
    abs_edge: float             # absolute value
    entry_price: float          # price we'd pay on Polymarket
    market_liquidity: float
    detected_at: float          # monotonic


class EdgeDetector:
    """
    Compare real-time CEX price momentum against Polymarket implied odds.

    For a *short-duration* binary ("Will BTC be above $X in 5 min?"),
    the Binance spot price and its recent velocity give us a faster
    probability estimate than the Polymarket order book.
    """

    def __init__(self, cfg: TradingConfig, feed: PriceFeed) -> None:
        self._cfg = cfg
        self._feed = feed
        # Rolling price history: symbol → list of (monotonic_ts, price)
        self._history: dict[str, list[tuple[float, float]]] = {}
        self._history_window = 120.0  # keep 2 minutes of ticks

    # ------------------------------------------------------------------
    # Ingest price ticks (called from PriceFeed callback)
    # ------------------------------------------------------------------

    def record_tick(self, symbol: str, price: float, ts: float) -> None:
        buf = self._history.setdefault(symbol, [])
        buf.append((ts, price))
        # Trim old entries
        cutoff = ts - self._history_window
        while buf and buf[0][0] < cutoff:
            buf.pop(0)

    # ------------------------------------------------------------------
    # Scan all tracked markets for edges
    # ------------------------------------------------------------------

    def scan(self, markets: dict[str, PolyMarket]) -> list[EdgeSignal]:
        """Return list of actionable EdgeSignals (edge >= min_execution_edge)."""
        signals: list[EdgeSignal] = []

        for cid, mkt in markets.items():
            binance_sym = f"{mkt.asset}USDT"
            latest = self._feed.latest(binance_sym)
            if latest is None:
                continue  # stale or missing

            model_prob = self._estimate_probability(mkt, latest.price)
            if model_prob is None:
                continue

            # YES side edge: we think YES is more likely than market implies
            implied_yes = mkt.best_ask_yes if mkt.best_ask_yes > 0 else 0.5
            edge_yes = model_prob - implied_yes

            # NO side edge: we think NO is more likely
            implied_no = mkt.best_ask_no if mkt.best_ask_no > 0 else 0.5
            edge_no = (1.0 - model_prob) - implied_no

            # Pick the stronger side
            if edge_yes >= edge_no:
                edge, direction = edge_yes, "YES"
                entry = mkt.best_ask_yes
                token_id = mkt.token_id_yes
                implied = implied_yes
                model_p = model_prob
            else:
                edge, direction = edge_no, "NO"
                entry = mkt.best_ask_no
                token_id = mkt.token_id_no
                implied = implied_no
                model_p = 1.0 - model_prob

            abs_edge = abs(edge)

            if abs_edge < self._cfg.min_detectable_edge:
                continue  # noise

            if abs_edge < self._cfg.min_execution_edge:
                log.debug(
                    "Detectable but sub-threshold edge %.2f%% on %s",
                    abs_edge * 100,
                    cid[:12],
                )
                continue

            signals.append(
                EdgeSignal(
                    condition_id=cid,
                    asset=mkt.asset,
                    direction=direction,
                    token_id=token_id,
                    implied_prob=implied,
                    model_prob=model_p,
                    edge=edge,
                    abs_edge=abs_edge,
                    entry_price=entry,
                    market_liquidity=mkt.liquidity_usd,
                    detected_at=time.monotonic(),
                )
            )

        # Sort by absolute edge descending – best opportunity first
        signals.sort(key=lambda s: s.abs_edge, reverse=True)
        return signals

    # ------------------------------------------------------------------
    # Probability model (momentum-based)
    # ------------------------------------------------------------------

    def _estimate_probability(self, mkt: PolyMarket, current_price: float) -> float | None:
        """
        Very simple momentum model:
        - Compute short-term return (last 30 s).
        - Map that to a probability that price will be *above current level*
          at expiry using a logistic function.

        In production you'd replace this with a proper model (e.g., GBM
        simulation, order-flow imbalance, vol-adjusted z-score).
        """
        symbol = f"{mkt.asset}USDT"
        buf = self._history.get(symbol)
        if not buf or len(buf) < 5:
            return None

        now = buf[-1][0]
        # 30-second lookback return
        lookback = 30.0
        past_idx = 0
        for i, (ts, _) in enumerate(buf):
            if ts >= now - lookback:
                past_idx = i
                break
        past_price = buf[past_idx][1]
        if past_price == 0:
            return None

        ret = (current_price - past_price) / past_price

        # Logistic mapping: ret → probability in (0, 1)
        # Scale factor tuned so a 0.5 % 30-s move maps to ~0.65 prob
        import math

        scale = 130.0  # sensitivity knob
        prob = 1.0 / (1.0 + math.exp(-scale * ret))

        # Clamp to avoid extreme model confidence
        prob = max(0.02, min(0.98, prob))
        return prob
