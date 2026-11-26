"""Enc Chat commands for the owner: status, timings, pause and resume, a reply feed, and
the bot's own log lines at a chosen /verbose level.

The bot is an Enc Chat bot (bot_linkedin) reached through the local bot agent, in the agent's
`stream` mode: commands arrive as events on GET /v1/events and answers go out as
POST /v1/messages (bot/enc_bot_client.py). The agent already admits only the owner; this
module checks the sender again and fails closed: ENC_OWNER is required (an Enc user id, or
"@username"), and with it unset or malformed every command is ignored.

Every message follows the Enc Chat bot style (v2, 2026-11-25), because a chat bubble on a
phone is about 35 characters wide:
- The first line is the title: a state emoji (TITLE_EMOJI), then `*LinkedIn · state*` in
  bold, then at most one fact (title()). A message that lists a problem never starts with 🟢.
- Each part after it is one line that starts with its name in bold, its facts side by side
  joined by " · ", at most three. No line is longer than LINE_MAX (70, two phone lines).
- No headings, blank lines or dividers; a "- " list only for log lines, a "> " quote for
  what someone wrote.
- A table where three or more things share the same facts (/stats: accepts and replies a
  day, how long each takes): a code box laid out by marks.table(), a header in capitals, at
  most 32 characters a line, at most two boxes a message. Everything else stays in lines.
- Numbers read the same everywhere: durations 4.2s, 42s, 4m 12s, 2h 5m, 3d 4h; ages 7h ago;
  clock times 18:40 on the owner's clock (owner_time(), Beirut).
- A refusal or a bad argument is one line that says what to type (`Use /verbose 0-5`).
- /help and the "/" menu come from the same COMMANDS list, so they always match.
- Few messages, Details for the rest (2026-11-26): a message shows what to know at a glance
  and carries the rest as `details` (SDK 0.6.0): a Details button under it opens the long
  text in the same message. The reply feed goes out at most every FEED_EVERY_S as one
  message, the log at most every few minutes (VERBOSE_LEVELS), its newest lines first.
Enc Chat 1.7.14 draws the marks; older apps show them as typed, so each message also reads as
plain text. Text from outside (names, messages, errors) goes through marks.inline() first, so
it stays on its line and cannot add a mark of its own. The bot sends nothing to Telegram.
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
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from . import metrics, settings
from .config import CONFIG
from .enc_bot_client import Bot, EncBotError
from .marks import inline, start_safe, table

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

SUBJECT = "LinkedIn"  # the name in every title
DOT = " · "
LINE_MAX = 70  # characters of one line: two lines of a phone's chat bubble
# The emoji a title may start with, and what each one says.
TITLE_EMOJI = {
    "🟢": "all well",
    "🟡": "needs a look",
    "🔴": "down or broken",
    "✅": "done",
    "❌": "failed",
    "🆕": "new",
    "ℹ️": "information",
    "⏳": "in progress",
}


def title(emoji: str, state: str, fact: str = "") -> str:
    """The first line of a message: the state emoji, *LinkedIn · state* in bold (one to four
    words), then at most one fact."""
    if emoji not in TITLE_EMOJI:
        raise ValueError(f"not a title emoji: {emoji}")
    return f"{emoji} *{SUBJECT} · {state}*" + (DOT + fact if fact else "")


# Each command: its name, the argument /help shows, and what it does. /help and the app's
# "/" menu list exactly these, most used first.
COMMANDS = [
    ("status", "", "Is it running, last inbox check"),
    ("pause", "", "Stop accepting and answering"),
    ("resume", "", "Accept and answer again"),
    ("stats", "", "Accepts, replies and how fast"),
    ("replies", "on/off", "Show me each reply it sends"),
    ("catchup", "1-30", "Answer missed messages, days back"),
    ("verbose", "0-5", "Send me its log, 0 is off"),
    ("settings", "", "List timings and limits"),
    ("set", "name value", "Change one setting"),
    ("reset", "name/all", "Put settings back to default"),
    ("help", "", "This list"),
]
# Names that still work but are not listed: /start opens the chat, /interval is /set poll.
HIDDEN = ("start", "interval")


def help_line(name: str, argument: str, description: str) -> str:
    return f"/{name}{' ' + argument if argument else ''}{DOT}{description}"


HELP = "\n".join([title("ℹ️", "help", "I accept invites and answer messages for you"),
                  *(help_line(*command) for command in COMMANDS)])
MAX_MESSAGE = 4000
VERBOSE_LIMIT = 3800  # characters of log in Details (the agent cuts at 4000); older lines left out
VERBOSE_SHORT = 5  # newest log lines in the message itself; the rest behind Details
FEED_EVERY_S = 600  # the reply feed: at most one message this often, every reply since in it
FEED_SHORT = 6  # people named in one feed message; the rest behind Details
QUOTE_LINES = 3  # lines of one incoming message in the replies feed
QUOTES = 8  # lines of incoming messages in one feed message
REPLY_LINES = 4  # lines of the reply in the replies feed
# /verbose levels: what each sends, the short name in the list of levels, and seconds
# between the messages with the log.
VERBOSE_LEVELS = {
    0: ("off, only alerts", "off", 0),
    1: ("warnings and errors", "warnings", 600),
    2: ("warnings, accepts and replies", "+ accepts, replies", 600),
    3: ("everything but the heartbeat", "all but heartbeat", 300),
    4: ("everything with the heartbeat", "+ heartbeat", 120),
    5: ("everything, debug and libraries too", "+ debug", 120),
}
# How /status names the engine's state, and what the owner can do about it.
STATES = {
    "running": ("🟢", "running"),
    "paused": ("🟡", "paused"),
    "needs_login": ("🔴", "sign-in needed"),
    "no_proxy": ("🔴", "home tunnel down"),
    "asleep": ("ℹ️", "asleep"),
    "resting": ("🟢", "running"),  # Chrome closed until the next check
    "starting": ("⏳", "starting"),
}
NEXT_STEP = {
    "paused": "/resume starts it again",
    "needs_login": "sign in again over VNC",
    "no_proxy": "start it so LinkedIn can open",
}
# /settings: one line each, at most three settings a line; a setting in none is under Other.
SETTING_LINES = {
    "Inbox": ("checks", "watch", "poll"),
    "Replies": ("reply_delay", "max_replies", "catchup_gap"),
    "Invites": ("invites", "accept_gap", "max_accepts"),
    "Back-off": ("backoff", "max_backoff", "check_gap"),
    "Mode": ("active_hours", "dry_run", "scope"),
}
LOG_ICONS = {logging.WARNING: "🟡", logging.ERROR: "🔴", logging.CRITICAL: "🔴"}
KEY_ACTIONS = ("Accepted invitation", "Replied", "Paused", "Resumed", "Running", "DRY RUN")
# Never forwarded: the HTTP client logs a line per request (each AI call), noise the owner
# does not need, and a request's address is no business of the chat.
NEVER_FORWARDED = ("httpx", "httpcore")
OWNER_ID = re.compile(r"[0-9A-Za-z]{26}")  # an Enc user id (ULID)
OWNER_NAME = re.compile(r"@[A-Za-z0-9_]{1,30}")
SEEN_EVENTS = 500  # event ids kept to drop a repeat: the agent delivers at least once
# The owner's clock (Beirut): UTC+2, and UTC+3 in summer time.
WINTER = timezone(timedelta(hours=2))
SUMMER = timezone(timedelta(hours=3))


def last_sunday(year: int, month: int) -> date:
    """The last Sunday of [month] (March or October)."""
    last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() + 1) % 7)


def owner_time(at: float) -> datetime:
    """[at] (a Unix time) on the owner's clock in Beirut: summer time (UTC+3) from the last
    Sunday of March 00:00 to the last Sunday of October 00:00 local, else UTC+2. The
    container's own TZ is another zone, so local time is not the owner's."""
    utc = datetime.fromtimestamp(at, timezone.utc)
    starts = datetime.combine(last_sunday(utc.year, 3), datetime.min.time(), WINTER)
    ends = datetime.combine(last_sunday(utc.year, 10), datetime.min.time(), SUMMER)
    return utc.astimezone(SUMMER if starts <= utc < ends else WINTER)


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


def duration(seconds: float) -> str:
    """A duration the way the owner reads it: 4.2s, 42s, 4m 12s, 2h 5m, 3d 4h."""
    if seconds < 10:
        return f"{max(seconds, 0):.1f}s"
    seconds = round(seconds)
    for size, unit, small_size, small in ((86400, "d", 3600, "h"), (3600, "h", 60, "m"),
                                          (60, "m", 1, "s")):
        if seconds >= size:
            big, rest = divmod(seconds, size)
            rest //= small_size
            return f"{big}{unit}" + (f" {rest}{small}" if rest else "")
    return f"{seconds}s"


def ago(seconds: float) -> str:
    """How long ago, in one unit: 45s ago, 5m ago, 7h ago, 3d ago."""
    seconds = max(int(seconds), 0)
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def plural(count: float, word: str, words: str = "") -> str:
    return f"{count:g} {word if count == 1 else words or word + 's'}"


def days_text(days: float) -> str:
    return plural(days, "day")


def minutes_range(low_s: float, high_s: float) -> str:
    """A range of seconds in minutes: 1.5-5m, or 2m when both ends are the same."""
    low, high = (f"{round(v / 60, 1):g}" for v in (low_s, high_s))
    return f"{low}m" if low == high else f"{low}-{high}m"


def part(name: str, facts: list[str | None]) -> str | None:
    """One line of a message: its name in bold, then the facts that have data."""
    facts = [fact for fact in facts if fact]
    return f"*{name}* " + DOT.join(facts) if facts else None


def wrap(text: str, width: int, most: int, first: int = 0) -> list[str]:
    """One line of [text] cut into lines of at most [width] characters ([first] for the
    first one) at spaces, at most [most] lines; the last ends in … when text is left out."""
    out: list[str] = []
    line = ""
    for word in text.split():
        room = first if first and not out else width
        candidate = f"{line} {word}" if line else word
        if len(candidate) <= room:
            line = candidate
            continue
        if line:
            out.append(line)
            room = width
        while len(word) > room:
            out.append(word[:room])
            word = word[room:]
            room = width
        line = word
    if line:
        out.append(line)
    if len(out) > most:
        out = out[:most]
        room = first if first and most == 1 else width
        out[-1] = out[-1][:room - 1].rstrip() + "…"
    return out


def levels_lines() -> list[str]:
    levels = [f"{n} {short}" for n, (_, short, _) in VERBOSE_LEVELS.items()]
    return ["*Levels* " + DOT.join(levels[:3]), "*More levels* " + DOT.join(levels[3:])]


def level_title(emoji: str, level: int) -> str:
    text = VERBOSE_LEVELS[level][0]
    return title(emoji, "log off", "only alerts come here") if not level else \
        title(emoji, f"log level {level}", text)


def verbose_help(current: int) -> str:
    every = VERBOSE_LEVELS[current][2]
    how = "/verbose 0-5 changes it" + (f" · sent every {duration(every)}" if every else "")
    return "\n".join([level_title("ℹ️", current), *levels_lines(), how])


def alert_text(state: str, detail: str = "", emoji: str = "🟡") -> str:
    """A health alert on one line: the state emoji, *LinkedIn · the problem*, then what it
    means or what to do, cut to fit the line. Alerts can carry error strings, so both parts
    are made inert and kept on the line."""
    head = title(emoji if emoji in TITLE_EMOJI else "🟡", inline(state, 40) or "alert")
    if not detail.strip():
        return head
    return head + DOT + inline(detail, max(LINE_MAX - len(head) - len(DOT), 20))


def catchup_text(names: list[str], days: float, gap: tuple[float, float]) -> str:
    """The catch-up's plan: how many conversations, the names that fit one line in answer
    order, the pace."""
    shown: list[str] = []
    for number, name in enumerate(names):
        name = inline(name, 30) or "?"
        left = len(names) - number - 1
        line = "*In order* " + ", ".join([*shown, name]) + (f" and {left} more" if left else "")
        if len(line) > LINE_MAX:
            break
        shown.append(name)
    more = len(names) - len(shown)
    return "\n".join([
        title("⏳", f"catch-up, {len(names)} to answer", f"last {days_text(days)}"),
        "*In order* " + ", ".join(shown) + (f" and {more} more" if more else ""),
        f"*Pace* one every {minutes_range(*gap)} · /catchup stop ends it",
    ])


def catchup_limit_text(answered: int, left: int) -> str:
    return "\n".join([title("🟡", "catch-up stopped", "daily reply limit reached"),
                      f"*So far* {answered} answered · {left} left · /catchup again tomorrow"])


def _log_head(lines: list[str], fact: str) -> str:
    problems = sum(line.startswith(tuple(LOG_ICONS.values())) for line in lines)
    return (title("🟡", f"log, {plural(problems, 'problem')}", fact) if problems
            else title("ℹ️", "log", fact))


def verbose_message(lines: list[str], short: int = VERBOSE_SHORT) -> str:
    """Log lines for one message: a title (🟡 when a line is a warning or an error), then
    the newest [short] lines as a list, each on one line, cut to fit and made inert. The
    rest is in verbose_details()."""
    kept = [inline(line, LINE_MAX - 2) for line in lines[-short:]]
    more = len(lines) - len(kept)
    fact = plural(len(lines), "line") + (f" · {more} more in Details" if more else "")
    return "\n".join([_log_head(lines, fact), *(f"- {line}" for line in kept)])


def verbose_details(lines: list[str], limit: int = VERBOSE_LIMIT) -> str | None:
    """Every log line that fits [limit], newest last, whole (inert): what Details shows.
    None when the message itself already holds them all, uncut."""
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        line = inline(line, 400)
        size += len(line) + 3
        if size > limit:
            break
        kept.append(line)
    kept.reverse()
    left_out = len(lines) - len(kept)
    if not left_out and len(lines) <= VERBOSE_SHORT and all(len(x) <= LINE_MAX - 2 for x in kept):
        return None
    fact = plural(len(kept), "line") + (f" · {left_out} older left out" if left_out else "")
    return "\n".join([_log_head(kept, fact), *(f"- {line}" for line in kept)])


def cut_line(text: str, width: int = LINE_MAX) -> str:
    """[text] on one line of at most [width], cut at a word end with …"""
    if len(text) <= width:
        return text
    cut = text[:width - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > width // 2 else cut).rstrip(" ,;:·") + "…"


def reply_text(sender: str, incoming: list[str], reply: str, short: bool = True) -> tuple[str, str | None]:
    """One answered conversation: who, each message quoted, and the reply, long text cut into
    lines that fit, outside text inert (marks.inline). Short: two quoted lines and two of the
    reply, with the whole conversation as details; long (short=False): all of it."""
    who = inline(sender, 30) or "someone"
    most_quotes, most_reply = (2, 2) if short else (QUOTES * 3, REPLY_LINES * 4)
    quoted = [f"> {line}" for message in incoming if (text := inline(message, 1000))
              for line in wrap(text, LINE_MAX - 2, QUOTE_LINES if short else QUOTE_LINES * 2)]
    if len(quoted) > most_quotes:
        quoted = [*quoted[:most_quotes - 1], "> …"]
    label = "↩️ *Reply* "
    answer = wrap(inline(reply, 2000) or "…", LINE_MAX, most_reply, first=LINE_MAX - len(label))
    text = "\n".join([title("🆕", f"replied to {who}"), *(quoted or ["> …"]),
                      label + answer[0], *(start_safe(a) for a in answer[1:])])
    if not short:
        return text, None
    whole = reply_text(sender, incoming, reply, short=False)[0]
    return text, (whole if whole != text else None)


class ChatLogFormatter(logging.Formatter):
    """A log record as one short line: an emoji for a warning or an error, the time on the
    owner's clock, the module only when it is not the bot's own, the message, and for an
    exception only its last line (no traceback)."""

    def format(self, record: logging.LogRecord) -> str:
        icon = LOG_ICONS.get(record.levelno)
        when = owner_time(record.created).strftime("%H:%M")
        own = record.name == "bot" or record.name.startswith("bot.")
        text = f"{icon + ' ' if icon else ''}{when} {'' if own else record.name + ': '}"
        text += record.getMessage()
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


def median(entries: list[dict], field: str) -> float | None:
    values = [e[field] for e in entries if isinstance(e.get(field), (int, float))]
    return statistics.median(values) if values else None


def typical(entries: list[dict], field: str) -> list[str]:
    """The median and the 95th percentile of a timing field, in plain words."""
    values = [e[field] for e in entries if isinstance(e.get(field), (int, float))]
    if not values:
        return []
    return [f"typical {duration(statistics.median(values))}",
            f"95% within {duration(metrics.percentile(values, 0.95))}"]


# /stats timings: the table's row name, the kind of entry, the field. "- " rows are the parts
# of a reply's time.
TIMINGS = (("accept", "accept", "total_s"), ("reply", "reply", "total_s"),
           ("- seen", "reply", "detect_s"), ("- waited", "reply", "quiet_s"),
           ("- AI", "reply", "draft_s"))
STATS_DAYS = 7  # days in the /stats table, today first
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def timings_table(groups: dict[str, list[dict]]) -> str | None:
    """How long accepts, replies and a reply's parts take, typical and 95% within, as a
    table; None when fewer than three of them have data (the lines read better then)."""
    rows = []
    for name, kind, field in TIMINGS:
        values = [e[field] for e in groups[kind] if isinstance(e.get(field), (int, float))]
        if values:
            rows.append([name, duration(statistics.median(values)),
                         duration(metrics.percentile(values, 0.95))])
    return table(["time", "typical", "95%"], rows, right=(1, 2)) if len(rows) >= 3 else None


def days_table(entries: list[dict], now: float) -> str | None:
    """Accepts and replies a day for the last STATS_DAYS days on the owner's clock, today
    first, then each day by its weekday (none repeats); - for none. None when none of the
    days had any."""
    today = owner_time(now).date()
    counts = {today - timedelta(days=n): [0, 0] for n in range(STATS_DAYS)}
    for entry in entries:
        at = entry.get("at")
        kind = entry.get("kind")
        if isinstance(at, (int, float)) and kind in ("accept", "reply"):
            day = counts.get(owner_time(at).date())
            if day is not None:
                day[kind == "reply"] += 1
    if not any(sum(day) for day in counts.values()):
        return None
    rows = [["today" if day == today else WEEKDAYS[day.weekday()],
             accepts or "-", replies or "-"] for day, (accepts, replies) in counts.items()]
    return table(["day", "accepts", "replies"], rows, right=(1, 2))


def stats_text(entries: list[dict], now: float | None = None) -> str:
    """/stats: counts in the title, when the latest happened, who wrote the replies, then a
    table of the last days' accepts and replies and one of how long they take (typical and
    95% within, with where a reply's time goes)."""
    now = time.time() if now is None else now
    accepts = [e for e in entries if e.get("kind") == "accept"]
    replies = [e for e in entries if e.get("kind") == "reply"]
    if not (accepts or replies):
        return title("ℹ️", "stats", "no accepts or replies yet")
    latest = [f"{kind} {ago(now - max(e.get('at', 0) for e in group))}"
              for kind, group in (("reply", replies), ("accept", accepts)) if group]
    by_ai = sum(e.get("outcome") == "draft_sent" for e in replies)
    canned = sum(e.get("outcome") not in (None, "", "draft_sent") for e in replies)
    timings = timings_table({"accept": accepts, "reply": replies})
    steps = [f"{label} {duration(m)}" for label, field in
             (("seen after", "detect_s"), ("waited", "quiet_s"), ("AI", "draft_s"))
             if (m := median(replies, field)) is not None]
    lines = [
        title("ℹ️", "stats", f"{plural(len(accepts), 'accept')} · "
                             f"{plural(len(replies), 'reply', 'replies')}"),
        part("Latest", latest),
        part("Written", [f"{by_ai} by the AI" if by_ai else None,
                         f"{canned} canned" if canned else None]),
        days_table(entries, now),
        timings,
        None if timings else part("Time to accept", typical(accepts, "total_s")),
        None if timings else part("Time to reply", typical(replies, "total_s")),
        None if timings else part("Reply steps", steps),
    ]
    return "\n".join(line for line in lines if line)


