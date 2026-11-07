"""Telegram commands for the owner: status, timings, pause and resume, and a reply feed.

Only the chat in TG_CHAT_ID is answered. Telegram's servers set the sender's chat id, so a
stranger who finds the bot gets no reply at all.
"""

import asyncio
import html
import logging
import os
import time
from typing import TYPE_CHECKING

import httpx

from . import metrics
from .config import CONFIG

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

COMMANDS = [
    ("status", "Running, signed in, last check"),
    ("stats", "Accepts and replies, and how fast"),
    ("pause", "Stop accepting and answering until /resume"),
    ("resume", "Start accepting and answering again"),
    ("replies", "Each message and the reply sent: /replies on or off"),
    ("help", "List the commands"),
]
HELP = "<b>LinkedIn bot</b>\n" + "\n".join(f"/{c} - {d}" for c, d in COMMANDS)
MAX_MESSAGE = 4000


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def ago(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds}s ago" if seconds < 120 else f"{seconds // 60} min ago"


class TelegramControl:
    def __init__(self, engine: "Engine") -> None:
        self.engine = engine
        self.token = os.environ.get("TG_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TG_CHAT_ID", "")
        self.client = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{self.token}")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def call(self, method: str, http_timeout: float = 20, **params) -> object:
        response = await self.client.post(f"/{method}", json=params, timeout=http_timeout)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram error"))
        return data["result"]

    async def send(self, text: str) -> None:
        try:
            await self.call("sendMessage", chat_id=self.chat_id, text=text[:MAX_MESSAGE],
                            parse_mode="HTML", disable_web_page_preview=True)
        except (httpx.HTTPError, RuntimeError, ValueError) as error:
            log.warning("Telegram send failed: %s", type(error).__name__)

    async def loop(self) -> None:
        if not self.enabled:
            return
        try:
            await self.call("setMyCommands",
                            commands=[{"command": c, "description": d} for c, d in COMMANDS])
        except (httpx.HTTPError, RuntimeError, ValueError) as error:
            log.warning("setMyCommands failed: %s", type(error).__name__)
        offset = None
        while True:
            try:
                updates = await self.call("getUpdates", http_timeout=70, offset=offset, timeout=50)
            except (httpx.HTTPError, RuntimeError, ValueError) as error:
                log.warning("getUpdates failed: %s", type(error).__name__)
                await asyncio.sleep(15)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                if str(message.get("chat", {}).get("id")) != self.chat_id:
                    continue  # Only the owner's chat; strangers get nothing.
                try:
                    await self.handle((message.get("text") or "").strip())
                except Exception:  # noqa: BLE001 - one bad command must not stop the loop
                    log.exception("Telegram command failed")

    async def handle(self, text: str) -> None:
        if not text.startswith("/"):
            return
        words = text.split()
        command = words[0].split("@")[0].lower()
        argument = words[1].lower() if len(words) > 1 else ""
        engine = self.engine
        if command in ("/start", "/help"):
            await self.send(HELP)
        elif command == "/status":
            await self.send(self.status_text())
        elif command == "/stats":
            await self.send(f"<pre>{esc(metrics.report(metrics.load(CONFIG.metrics_path)))}</pre>")
        elif command == "/pause":
            engine.set_paused(True)
            await self.send("Paused: no accepting or answering until /resume. Messages that arrive "
                            "meanwhile are answered after /resume unless you reply yourself.")
        elif command == "/resume":
            engine.set_paused(False)
            await self.send("Resumed: accepting and answering again.")
        elif command == "/replies":
            if argument not in ("", "on", "off"):
                await self.send("Use /replies on, /replies off, or /replies alone to switch it.")
                return
            on = argument == "on" if argument else not engine.replies_feed
            engine.set_replies_feed(on)
            await self.send("Replies on: each message and the reply sent come here. /replies off "
                            "stops it." if on else "Replies off.")
        else:
            await self.send("Unknown command. /help lists them.")

    def status_text(self) -> str:
        engine = self.engine
        now = time.time()
        state = "paused" if engine.paused else engine.state
        return "\n".join([
            f"State: <b>{esc(state)}</b>",
            f"Last successful LinkedIn check: {ago(now - engine.last_ok)}",
            f"Since start: {engine.accepted} accepted, {engine.replies_sent} replies",
            f"Replies feed: {'on' if engine.replies_feed else 'off'}",
        ])

    async def reply_sent(self, sender: str, incoming: list[str], reply: str) -> None:
        if not (self.enabled and self.engine.replies_feed):
            return
        them = "\n".join(esc(text) for text in incoming)
        await self.send(f"<b>{esc(sender or 'Someone')}</b>:\n{them}\n\n<b>You</b>: {esc(reply)}")
