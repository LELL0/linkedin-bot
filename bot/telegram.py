"""Telegram commands for the owner: status, timings, pause and resume, a reply feed, and
the bot's own log lines at a chosen /verbose level.

Only the chat in TG_CHAT_ID is answered. Telegram's servers set the sender's chat id, so a
stranger who finds the bot gets no reply at all.
"""

import asyncio
import collections
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
    ("catchup", "Answer unanswered messages from the last N days: /catchup 3, /catchup stop"),
    ("verbose", "The bot's log while it runs: /verbose 0-5 (0 off, 5 everything)"),
    ("help", "List the commands"),
]
HELP = "<b>LinkedIn bot</b>\n" + "\n".join(f"/{c} - {d}" for c, d in COMMANDS)
MAX_MESSAGE = 4000
VERBOSE_LIMIT = 3500  # characters of log per message; older lines are left out
# /verbose levels: what each adds, and seconds between the messages with the log.
VERBOSE_LEVELS = {
    0: ("off: only alerts", 0),
    1: ("warnings and errors", 300),
    2: ("level 1, plus each invitation accepted and reply sent", 120),
    3: ("everything the bot logs, except the heartbeat", 60),
    4: ("level 3, plus the heartbeat each minute", 30),
    5: ("everything, also debug details and other libraries' messages", 30),
}
KEY_ACTIONS = ("Accepted invitation", "Replied", "Paused", "Resumed", "Running", "DRY RUN")
# Never forwarded: their lines hold the Telegram URL with the bot token, and each message
# sent would log one more line to send.
NEVER_FORWARDED = ("httpx", "httpcore")


def record_level(record: logging.LogRecord) -> int:
    """The lowest /verbose level at which a log record is sent (6: never)."""
    name = record.name
    if name.split(".")[0] in NEVER_FORWARDED:
        return 6
    if name != "bot" and not name.startswith("bot."):
        return 5
    if record.levelno >= logging.WARNING:
        return 1
    if record.levelno < logging.INFO:
        return 5
    message = record.getMessage()
    if message.startswith(KEY_ACTIONS):
        return 2
    return 4 if message.startswith("alive:") else 3


def every_text(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


def verbose_help(current: int) -> str:
    rows = "\n".join(f"{'&gt;' if n == current else '  '} /verbose {n} - {esc(text)}"
                     + (f" (every {every_text(every)})" if every else "")
                     for n, (text, every) in VERBOSE_LEVELS.items())
    return f"Verbose level: <b>{current}</b>\n{rows}"


def verbose_message(lines: list[str], limit: int = VERBOSE_LIMIT) -> str:
    """Log lines for one message: the newest that fit, each cut to a sane length."""
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        line = esc(line[:300])
        size += len(line) + 1
        if size > limit:
            break
        kept.append(line)
    left_out = len(lines) - len(kept)
    head = f"({left_out} earlier line(s) left out)\n" if left_out else ""
    return head + "<pre>" + "\n".join(reversed(kept)) + "</pre>"


class VerboseHandler(logging.Handler):
    """Keeps the log lines /verbose sends; TelegramControl.verbose_loop sends them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                                            "%H:%M:%S"))
        self.lines: collections.deque[tuple[int, str]] = collections.deque(maxlen=500)

    def emit(self, record: logging.LogRecord) -> None:
        level = record_level(record)
        if level <= 5:
            try:
                self.lines.append((level, self.format(record)))
            except Exception:  # noqa: BLE001 - logging must never raise
                self.handleError(record)

    def take(self, level: int) -> list[str]:
        """The kept lines the level shows; every kept line is dropped."""
        lines = []
        while self.lines:
            line_level, line = self.lines.popleft()
            if line_level <= level:
                lines.append(line)
        return lines


def apply_log_level(level: int) -> None:
    """Level 5 also needs debug records made; the console and file handlers stay at INFO."""
    logging.getLogger().setLevel(logging.DEBUG if level >= 5 else logging.INFO)
    for name in NEVER_FORWARDED:
        logging.getLogger(name).setLevel(logging.WARNING)


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
        self.verbose_log = VerboseHandler()
        if self.enabled:
            logging.getLogger().addHandler(self.verbose_log)
            apply_log_level(self.verbose)

    @property
    def verbose(self) -> int:
        return getattr(self.engine, "verbose", 0)

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
        self.verbose_task = asyncio.create_task(self.verbose_loop())
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

    async def verbose_loop(self) -> None:
        """Send the log lines the /verbose level shows, as often as the level says."""
        last_sent = time.monotonic()
        while True:
            await asyncio.sleep(VERBOSE_LEVELS[4][1])
            every = VERBOSE_LEVELS[self.verbose][1]
            if time.monotonic() - last_sent >= every - 1:
                await self.send_verbose()
                last_sent = time.monotonic()

    async def send_verbose(self) -> None:
        lines = self.verbose_log.take(self.verbose)
        if lines:
            text = verbose_message(lines)
            await self.send(text.replace(self.token, "***") if self.token else text)

    async def set_verbose(self, argument: str) -> None:
        current = self.verbose
        if not argument:
            await self.send(verbose_help(current))
            return
        word = {"off": "0", "on": "3"}.get(argument, argument)
        if not (word.isdigit() and int(word) in VERBOSE_LEVELS):
            await self.send("Use /verbose with a level from 0 to 5.\n" + verbose_help(current))
            return
        level = int(word)
        self.engine.set_verbose(level)
        apply_log_level(level)
        self.verbose_log.take(0)  # start from new lines only
        text, every = VERBOSE_LEVELS[level]
        if not level:
            await self.send(f"Verbose 0, {text}.")
            return
        await self.send(f"Verbose {level}: {text}. The log comes every {every_text(every)}. "
                        "/verbose 0 turns it off.")

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
        elif command == "/verbose":
            await self.set_verbose(argument)
        elif command == "/catchup":
            await self.catchup(argument)
        else:
            await self.send("Unknown command. /help lists them.")

    async def catchup(self, argument: str) -> None:
        engine = self.engine
        if argument == "stop":
            await self.send("Stopping the catch-up." if engine.stop_catchup()
                            else "No catch-up is going.")
            return
        try:
            days = float(argument)
        except ValueError:
            days = 0
        if not 0 < days <= 30:
            await self.send("Use /catchup with a number of days from 1 to 30, e.g. /catchup 3. "
                            "It answers unanswered messages from that time, older conversations "
                            "included. /catchup stop cancels.")
            return
        if engine.start_catchup(days, self.send):
            await self.send(f"Catch-up: looking for unanswered messages from the last {days:g} "
                            "days. The list comes next.")
        else:
            await self.send("A catch-up is already going. /catchup stop cancels it.")

    def status_text(self) -> str:
        engine = self.engine
        now = time.time()
        state = "paused" if engine.paused else engine.state
        return "\n".join([
            f"State: <b>{esc(state)}</b>",
            f"Last successful LinkedIn check: {ago(now - engine.last_ok)}",
            f"Since start: {engine.accepted} accepted, {engine.replies_sent} replies",
            f"Replies feed: {'on' if engine.replies_feed else 'off'}",
            f"Verbose: {self.verbose}",
        ])

    async def reply_sent(self, sender: str, incoming: list[str], reply: str) -> None:
        if not (self.enabled and self.engine.replies_feed):
            return
        them = "\n".join(esc(text) for text in incoming)
        await self.send(f"<b>{esc(sender or 'Someone')}</b>:\n{them}\n\n<b>You</b>: {esc(reply)}")
