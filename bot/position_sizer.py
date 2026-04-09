"""Half-Kelly position sizing with hard portfolio cap."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.config import TradingConfig
from bot.edge_detector import EdgeSignal

log = logging.getLogger("polybot.sizer")


@dataclass(frozen=True, slots=True)
class SizeResult:
    """Output of the position sizer."""

    size_usd: float
    kelly_full: float
    kelly_half: float
    capped: bool          # True if hard cap was applied
    reason: str           # human-readable explanation


class PositionSizer:
    """Half-Kelly Criterion position sizing."""

    def __init__(self, cfg: TradingConfig) -> None:
        self._cfg = cfg

    def calculate(self, signal: EdgeSignal, portfolio_usd: float) -> SizeResult:
        """
        Compute position size in USD for *signal* given current *portfolio_usd*.

        Kelly Criterion for a binary bet:
            f* = (p * b - q) / b
        where
            p = estimated win probability  (model_prob)
            q = 1 - p
            b = net odds received          (payout / cost - 1)

        We use half-Kelly:  size = (f* / 2) * portfolio
        Then hard-cap at max_portfolio_pct_per_trade.
        """
        if portfolio_usd <= 0:
            return SizeResult(0.0, 0.0, 0.0, False, "zero portfolio")

        p = signal.model_prob
        q = 1.0 - p
        entry = signal.entry_price

        if entry <= 0 or entry >= 1:
            return SizeResult(0.0, 0.0, 0.0, False, f"invalid entry price {entry}")

        # Binary outcome: pay `entry`, receive 1.0 if correct → net odds
        b = (1.0 / entry) - 1.0
        if b <= 0:
            return SizeResult(0.0, 0.0, 0.0, False, "non-positive odds")

        kelly_full = (p * b - q) / b

        if kelly_full <= 0:
            return SizeResult(
                0.0, kelly_full, 0.0, False,
                f"negative Kelly ({kelly_full:.4f}) – no edge per model",
            )

        kelly_half = kelly_full * self._cfg.kelly_fraction
        size_pct = kelly_half
        capped = False

        if size_pct > self._cfg.max_portfolio_pct_per_trade:
            size_pct = self._cfg.max_portfolio_pct_per_trade
            capped = True

        size_usd = round(size_pct * portfolio_usd, 2)

        # Floor at $1 to avoid dust orders
        if size_usd < 1.0:
            return SizeResult(0.0, kelly_full, kelly_half, capped, "sub-$1 size")

        log.info(
            "Size: $%.2f (%.2f%% portfolio) | Kelly full=%.4f half=%.4f%s | edge=%.2f%%",
            size_usd,
            size_pct * 100,
            kelly_full,
            kelly_half,
            " CAPPED" if capped else "",
            signal.abs_edge * 100,
        )

        return SizeResult(
            size_usd=size_usd,
            kelly_full=kelly_full,
            kelly_half=kelly_half,
            capped=capped,
            reason="ok",
        )
