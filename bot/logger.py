"""Rotating file + console logger."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot.config import TradingConfig


def setup_logging(cfg: TradingConfig) -> logging.Logger:
    """Return the root application logger with rotating file + stderr handlers."""
    log_dir: Path = cfg.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("polybot")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # Rotating file handler – 50 MB max, 5 backups
    fh = RotatingFileHandler(
        log_dir / "polybot.log",
        maxBytes=cfg.log_max_bytes,
        backupCount=cfg.log_backup_count,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger
