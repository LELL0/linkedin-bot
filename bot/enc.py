"""Enc Chat commands for the owner: status, timings, pause and resume, a reply feed, and
the bot's own log lines at a chosen /verbose level.

The bot is an Enc Chat bot (bot_linkedin) reached through the local bot agent, in the agent's
`stream` mode: commands arrive as events on GET /v1/events and answers go out as
POST /v1/messages (bot/enc_bot_client.py). The agent already admits only the owner; this
module checks the sender again and fails closed: ENC_OWNER is required (an Enc user id, or
"@username"), and with it unset or malformed every command is ignored.

Every message is written the way a person texts, because a chat bubble on a phone is about
35 characters wide (Enc Chat's "Keep bot messages small"): the first line is an emoji, a
*bold* title and the fact that matters; each part after it is one line that starts with its
name in bold, its facts side by side joined by " · ". No headings, blank lines or dividers;
a "- " list only for items that stand alone (log lines), a "> " quote for what someone
wrote, and at most one code box, for a real table. Enc Chat 1.7.14 draws the marks; older
apps show them as typed, so each message also reads as plain text. Text from outside
(names, messages, errors) goes through marks.inline() first, so it stays on one line and
cannot add a mark of its own. The bot sends nothing to Telegram.
"""

import asyncio
import collections
import logging
import os
import re
import statistics
import threading
import time
import traceback
from datetime import datetime
from typing import TYPE_CHECKING

from . import metrics, settings
from .config import CONFIG
from .enc_bot_client import Bot, EncBotError
from .marks import fence, inline

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

DOT = " · "
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
# The "/" menu (COMMANDS) says what each command does; /help only groups them.
HELP = "\n".join([
    "🤖 *LinkedIn bot* · commands",
    "*Watch* /status · /stats · /replies on or off · /verbose 0-5",
    "*Control* /pause · /resume · /catchup 3 · /catchup stop",
    "*Settings* /settings · /set poll 5-15 · /reset all · /interval 5-15",
    "_The / menu says what each one does._",
])
CATCHUP_NAMES = 20  # names listed when a catch-up starts; the rest are counted
MAX_MESSAGE = 4000
VERBOSE_LIMIT = 3500  # characters of log per message; older lines are left out
VERBOSE_LINE = 200  # characters of one log line
STATS_LATEST = 5  # rows in the /stats table of the latest accepts and replies
TABLE_WIDTH = 31  # a code box line, so it fits a phone's chat bubble
# /verbose levels: what each sends, the short name in the list of levels, and seconds
# between the messages with the log.
VERBOSE_LEVELS = {
    0: ("off, only alerts", "off", 0),
    1: ("warnings and errors", "warnings", 300),
    2: ("warnings, accepts and replies", "+ accepts, replies", 120),
    3: ("everything but the heartbeat", "all but heartbeat", 60),
    4: ("everything with the heartbeat", "+ heartbeat", 30),
    5: ("everything, debug and libraries too", "+ debug", 30),
}
# How /status and alerts name the engine's state.
STATES = {
    "running": ("🟢", "Running"),
    "paused": ("⏸️", "Paused"),
    "needs_login": ("🔴", "Signed out"),
    "no_proxy": ("🟠", "No home tunnel"),
    "asleep": ("😴", "Asleep"),
    "starting": ("⏳", "Starting"),
}
# /settings groups; a setting in no group is listed under Other.
SETTING_GROUPS = {
    "Timing": ("poll", "check_gap", "reply_delay", "accept_gap", "catchup_gap"),
    "Hours": ("active_hours", "invite_window"),
    "Limits": ("max_replies", "max_accepts", "backoff", "max_backoff"),
    "Mode": ("dry_run", "scope"),
}
LOG_ICONS = {logging.WARNING: "⚠️ ", logging.ERROR: "🔴 ", logging.CRITICAL: "🔴 "}
KEY_ACTIONS = ("Accepted invitation", "Replied", "Paused", "Resumed", "Running", "DRY RUN")
# Never forwarded: the HTTP client logs a line per request (each AI call), noise the owner
# does not need, and a request's address is no business of the chat.
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


def plural(count: int, word: str, words: str = "") -> str:
    return f"{count} {word if count == 1 else words or word + 's'}"


def levels_line() -> str:
    return "*Levels* " + DOT.join(f"{n} {short}" for n, (_, short, _) in VERBOSE_LEVELS.items())


def verbose_help(current: int) -> str:
    text, _, every = VERBOSE_LEVELS[current]
    title = f"📜 *Verbose {current}* · {text}" + (f" · log every {every_text(every)}" if every else "")
    return "\n".join([title, levels_line(), "_/verbose 0-5 changes it._"])


def alert_text(title: str, detail: str = "", icon: str = "⚠️") -> str:
    """A health alert on one line: an emoji, the problem in bold, then what it means. Alerts
    can carry error strings, so both parts are made inert and kept on the line."""
    line = f"{icon} *{inline(title, 100) or 'Alert'}*"
    return line + (f" · {inline(detail, 1000)}" if detail.strip() else "")


def catchup_text(names: list[str], days: float, gap: tuple[float, float]) -> str:
    """The catch-up's plan: how many conversations, their names in answer order, the pace."""
    shown = [inline(name, 40) or "?" for name in names[:CATCHUP_NAMES]]
    more = len(names) - len(shown)
    return "\n".join([
        f"🔁 *Catch-up · {plural(len(names), 'conversation')}* · last {days:g} days",
        "*In order* " + DOT.join(shown) + (f" · and {more} more" if more else ""),
        f"*Pace* one every {gap[0] / 60:.0f}-{gap[1] / 60:.0f} min · /catchup stop cancels",
    ])


def verbose_message(lines: list[str], limit: int = VERBOSE_LIMIT) -> str:
    """Log lines for one message: a title, then the newest lines that fit as a list, each
    on one line, cut to a sane length and made inert."""
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        line = inline(line, VERBOSE_LINE)
        size += len(line) + 3
        if size > limit:
            break
        kept.append(line)
    left_out = len(lines) - len(kept)
    title = f"📜 *Log* · {plural(len(kept), 'line')}"
    if left_out:
        title += f" · {left_out} earlier left out"
    return "\n".join([title, *(f"- {line}" for line in reversed(kept))])


class ChatLogFormatter(logging.Formatter):
    """A log record as one short line: time, an emoji for a warning or an error, the module,
    the message, and for an exception only its last line (no traceback)."""

    def format(self, record: logging.LogRecord) -> str:
        name = record.name.removeprefix("bot.")
        icon = LOG_ICONS.get(record.levelno, "")
        text = f"{self.formatTime(record, '%H:%M:%S')} {icon}{name}: {record.getMessage()}"
        if record.exc_info and record.exc_info[1] is not None:
            last = traceback.format_exception_only(record.exc_info[1])[-1].strip()
            text += f" ({last})"
        return text


