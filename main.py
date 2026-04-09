#!/usr/bin/env python3
"""
Polymarket Latency Arbitrage Bot – CLI entry point.

PAPER MODE (default):
    python main.py

LIVE MODE (requires ALL three flags):
    python main.py --live --confirm --i-understand-risks
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Polymarket latency arbitrage bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "SAFETY: Live trading requires --live --confirm --i-understand-risks.\n"
            "Paper mode is enabled by default and logs all trades to SQLite."
        ),
    )
    p.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Enable live trading (requires --confirm and --i-understand-risks)",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Confirm live trading intent",
    )
    p.add_argument(
        "--i-understand-risks",
        action="store_true",
        default=False,
        help="Acknowledge trading risks",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    live = False
    if args.live:
        if not args.confirm or not args.i_understand_risks:
            print(
                "ERROR: Live trading requires ALL three flags:\n"
                "  --live --confirm --i-understand-risks\n\n"
                "Run without flags for paper mode.",
                file=sys.stderr,
            )
            sys.exit(1)

        print("=" * 60)
        print("  WARNING: LIVE TRADING MODE")
        print("  Real money will be at risk.")
        print("  Kill switches are active but NOT foolproof.")
        print("=" * 60)
        response = input("Type 'YES' to proceed: ").strip()
        if response != "YES":
            print("Aborted.")
            sys.exit(0)
        live = True

    # Late import so env validation happens after arg parse
    from bot.bot import ArbitrageBot

    bot = ArbitrageBot(live=live)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Graceful shutdown on SIGINT / SIGTERM
    def _shutdown(sig: signal.Signals) -> None:
        print(f"\nReceived {sig.name}, shutting down…")
        loop.create_task(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        loop.run_until_complete(bot.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
