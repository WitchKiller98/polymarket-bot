"""SQLite-backed paper trading ledger."""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from bot.executor import TradeRecord

log = logging.getLogger("polybot.paper")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    condition_id    TEXT    NOT NULL,
    asset           TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    size_usd        REAL    NOT NULL,
    edge            REAL    NOT NULL,
    kelly_half      REAL    NOT NULL,
    order_id        TEXT,
    success         INTEGER NOT NULL,
    latency_ms      REAL    NOT NULL,
    pnl             REAL    DEFAULT 0.0,
    settled         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    balance_usd     REAL    NOT NULL,
    note            TEXT
);
"""


class PaperLedger:
    """Async SQLite ledger for paper trades."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(str(self._path))
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        log.info("Paper ledger opened: %s", self._path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def record_trade(self, rec: TradeRecord) -> int:
        assert self._db is not None
        cursor = await self._db.execute(
            """
            INSERT INTO trades
                (timestamp, condition_id, asset, direction, entry_price,
                 size_usd, edge, kelly_half, order_id, success, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.timestamp,
                rec.condition_id,
                rec.asset,
                rec.direction,
                rec.entry_price,
                rec.size_usd,
                rec.edge,
                rec.kelly_half,
                rec.order_id,
                int(rec.success),
                rec.latency_ms,
            ),
        )
        await self._db.commit()
        row_id = cursor.lastrowid or 0
        log.debug("Paper trade #%d recorded", row_id)
        return row_id

    async def settle_trade(self, trade_id: int, pnl: float) -> None:
        assert self._db is not None
        await self._db.execute(
            "UPDATE trades SET pnl = ?, settled = 1 WHERE id = ?",
            (pnl, trade_id),
        )
        await self._db.commit()

    async def snapshot_balance(self, balance: float, note: str = "") -> None:
        assert self._db is not None
        import time

        await self._db.execute(
            "INSERT INTO balance_snapshots (timestamp, balance_usd, note) VALUES (?, ?, ?)",
            (time.time(), balance, note),
        )
        await self._db.commit()

    async def total_pnl(self) -> float:
        assert self._db is not None
        async with self._db.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE settled = 1"
        ) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else 0.0

    async def trade_count_today(self) -> int:
        """Count trades from the last 24 hours."""
        assert self._db is not None
        import time

        cutoff = time.time() - 86400
        async with self._db.execute(
            "SELECT COUNT(*) FROM trades WHERE timestamp >= ?", (cutoff,)
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0
