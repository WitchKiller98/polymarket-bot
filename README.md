# Polymarket Latency Arbitrage Bot

Async Python bot that detects pricing edges between Binance spot prices (via WebSocket) and Polymarket binary contracts, then executes trades using half-Kelly position sizing with comprehensive risk management.

**Paper mode by default. No real money at risk unless you explicitly opt in with three CLI flags.**

## Architecture

```
main.py                        CLI entry point + triple-flag live gate
bot/
├── config.py                  Credentials + trading parameters from .env
├── logger.py                  50 MB rotating file + stderr logging
├── telegram_notifier.py       Async Telegram alerts (HTML-escaped)
├── price_feed.py              Binance WebSocket (BTC/ETH, auto-reconnect)
├── polymarket_client.py       CLOB market discovery + order placement
├── edge_detector.py           Momentum-based probability vs implied odds
├── position_sizer.py          Half-Kelly with 8% portfolio hard cap
├── risk_manager.py            Kill switches (daily loss, drawdown, streaks)
├── executor.py                Routes to live or paper backend
├── paper_trader.py            SQLite ledger for simulated trades
└── bot.py                     Async orchestrator (scan loop, heartbeat, lifecycle)
```

## Quick Start

```bash
# Clone and install
git clone <repo-url> && cd polymarket-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env with your keys

# Run in paper mode (default)
python main.py
```

## Configuration

All credentials are loaded from `.env` via python-dotenv. **Never hardcode keys.**

| Variable | Description |
|---|---|
| `POLY_API_KEY` | Polymarket CLOB API key |
| `POLY_PRIVATE_KEY` | Ethereum private key (for signing + balance queries) |
| `TELEGRAM_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for alerts |
| `ALCHEMY_RPC_URL` | Alchemy RPC endpoint (Polygon PoS) |

## How It Works

### Price Monitoring
- Connects to Binance WebSocket (`wss://stream.binance.com:9443`)
- Tracks BTC/USDT and ETH/USDT ticker streams
- Auto-reconnects on disconnect with exponential backoff (max 10 retries)
- Rejects price data older than 10 seconds as stale
- **All trading pauses when price data is stale**

### Edge Detection
- Compares real-time Binance spot momentum against Polymarket implied odds
- Minimum detectable edge: 5% (logged but ignored)
- Minimum execution edge: 8% (only trades above this)
- Only monitors BTC/ETH contracts with 5-minute and 15-minute durations
- Minimum market liquidity: $50,000
- Maximum 20 markets monitored simultaneously

### Position Sizing
- Half-Kelly Criterion for every position
- Hard cap: 8% of current portfolio per trade
- Portfolio value recalculated before every trade via Alchemy RPC

### Risk Management (Non-Negotiable)

| Rule | Trigger | Action |
|---|---|---|
| Daily loss halt | P&L falls below -20% of day-start balance | **Permanent halt** + Telegram alert |
| Drawdown halt | Portfolio falls below 60% of all-time high | **Permanent halt** + Telegram alert |
| Consecutive loss pause | 5 losses in a row | 30-minute pause + Telegram alert |

All halts require a manual restart to resume trading. Risk counters reset automatically at UTC midnight.

### Monitoring
- **Telegram alerts** on every: trade entry, trade exit, error, kill switch activation
- **Hourly heartbeat**: balance, today's trade count, win rate, stale status
- **Rotating log file**: all activity with timestamps, 50 MB max, 5 backups

## Live Trading

Live mode requires all three flags **and** an interactive confirmation:

```bash
python main.py --live --confirm --i-understand-risks
# Then type YES at the prompt
```

Missing any flag exits with an error. This is intentional.

## Paper Mode

Paper mode is the default. It:
- Simulates all trades with no real orders
- Logs every trade to a local SQLite database (`paper_trades.db`)
- Tracks simulated P&L and balance snapshots
- Runs the full risk management stack (halts, pauses, etc.)

## Docker

```bash
# Build
docker build -t polymarket-bot .

# Run in paper mode
docker run --env-file .env polymarket-bot

# Run in live mode
docker run -it --env-file .env polymarket-bot --live --confirm --i-understand-risks
```

## Project Requirements

- Python 3.11+
- All dependencies pinned in `requirements.txt`
- Full async architecture (asyncio + aiohttp, zero blocking calls)
- Execution target: under 800ms from detection to order

## Disclaimer

This software is provided as-is for educational and research purposes. Trading binary contracts involves substantial risk of loss. Kill switches reduce but do not eliminate risk. You are solely responsible for any financial losses incurred. Always start in paper mode and understand the code before enabling live trading.
