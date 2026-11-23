"""TELEGRAM-MIRROR: delete at M9.

Enc Chat takes every command; for the three day trial each message the bot sends to the owner
is also sent to Telegram while TG_BOT_TOKEN and TG_CHAT_ID are still set. Remove the two
variables to end the trial, then delete this file and its three callers (bot/enc.py,
bot/health.py and the httpx log filter in bot/enc.py).
"""

import logging
import os

import httpx

log = logging.getLogger(__name__)

MAX_MESSAGE = 4000


def enabled() -> bool:
    return bool(os.environ.get("TG_BOT_TOKEN") and os.environ.get("TG_CHAT_ID"))


def send_sync(text: str) -> None:
    """Plain text, as written (Enc Chat marks show as typed). Never raises."""
    token, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not (token and chat):
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[:MAX_MESSAGE], "disable_web_page_preview": True},
            timeout=10,
        )
    except httpx.HTTPError as error:
        log.warning("Telegram mirror failed: %s", type(error).__name__)
