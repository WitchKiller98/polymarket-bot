"""Async Telegram notification helper."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from bot.config import Credentials

log = logging.getLogger("polybot.telegram")

# Telegram send-message endpoint template
_TG_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Rate-limit: Telegram allows ~30 msgs/sec to same chat, but we throttle to
# avoid bursts during volatile periods.
_MIN_INTERVAL = 0.5  # seconds between messages


class TelegramNotifier:
    """Fire-and-forget Telegram alerts via aiohttp."""

    def __init__(self, creds: Credentials) -> None:
        self._token = creds.telegram_token
        self._chat_id = creds.telegram_chat_id
        self._url = _TG_URL.format(token=self._token)
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._last_send: float = 0.0

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def send(self, text: str) -> None:
        """Send *text* to the configured Telegram chat (Markdown V2 safe)."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._MIN_INTERVAL - (now - self._last_send)
            if wait > 0:
                await asyncio.sleep(wait)

            session = await self._ensure_session()
            payload = {
                "chat_id": self._chat_id,
                "text": text[:4096],  # Telegram hard limit
                "parse_mode": "HTML",
            }
            try:
                async with session.post(self._url, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning("Telegram API %s: %s", resp.status, body)
            except Exception:
                log.exception("Failed to send Telegram message")
            finally:
                self._last_send = asyncio.get_event_loop().time()

    async def alert(self, title: str, body: str) -> None:
        """Convenience wrapper that formats title + body."""
        msg = f"<b>{title}</b>\n{body}"
        await self.send(msg)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
