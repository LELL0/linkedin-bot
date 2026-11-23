"""Enc Chat commands for the owner: status, timings, pause and resume, a reply feed, and
the bot's own log lines at a chosen /verbose level.

The bot is an Enc Chat bot (bot_linkedin) reached through the local bot agent, in the agent's
`stream` mode: commands arrive as events on GET /v1/events and answers go out as
POST /v1/messages (bot/enc_bot_client.py). The agent already admits only the owner; this
module checks the sender again and fails closed: ENC_OWNER is required (an Enc user id, or
"@username"), and with it unset or malformed every command is ignored.

Texts use Enc Chat marks: *bold*, _italic_, ~strike~, `code`, ```block```, "> " quote, "- " bullet.
"""

import asyncio
import collections
import logging
import os
import re
import threading
import time
from typing import TYPE_CHECKING

from . import metrics, settings, tg_mirror
from .config import CONFIG
from .enc_bot_client import Bot, EncBotError

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
    ("settings", "Timings, limits and other settings, and how to change them"),
    ("set", "Change a setting: /set poll 5-15, /set max_replies 30"),
    ("reset", "Back to the default: /reset poll, /reset all"),
    ("interval", "Random wait between inbox checks, in minutes: /interval 5-15"),
    ("help", "List the commands"),
]
HELP = "*LinkedIn bot*\n" + "\n".join(f"- /{c} - {d}" for c, d in COMMANDS)
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
# Never forwarded: their lines hold the Telegram URL with the bot token while the trial mirror
# runs (TELEGRAM-MIRROR: delete at M9), and each message sent would log one more line to send.
NEVER_FORWARDED = ("httpx", "httpcore")
OWNER_ID = re.compile(r"[0-9A-Za-z]{26}")  # an Enc user id (ULID)
OWNER_NAME = re.compile(r"@[A-Za-z0-9_]{1,30}")
SEEN_EVENTS = 500  # event ids kept to drop a repeat: the agent delivers at least once


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


def fence(text: object) -> str:
    """Text as a code block, so marks inside it (a message's own asterisks) stay as written."""
    return "```\n" + str(text).replace("```", "'''").strip("\n") + "\n```"


def verbose_help(current: int) -> str:
    rows = []
    for n, (text, every) in VERBOSE_LEVELS.items():
        row = f"/verbose {n} - {text}" + (f" (every {every_text(every)})" if every else "")
        rows.append(f"- *{row}*" if n == current else f"- {row}")
    return f"Verbose level: *{current}*\n" + "\n".join(rows)


def verbose_message(lines: list[str], limit: int = VERBOSE_LIMIT) -> str:
    """Log lines for one message: the newest that fit, each cut to a sane length."""
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        line = line[:300]
        size += len(line) + 1
        if size > limit:
            break
        kept.append(line)
    left_out = len(lines) - len(kept)
    head = f"({left_out} earlier line(s) left out)\n" if left_out else ""
    return head + fence("\n".join(reversed(kept)))


class VerboseHandler(logging.Handler):
    """Keeps the log lines /verbose sends; EncControl.verbose_loop sends them."""

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


