"""Telegram outbound notifications, with CRITICALs that repeat until resolved."""
from __future__ import annotations

import asyncio
import time

import httpx

from . import log

logger = log.get("notify")

INFO = "INFO"
WARN = "WARN"
CRITICAL = "CRITICAL"

_PREFIX = {INFO: "ℹ️", WARN: "⚠️ WARN:", CRITICAL: "🚨 CRITICAL:"}


class Notifier:
    def __init__(self, bot_token: str, chat_id: int, critical_repeat_s: int):
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._repeat_s = critical_repeat_s
        # key -> (message, last_sent_monotonic)
        self._active_criticals: dict[str, tuple[str, float]] = {}
        self._client = httpx.AsyncClient(timeout=15)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, text: str, level: str = INFO) -> bool:
        msg = f"{_PREFIX.get(level, '')} {text}".strip()
        for attempt in range(3):
            try:
                r = await self._client.post(
                    self._url, json={"chat_id": self._chat_id, "text": msg}
                )
                if r.status_code == 200:
                    return True
                logger.warning("telegram send failed", extra={"data": {"status": r.status_code, "body": r.text[:300]}})
            except httpx.HTTPError as e:
                logger.warning(f"telegram send error: {e}")
            await asyncio.sleep(2 * (attempt + 1))
        return False

    async def info(self, text: str) -> None:
        await self.send(text, INFO)

    async def warn(self, text: str) -> None:
        logger.warning(text)
        await self.send(text, WARN)

    async def critical(self, key: str, text: str) -> None:
        """Send a CRITICAL and register it to repeat every critical_repeat_s
        until resolve(key) is called."""
        logger.error(f"CRITICAL[{key}]: {text}")
        self._active_criticals[key] = (text, time.monotonic())
        await self.send(text, CRITICAL)

    def resolve(self, key: str) -> bool:
        return self._active_criticals.pop(key, None) is not None

    def is_active(self, key: str) -> bool:
        return key in self._active_criticals

    async def repeat_tick(self) -> None:
        """Called periodically by the watchdog: re-send stale unresolved CRITICALs."""
        now = time.monotonic()
        for key, (text, last) in list(self._active_criticals.items()):
            if now - last >= self._repeat_s:
                self._active_criticals[key] = (text, now)
                await self.send(f"(still unresolved) {text}", CRITICAL)
