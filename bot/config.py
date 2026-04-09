"""Centralised configuration loaded from environment variables."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        sys.exit(f"FATAL: environment variable {name} is not set. See .env.example")
    return val


# ---------------------------------------------------------------------------
# Credentials (always loaded from env)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Credentials:
    poly_api_key: str = field(repr=False)
    poly_private_key: str = field(repr=False)
    telegram_token: str = field(repr=False)
    telegram_chat_id: str = field(repr=False)
    alchemy_rpc_url: str = field(repr=False)

    @classmethod
    def from_env(cls) -> Credentials:
        return cls(
            poly_api_key=_require_env("POLY_API_KEY"),
            poly_private_key=_require_env("POLY_PRIVATE_KEY"),
            telegram_token=_require_env("TELEGRAM_TOKEN"),
            telegram_chat_id=_require_env("TELEGRAM_CHAT_ID"),
            alchemy_rpc_url=_require_env("ALCHEMY_RPC_URL"),
        )


# ---------------------------------------------------------------------------
# Trading parameters
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TradingConfig:
    # Edge thresholds
    min_detectable_edge: float = 0.05       # 5% – ignore below
    min_execution_edge: float = 0.08        # 8% – only trade above

    # Market filters
    allowed_assets: tuple[str, ...] = ("BTC", "ETH")
    allowed_durations_minutes: tuple[int, ...] = (5, 15)
    min_market_liquidity_usd: float = 50_000.0
    max_monitored_markets: int = 20

    # Position sizing – half-Kelly
    kelly_fraction: float = 0.5
    max_portfolio_pct_per_trade: float = 0.08   # hard cap 8 %

    # Risk management
    daily_loss_halt_pct: float = -0.20          # -20 % of day-start balance
    drawdown_permanent_halt_pct: float = 0.60   # 60 % of all-time high
    consecutive_loss_pause_count: int = 5
    consecutive_loss_pause_minutes: int = 30

    # Price freshness
    max_price_age_seconds: float = 10.0

    # WebSocket
    binance_ws_url: str = "wss://stream.binance.com:9443"
    ws_max_reconnect_retries: int = 10

    # Execution
    target_execution_ms: int = 800

    # Logging
    log_dir: Path = Path("logs")
    log_max_bytes: int = 50 * 1024 * 1024       # 50 MB
    log_backup_count: int = 5

    # Paper trading database
    paper_db_path: Path = Path("paper_trades.db")

    # Heartbeat interval
    heartbeat_interval_seconds: int = 3600       # 1 hour