def ago(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds}s ago" if seconds < 120 else f"{seconds // 60} min ago"


def client_from_env() -> Bot | None:
    """The Enc Chat bot client, or None (Enc off) when no secret is set.

    ENC_BOT_SECRET, or ENC_BOT_SECRET_FILE pointing at a file; ENC_BOT_URL defaults to the
    agent on the docker network enc-bots."""
    secret = os.environ.get("ENC_BOT_SECRET", "").strip()
    path = os.environ.get("ENC_BOT_SECRET_FILE", "").strip()
    if not secret and path:
        try:
            with open(path, encoding="utf-8") as handle:
                secret = handle.read().strip()
        except OSError:
            log.warning("Cannot read ENC_BOT_SECRET_FILE")
    if not secret:
        return None
    return Bot(os.environ.get("ENC_BOT_URL", "http://enc-bot-agent:8787").strip(), secret)


def notify(text: str) -> bool:
    """Send [text] to the owner from any thread (health alerts). True when the agent took it;
    a message it queued while Enc Chat is unreachable counts. Never raises, logs a code only."""
    bot = client_from_env()
    if bot is None:
        return False
    try:
        bot.send(text[:MAX_MESSAGE])
    except EncBotError as error:
        log.warning("Enc Chat alert failed: %s", error.code)
        return False
    return True


class EncControl:
    def __init__(self, engine: "Engine") -> None:
        self.engine = engine
        self.bot = client_from_env()
        self.owner = self.owner_from_env()
        self.seen: collections.deque[str] = collections.deque(maxlen=SEEN_EVENTS)
        self.verbose_log = VerboseHandler()
        if self.enabled:
            if not self.owner:
                log.warning("ENC_OWNER_MISSING: ENC_OWNER is unset or not an Enc user id or "
                            "@username; every command is ignored")
            logging.getLogger().addHandler(self.verbose_log)
            apply_log_level(self.verbose)

    @property
    def verbose(self) -> int:
        return getattr(self.engine, "verbose", 0)

    @property
    def enabled(self) -> bool:
        return self.bot is not None

    async def send(self, text: str) -> None:
        text = text[:MAX_MESSAGE]
        if self.bot is not None:
            try:
                await asyncio.to_thread(self.bot.send, text)
            except EncBotError as error:
                log.warning("Enc Chat send failed: %s", error.code)
        if tg_mirror.enabled():  # TELEGRAM-MIRROR: delete at M9
            await asyncio.to_thread(tg_mirror.send_sync, text)  # TELEGRAM-MIRROR: delete at M9

    async def register_commands(self) -> None:
        """Fill the app's "/" menu; try again until the agent answers."""
        commands = [{"command": c, "description": d} for c, d in COMMANDS]
        while True:
            try:
                await asyncio.to_thread(self.bot.set_commands, commands)
                return
            except EncBotError as error:
                log.warning("Setting the commands failed: %s", error.code)
            await asyncio.sleep(30)

    def pump_events(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        """Thread body: the agent's event stream into [queue]. The stream reconnects by itself
        and resumes after the last event; only a wrong secret or a webhook bot ends it."""
        while True:
            try:
                for event in self.bot.events():
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except EncBotError as error:
                log.warning("Enc Chat events stopped: %s", error.code)
            except Exception as error:  # noqa: BLE001 - the thread must keep listening
                log.warning("Enc Chat events failed: %s", type(error).__name__)
            time.sleep(30)

    async def loop(self) -> None:
        if not self.enabled:
            return
        self.verbose_task = asyncio.create_task(self.verbose_loop())
        self.commands_task = asyncio.create_task(self.register_commands())
        queue: asyncio.Queue = asyncio.Queue()
        threading.Thread(target=self.pump_events, args=(asyncio.get_running_loop(), queue),
                         name="enc-events", daemon=True).start()
        while True:
            event = await queue.get()
            try:
                await self.on_event(event)
            except Exception:  # noqa: BLE001 - one bad command must not stop the loop
                log.exception("Enc Chat command failed")

    @staticmethod
    def owner_from_env() -> tuple[str, str] | None:
        """ENC_OWNER as (kind, value): ("id", the Enc user id) compared only to the sender's id,
        or ("name", "@username" lowercased) compared only to the sender's username."""
        value = os.environ.get("ENC_OWNER", "").strip()
        if OWNER_ID.fullmatch(value):
            return ("id", value)
        if OWNER_NAME.fullmatch(value):
            return ("name", value.lower())
        return None

    def is_owner(self, sender: dict) -> bool:
        """The program's own check, on top of the agent's. Fails closed: without a valid
        ENC_OWNER nobody is the owner."""
        if not self.owner:
            return False
        kind, wanted = self.owner
        if kind == "id":
            return str(sender.get("id") or "") == wanted
        username = str(sender.get("username") or "").strip()
        return bool(username) and "@" + username.lstrip("@").lower() == wanted

    async def on_event(self, event: dict) -> None:
        """One agent event: a command, or /start. Repeats and strangers are dropped silently."""
        event_id = str(event.get("id") or "")
        if event_id:
            if event_id in self.seen:
                return
            self.seen.append(event_id)
        kind = event.get("type")
        if kind not in ("command", "started"):
            return
        if not self.is_owner(event.get("from") or {}):
            return  # Only the owner; strangers get nothing.
        if kind == "started":
            await self.send(HELP)
            return
        args = event.get("args") or ""
        if isinstance(args, (list, tuple)):
            args = " ".join(str(a) for a in args)
        await self.handle(f"/{event.get('command', '')} {args}".strip())

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
            # TELEGRAM-MIRROR: delete at M9 (the token is only ever Telegram's)
            token = os.environ.get("TG_BOT_TOKEN", "")
            await self.send(text.replace(token, "***") if token else text)

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
            await self.send(fence(metrics.report(metrics.load(CONFIG.metrics_path))))
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
        elif command == "/settings":
            await self.send(self.settings_text())
        elif command == "/set":
            await self.set_setting(words[1].lower() if len(words) > 1 else "",
                                   " ".join(words[2:]))
        elif command == "/interval":
            await self.set_setting("poll", " ".join(words[1:]))
        elif command == "/reset":
            await self.reset_setting(argument)
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

    def settings_text(self) -> str:
        return ("*Settings* (a star marks one changed from the default)\n"
                f"{fence(settings.listing())}\n"
                "Change one: /set name value, e.g. /set poll 5-15 or /set dry_run on. "
                "Ranges are random between the two numbers. /reset name or /reset all.")

    async def set_setting(self, name: str, value: str) -> None:
        name = name.replace("-", "_")
        if name not in settings.TUNABLES:
            await self.send("Use /set name value. Names: "
                            + ", ".join(settings.TUNABLES) + ". /settings shows them all.")
            return
        tunable = settings.TUNABLES[name]
        if not value:
            await self.send(f"{name} = *{settings.show(name)}*\n{tunable.help}\n"
                            f"Change it with /set {name} value.")
            return
        try:
            parsed = settings.parse(name, value)
            self.engine.change_setting(name, parsed)
        except ValueError as error:
            await self.send(f"Not changed: {error}.")
            return
        note = ""
        if name == "scope" and parsed == "all":
            note = ("\nThe bot may now answer in every one-to-one conversation, also ones "
                    "you started yourself.")
        if name == "dry_run" and parsed:
            note = "\nNothing is sent or accepted until /set dry_run off."
        await self.send(f"{name} = *{settings.show(name)}*. Kept across restarts; "
                        f"/reset {name} goes back to {settings.show(name, settings.DEFAULTS[name])}."
                        + note)

    async def reset_setting(self, name: str) -> None:
        names = list(settings.TUNABLES) if name == "all" else [name.replace("-", "_")]
        if not name or names[0] not in settings.TUNABLES:
            await self.send("Use /reset name or /reset all. /settings lists the names.")
            return
        changed = [n for n in names
                   if getattr(settings.CONFIG, settings.TUNABLES[n].field) != settings.DEFAULTS[n]]
        for n in names:
            self.engine.change_setting(n, None)
        if name == "all":
            await self.send("All settings back to their defaults"
                            + (f" ({', '.join(changed)} changed)." if changed else "; none had "
                               "been changed."))
        else:
            await self.send(f"{name} = *{settings.show(name)}* (default).")

    def status_text(self) -> str:
        engine = self.engine
        now = time.time()
        state = "paused" if engine.paused else engine.state
        return "\n".join([
            f"State: *{state}*",
            f"Last successful LinkedIn check: {ago(now - engine.last_ok)}",
            f"Since start: {engine.accepted} accepted, {engine.replies_sent} replies",
            f"Replies feed: {'on' if engine.replies_feed else 'off'}",
            f"Verbose: {self.verbose}",
            f"Inbox check every {settings.show('poll')}, active {settings.show('active_hours')}"
            + (", *dry run*" if CONFIG.dry_run else ""),
        ])

    async def reply_sent(self, sender: str, incoming: list[str], reply: str) -> None:
        if not (self.enabled and self.engine.replies_feed):
            return
        who = (sender or "Someone").replace("*", "")
        await self.send(f"*{who}*:\n{fence(chr(10).join(incoming))}\n*You*:\n{fence(reply)}")