def example(name: str) -> str:
    """A value to show in "/set name value": the current one, or the other choice."""
    tunable = settings.TUNABLES[name]
    value = getattr(settings.CONFIG, tunable.field)
    if tunable.kind == "bool":
        return "off" if value else "on"
    if tunable.kind == "choice":
        return next(choice for choice in tunable.choices if choice != value)
    return settings.show(name)


def changed(name: str) -> bool:
    return getattr(settings.CONFIG, settings.TUNABLES[name].field) != settings.DEFAULTS[name]


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


def notify(text: str, details: str | None = None) -> bool:
    """Send [text] to the owner from any thread (health alerts), [details] behind its Details
    button. True when the agent took it; a message it queued while Enc Chat is unreachable
    counts. Never raises, logs a code only."""
    bot = client_from_env()
    if bot is None:
        return False
    try:
        bot.send(text[:MAX_MESSAGE], details=details[:MAX_MESSAGE] if details else None)
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
        self.feed: list[tuple[str, list[str], str]] = []  # replies waiting for the next feed message
        self.feed_since = 0.0
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

    async def send(self, text: str, details: str | None = None) -> None:
        """[text] to the owner on Enc Chat, [details] behind its Details button. A refusal is
        logged, never raised."""
        text = text[:MAX_MESSAGE]
        if self.bot is not None:
            try:
                await asyncio.to_thread(self.bot.send, text, details=details[:MAX_MESSAGE] if details else None)
            except EncBotError as error:
                log.warning("Enc Chat send failed: %s", error.code)

    async def register_commands(self) -> None:
        """Fill the app's "/" menu (the same list as /help); try again until the agent
        answers."""
        commands = [{"command": name, "description": text} for name, _, text in COMMANDS]
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
        """One agent event: a command, /start, or a plain message from the owner (answered
        with a pointer to /help). Repeats and strangers are dropped silently."""
        event_id = str(event.get("id") or "")
        if event_id:
            if event_id in self.seen:
                return
            self.seen.append(event_id)
        kind = event.get("type")
        if kind not in ("command", "started", "message"):
            return
        if not self.is_owner(event.get("from") or {}):
            return  # Only the owner; strangers get nothing.
        if kind == "started":
            await self.send(HELP)
            return
        if kind == "message":
            await self.send("I only take commands · /help lists them")
            return
        args = event.get("args") or ""
        if isinstance(args, (list, tuple)):
            args = " ".join(str(a) for a in args)
        await self.handle(f"/{event.get('command', '')} {args}".strip())

    async def verbose_loop(self) -> None:
        """Send the log lines the /verbose level shows, as often as the level says, and the
        reply feed once FEED_EVERY_S has passed since its first reply."""
        last_sent = time.monotonic()
        while True:
            await asyncio.sleep(30)
            await self.flush_feed()
            every = VERBOSE_LEVELS[self.verbose][2]
            if every and time.monotonic() - last_sent >= every - 1:
                await self.send_verbose()
                last_sent = time.monotonic()

    async def send_verbose(self) -> None:
        lines = self.verbose_log.take(self.verbose)
        if lines:
            await self.send(verbose_message(lines), verbose_details(lines))

    async def set_verbose(self, argument: str) -> None:
        current = self.verbose
        if not argument:
            await self.send(verbose_help(current))
            return
        word = {"off": "0", "on": "3"}.get(argument, argument)
        if not (word.isdigit() and int(word) in VERBOSE_LEVELS):
            await self.send(f"Use /verbose 0-5 · it stays at {current}")
            return
        level = int(word)
        self.engine.set_verbose(level)
        apply_log_level(level)
        self.verbose_log.take(0)  # start from new lines only
        every = VERBOSE_LEVELS[level][2]
        text = level_title("✅", level)
        if level:
            text += f"\nSent every {duration(every)} · /verbose 0 stops it"
        await self.send(text)

    async def handle(self, text: str) -> None:
        if not text.startswith("/"):
            return
        words = text.split()
        command = words[0].split("@")[0].lower()
        if command == "/interval":  # the old name of /set poll
            command, words = "/set", ["/set", "poll", *words[1:]]
        argument = words[1].lower() if len(words) > 1 else ""
        engine = self.engine
        if command in ("/start", "/help"):
            await self.send(HELP)
        elif command == "/status":
            await self.send(self.status_text())
        elif command == "/stats":
            await self.send(stats_text(metrics.load(CONFIG.metrics_path)))
        elif command == "/pause":
            await self.pause(True)
        elif command == "/resume":
            await self.pause(False)
        elif command == "/replies":
            if argument not in ("", "on", "off"):
                await self.send("Use /replies on or /replies off")
                return
            on = argument == "on" if argument else not engine.replies_feed
            engine.set_replies_feed(on)
            await self.send(title("✅", "replies on", "each message and its reply come here")
                            if on else title("✅", "replies off", "/replies on brings them back"))
        elif command == "/verbose":
            await self.set_verbose(argument)
        elif command == "/catchup":
            await self.catchup(argument)
        elif command == "/settings":
            await self.send(self.settings_text())
        elif command == "/set":
            await self.set_setting(argument, " ".join(words[2:]))
        elif command == "/reset":
            await self.reset_setting(argument)
        else:
            await self.send(f"I don't know {inline(command, 30)} · /help lists what I can do")

    async def pause(self, paused: bool) -> None:
        engine = self.engine
        if engine.paused == paused:
            await self.send(title("ℹ️", "already paused", "/resume starts it again") if paused
                            else title("ℹ️", "not paused", "/status shows what it does"))
            return
        engine.set_paused(paused)
        if paused:
            await self.send("\n".join([
                title("✅", "paused", "no accepts or replies until /resume"),
                "Messages meanwhile get answered then, unless you reply first",
            ]))
        else:
            await self.send(title("✅", "resumed", "accepting and answering again"))

    async def catchup(self, argument: str) -> None:
        engine = self.engine
        if argument == "stop":
            progress = engine.stop_catchup()
            if progress is None:
                await self.send(title("ℹ️", "no catch-up running"))
            else:
                answered, total = progress
                await self.send(title("✅", "catch-up stopped",
                                      f"{answered} of {total} answered" if total
                                      else "before it began"))
            return
        try:
            days = float(argument)
        except ValueError:
            days = 0
        if not 0 < days <= 30:
            await self.send("Use /catchup 1-30 (days back) or /catchup stop")
            return
        if engine.start_catchup(days, self.send):
            await self.send(title("⏳", "catch-up", f"looking back {days_text(days)}…"))
        else:
            await self.send(title("❌", "catch-up already running", "/catchup stop ends it"))

    @staticmethod
    def settings_text() -> str:
        """Every setting, a line per kind, its value side by side with the others; a changed
        one in bold, with its default on a line of its own."""
        grouped = [n for names in SETTING_LINES.values() for n in names]
        groups = {**SETTING_LINES,
                  "Other": tuple(n for n in settings.TUNABLES if n not in grouped)}
        lines = []
        for group, names in groups.items():
            names = [n for n in names if n in settings.TUNABLES]
            for start in range(0, len(names), 3):
                lines.append(f"*{group}* " + DOT.join(
                    f"{n} *{settings.show(n)}*" if changed(n) else f"{n} {settings.show(n)}"
                    for n in names[start:start + 3]))
        moved = [n for n in settings.TUNABLES if changed(n)]
        defaults = [f"{n} {settings.show(n, settings.DEFAULTS[n])}" for n in moved]
        lines += ["*Defaults* " + DOT.join(defaults[start:start + 3])
                  for start in range(0, len(defaults), 3)]
        emoji = "🟡" if settings.CONFIG.dry_run else "ℹ️"
        head = title(emoji, "settings", f"{len(moved)} changed" if moved else "all default")
        how = "/set name explains · /set name value changes · /reset name undoes"
        return "\n".join([head, *lines, how])

    async def set_setting(self, name: str, value: str) -> None:
        name = name.replace("-", "_")
        if not name:
            await self.send("Use /set name value · /settings lists the names")
            return
        if name not in settings.TUNABLES:
            await self.send(f"I don't know the setting {inline(name, 30)} · "
                            "/settings lists them")
            return
        tunable = settings.TUNABLES[name]
        default = settings.show(name, settings.DEFAULTS[name])
        if not value:
            await self.send("\n".join([
                title("ℹ️", f"{name} {settings.show(name)}", tunable.help),
                f"Default {default} · /set {name} {example(name)} changes it",
            ]))
            return
        try:
            parsed = settings.parse(name, value)
            self.engine.change_setting(name, parsed)
        except ValueError as error:  # it may quote what the owner typed
            await self.send(alert_text(f"{name} not changed", str(error), "❌"))
            return
        if name == "dry_run" and parsed:
            text = [title("🟡", "dry run on", "nothing is sent or accepted"),
                    "/set dry_run off ends it"]
        elif name == "scope" and parsed == "all":
            text = [title("🟡", "scope all", "may answer chats you started too"),
                    f"/reset scope goes back to {default}"]
        else:
            text = [title("✅", f"{name} now {settings.show(name)}", "kept after restarts")]
            if changed(name):
                text.append(f"/reset {name} goes back to {default}")
        await self.send("\n".join(text))

    async def reset_setting(self, name: str) -> None:
        if not name:
            await self.send("Use /reset name or /reset all · /settings lists the names")
            return
        names = list(settings.TUNABLES) if name == "all" else [name.replace("-", "_")]
        if names[0] not in settings.TUNABLES:
            await self.send(f"I don't know the setting {inline(name, 30)} · "
                            "/settings lists them")
            return
        moved = [n for n in names if changed(n)]
        for n in names:
            self.engine.change_setting(n, None)
        if name == "all":
            fact = ("none had changed" if not moved
                    else f"{', '.join(moved)} had changed" if len(moved) <= 3
                    else f"{len(moved)} had changed")
            await self.send(title("✅", "all settings default", fact))
        else:
            await self.send(title("✅", f"{names[0]} back to {settings.show(names[0])}"))

    def status_text(self) -> str:
        """/status: the state and what to do about it first, then the inbox, the counts since
        the start, and what comes to this chat."""
        engine = self.engine
        state = "paused" if engine.paused else engine.state
        emoji, name = STATES.get(state, ("🟡", inline(state, 30) or "unknown"))
        dry_run = settings.CONFIG.dry_run
        if state == "running" and dry_run:
            emoji, name = "🟡", "running, dry run"
        now = time.time()
        checked = (f"checked {ago(now - engine.checked_at)}" if engine.checked_at
                   else "no check yet")
        step = NEXT_STEP.get(state)
        pace = (f"chat on, every {settings.show('poll')}" if now < engine.watch_until
                else f"{settings.show('checks')} a day")
        upcoming = (f"next {owner_time(engine.next_check):%H:%M}"
                    if not step and engine.next_check > now else None)
        log_level = f"log level {self.verbose}" if self.verbose else "log off"
        lines = [
            title(emoji, name, step or checked),
            "Nothing is sent or accepted · /set dry_run off" if dry_run else None,
            part("Inbox", [checked if step else None, pace, upcoming,
                           f"hours {settings.show('active_hours')}"]),
            (f"*Since start* {plural(engine.accepted, 'accept')} · "
             f"{plural(engine.replies_sent, 'reply', 'replies')}"),
            f"*In this chat* replies {'on' if engine.replies_feed else 'off'} · {log_level}",
        ]
        return "\n".join(line for line in lines if line)

    async def reply_sent(self, sender: str, incoming: list[str], reply: str) -> None:
        """An answered conversation joins the reply feed; flush_feed() sends what waits as
        one message at most every FEED_EVERY_S."""
        if not (self.enabled and self.engine.replies_feed):
            return
        if not self.feed:
            self.feed_since = time.monotonic()
        self.feed.append((sender, list(incoming), reply))

    async def flush_feed(self, force: bool = False) -> None:
        """The replies since the last feed message, as one message, once FEED_EVERY_S has
        passed since the first of them (or at once with [force])."""
        if not self.feed or not (force or time.monotonic() - self.feed_since >= FEED_EVERY_S):
            return
        items, self.feed = self.feed, []
        if len(items) == 1:
            await self.send(*reply_text(*items[0]))
            return
        lines = [title("🆕", f"{len(items)} replies sent")]
        for sender, _incoming, reply in items[:FEED_SHORT]:
            who = inline(sender, 30) or "someone"
            lines.append(start_safe(cut_line(f"*{who}* {inline(reply, 300) or '…'}")))
        if len(items) > FEED_SHORT:
            lines.append(f"+{len(items) - FEED_SHORT} more in Details")
        details = "\n\n".join(reply_text(*item, short=False)[0] for item in items)
        await self.send("\n".join(lines), details[:MAX_MESSAGE])