class VerboseHandler(logging.Handler):
    """Keeps the log lines /verbose sends; EncControl.verbose_loop sends them."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(ChatLogFormatter())
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
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def span(seconds: float, short: bool = False) -> str:
    """A duration the way a person says it: 4.2s, 41s, 3.1 min, 2.5 h (short: 3.1m)."""
    minute, hour = ("m", "h") if short else (" min", " h")
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}{minute}"
    return f"{seconds / 3600:.1f}{hour}"


def timing(entries: list[dict], field: str) -> str | None:
    """The median and 95th percentile of a timing field, or None without data."""
    values = [e[field] for e in entries if isinstance(e.get(field), (int, float))]
    if not values:
        return None
    return f"{span(statistics.median(values))} median, {span(metrics.percentile(values, 0.95))} p95"


def median(entries: list[dict], field: str) -> str | None:
    values = [e[field] for e in entries if isinstance(e.get(field), (int, float))]
    return span(statistics.median(values)) if values else None


def tally(entries: list[dict], field: str) -> str:
    """How often each value of a field came up, most first: "rest 10, page 2"."""
    found = collections.Counter(str(e.get(field, "unknown")) for e in entries)
    return ", ".join(f"{inline(name, 40)} {count}" for name, count in found.most_common(4))


def part(name: str, facts: list[str | None]) -> str | None:
    """One line of a message: its name in bold, then the facts that have data."""
    facts = [fact for fact in facts if fact]
    return f"*{name}* " + DOT.join(facts) if facts else None


def latest_table(entries: list[dict]) -> str:
    """The latest accepts and replies as a code box: when, which, how long, how it went."""
    rows = []
    for entry in sorted(entries, key=lambda e: e["at"])[-STATS_LATEST:]:
        when = datetime.fromtimestamp(entry["at"]).strftime("%d/%m %H:%M")
        detail = str(entry.get("via") or entry.get("outcome") or "")
        detail = detail.removeprefix("draft_").replace("rejected_", "rej_").replace("question_", "q_")
        row = f"{when} {entry['kind'][:6]:<6} {span(entry.get('total_s') or 0, short=True):>5} {detail}"
        rows.append(row[:TABLE_WIDTH].rstrip())
    return fence("\n".join(rows))


def stats_text(entries: list[dict]) -> str:
    """/stats: counts in the title, one line per part, the latest few in one small table."""
    accepts = [e for e in entries if e.get("kind") == "accept"]
    replies = [e for e in entries if e.get("kind") == "reply"]
    if not (accepts or replies):
        return "📊 *Stats* · no accepts or replies yet"
    answered = sum(e.get("messages", 1) for e in replies)
    lines = [(f"📊 *Stats* · {plural(len(accepts), 'accept')} · "
              f"{plural(len(replies), 'reply', 'replies')} to {plural(answered, 'message')}")]
    if accepts:
        lines += [
            part("Accepts", [f"invite to accept {t}" if (t := timing(accepts, "total_s")) else None,
                             f"noticed {m}" if (m := median(accepts, "detect_s")) else None,
                             f"call {m}" if (m := median(accepts, "action_s")) else None]),
            part("Accepts via", [tally(accepts, "via"), f"seen by {tally(accepts, 'trigger')}"]),
        ]
    if replies:
        steps = [("noticed", "detect_s"), ("quiet", "quiet_s"), ("draft", "draft_s"),
                 ("check", "check_s"), ("send", "send_s")]
        lines += [
            part("Replies", [f"first message to reply {t}"
                             if (t := timing(replies, "total_s")) else None,
                             f"last to reply {m}" if (m := median(replies, "since_last_s")) else None]),
            part("Reply steps", [f"{name} {m}" for name, field in steps
                                 if (m := median(replies, field))]),
            part("Outcomes", [tally(replies, "outcome"), f"models {tally(replies, 'model')}"]),
        ]
    lines.append(latest_table(accepts + replies))
    return "\n".join(line for line in lines if line)


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
        """[text] to the owner on Enc Chat. A refusal is logged, never raised."""
        text = text[:MAX_MESSAGE]
        if self.bot is not None:
            try:
                await asyncio.to_thread(self.bot.send, text)
            except EncBotError as error:
                log.warning("Enc Chat send failed: %s", error.code)

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
            await asyncio.sleep(VERBOSE_LEVELS[4][2])
            every = VERBOSE_LEVELS[self.verbose][2]
            if time.monotonic() - last_sent >= every - 1:
                await self.send_verbose()
                last_sent = time.monotonic()

    async def send_verbose(self) -> None:
        lines = self.verbose_log.take(self.verbose)
        if lines:
            await self.send(verbose_message(lines))

    async def set_verbose(self, argument: str) -> None:
        current = self.verbose
        if not argument:
            await self.send(verbose_help(current))
            return
        word = {"off": "0", "on": "3"}.get(argument, argument)
        if not (word.isdigit() and int(word) in VERBOSE_LEVELS):
            await self.send(f"❔ *Verbose {current}* stays · use a level from 0 to 5\n"
                            + levels_line())
            return
        level = int(word)
        self.engine.set_verbose(level)
        apply_log_level(level)
        self.verbose_log.take(0)  # start from new lines only
        text, _, every = VERBOSE_LEVELS[level]
        if not level:
            await self.send(f"📜 *Verbose 0* · {text}")
            return
        await self.send(f"📜 *Verbose {level}* · {text} · log every {every_text(every)} · "
                        "/verbose 0 stops it")

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
            await self.send(stats_text(metrics.load(CONFIG.metrics_path)))
        elif command == "/pause":
            engine.set_paused(True)
            await self.send("⏸️ *Paused* · no accepts or replies until /resume · messages that "
                            "come meanwhile get answered then, unless you reply first")
        elif command == "/resume":
            engine.set_paused(False)
            await self.send("▶️ *Resumed* · accepting and answering again")
        elif command == "/replies":
            if argument not in ("", "on", "off"):
                await self.send("❔ *Replies feed* · /replies on, /replies off, or /replies alone "
                                "to switch it")
                return
            on = argument == "on" if argument else not engine.replies_feed
            engine.set_replies_feed(on)
            await self.send("💬 *Replies feed on* · each message and its reply come here · "
                            "/replies off stops it" if on else "🔕 *Replies feed off*")
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
            await self.send("❔ *Unknown command* · /help lists them")

    async def catchup(self, argument: str) -> None:
        engine = self.engine
        if argument == "stop":
            await self.send("⏹️ *Stopping the catch-up*" if engine.stop_catchup()
                            else "ℹ️ *No catch-up is going*")
            return
        try:
            days = float(argument)
        except ValueError:
            days = 0
        if not 0 < days <= 30:
            await self.send("❔ *Catch-up* · /catchup 1-30 answers unanswered messages from that "
                            "many days, older conversations too · /catchup stop cancels")
            return
        if engine.start_catchup(days, self.send):
            await self.send(f"🔁 *Catch-up* · looking back {days:g} days · the list comes next")
        else:
            await self.send("⏳ *A catch-up is already going* · /catchup stop cancels it")

    @staticmethod
    def settings_text() -> str:
        """Every setting, grouped, its value side by side with the others; a changed one in
        bold with its default. /set name alone explains one."""
        grouped = [n for names in SETTING_GROUPS.values() for n in names]
        groups = {**SETTING_GROUPS,
                  "Other": tuple(n for n in settings.TUNABLES if n not in grouped)}
        changed = 0
        lines = []
        for group, names in groups.items():
            facts = []
            for name in names:
                if name not in settings.TUNABLES:
                    continue
                default = settings.DEFAULTS[name]
                if getattr(settings.CONFIG, settings.TUNABLES[name].field) != default:
                    changed += 1
                    facts.append(f"{name} *{settings.show(name)}* (default "
                                 f"{settings.show(name, default)})")
                else:
                    facts.append(f"{name} {settings.show(name)}")
            if facts:
                lines.append(f"*{group}* " + DOT.join(facts))
        title = "⚙️ *Settings* · " + (f"{changed} changed" if changed else "all default")
        how = "_/set name value changes one, /set name explains it, /reset name or all undoes._"
        return "\n".join([title, *lines, how])

    async def set_setting(self, name: str, value: str) -> None:
        name = name.replace("-", "_")
        if name not in settings.TUNABLES:
            await self.send("❔ *Set* · /set name value · names: " + ", ".join(settings.TUNABLES)
                            + " · /settings shows them")
            return
        tunable = settings.TUNABLES[name]
        if not value:
            await self.send(f"⚙️ *{name}* {settings.show(name)} · {tunable.help} · "
                            f"/set {name} value changes it")
            return
        try:
            parsed = settings.parse(name, value)
            self.engine.change_setting(name, parsed)
        except ValueError as error:  # it may quote what the owner typed
            await self.send(f"❌ *Not changed* · {inline(error)}")
            return
        note = ""
        if name == "scope" and parsed == "all":
            note = ("\n⚠️ The bot may now answer in every one-to-one conversation, also ones "
                    "you started yourself.")
        if name == "dry_run" and parsed:
            note = "\n⚠️ Nothing is sent or accepted until /set dry_run off."
        await self.send(f"✅ *{name}* {settings.show(name)} · kept across restarts · /reset {name} "
                        f"goes back to {settings.show(name, settings.DEFAULTS[name])}" + note)

    async def reset_setting(self, name: str) -> None:
        names = list(settings.TUNABLES) if name == "all" else [name.replace("-", "_")]
        if not name or names[0] not in settings.TUNABLES:
            await self.send("❔ *Reset* · /reset name or /reset all · /settings lists the names")
            return
        changed = [n for n in names
                   if getattr(settings.CONFIG, settings.TUNABLES[n].field) != settings.DEFAULTS[n]]
        for n in names:
            self.engine.change_setting(n, None)
        if name == "all":
            await self.send("↩️ *All settings back to default* · "
                            + (f"{', '.join(changed)} had changed" if changed
                               else "none had changed"))
        else:
            await self.send(f"↩️ *{name}* {settings.show(name)} · the default")

    def status_text(self) -> str:
        engine = self.engine
        now = time.time()
        state = "paused" if engine.paused else engine.state
        icon, name = STATES.get(state, ("⚪", inline(state, 40) or "Unknown"))
        checked = f"last check {ago(now - engine.last_ok)}" if engine.last_ok else "no check yet"
        return "\n".join([
            f"{icon} *{name}* · {checked}",
            (f"*Since start* {engine.accepted} accepted · "
             f"{plural(engine.replies_sent, 'reply', 'replies')}"),
            f"*Inbox* every {settings.show('poll')} · active {settings.show('active_hours')}"
            + (" · *dry run*" if CONFIG.dry_run else ""),
            f"*Feeds* replies {'on' if engine.replies_feed else 'off'} · verbose {self.verbose}",
        ])

    async def reply_sent(self, sender: str, incoming: list[str], reply: str) -> None:
        """Each answered conversation: who, each message as one quoted line, and the reply on
        one line. Outside text stays inert and on its line (marks.inline)."""
        if not (self.enabled and self.engine.replies_feed):
            return
        who = inline(sender, 60) or "Someone"
        said = [f"> {line}" for message in incoming if (line := inline(message, 500))]
        await self.send("\n".join([f"💬 *{who}*", *(said or ["> …"]),
                                   f"↩️ *Reply* {inline(reply, 1500) or '…'}"]))
