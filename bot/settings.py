"""Settings the owner can change from Enc Chat while the bot runs.

A change updates CONFIG in place, so every module sees it at once, and is kept in the
store so it survives a restart. /reset goes back to the value from the environment.
"""

from dataclasses import dataclass

from .config import CONFIG, Config

PREFIX = "setting:"
# What may follow a number; other units are refused rather than misread.
UNIT_ALIASES = {"min": ("min", "m"), "s": ("sec", "s"), "h": ("h",)}
# How a value's unit is shown, right after the number (5-15m, 60s, 8-24h).
UNIT_SHOWN = {"min": "m", "s": "s", "h": "h"}


@dataclass(frozen=True)
class Tunable:
    field: str  # Config field
    kind: str  # range, number, int, bool, choice
    unit: str  # what the owner types: min, s, h, or ""
    help: str  # what it is, in a few lowercase words (/set name shows it)
    low: float = 0
    high: float = 1e9
    wraps: bool = False  # range whose start may be after its end (hours past midnight)
    choices: tuple[str, ...] = ()

    @property
    def scale(self) -> float:
        return {"min": 60.0, "h": 3600.0}.get(self.unit, 1.0) if self.field.endswith("_s") else 1.0


TUNABLES: dict[str, Tunable] = {
    "poll": Tunable("poll_s", "range", "min", "random wait between inbox checks", 1, 240),
    "check_gap": Tunable("min_check_gap_s", "number", "s", "shortest time between checks",
                         10, 3600),
    "reply_delay": Tunable("reply_delay_s", "range", "s", "random wait before a reply", 0, 3600),
    "active_hours": Tunable("active_hours", "range", "h", "hours it works, 8-2 runs past 0",
                            0, 24, wraps=True),
    "invite_window": Tunable("invite_window", "range", "h", "hours of the daily accept round",
                             0, 24),
    "accept_gap": Tunable("accept_gap_s", "range", "s", "random pause between accepts", 0, 3600),
    "catchup_gap": Tunable("catchup_gap_s", "range", "min", "pause between /catchup answers",
                           0, 240),
    "max_replies": Tunable("max_replies_per_day", "int", "", "most replies a day", 0, 1000),
    "max_accepts": Tunable("max_accepts_per_day", "int", "", "most accepts a day, 0 no limit",
                           0, 1000),
    "backoff": Tunable("backoff_s", "number", "min", "first wait after a push-back, doubles",
                       1, 24 * 60),
    "max_backoff": Tunable("max_backoff_s", "number", "min", "longest wait after push-backs",
                           1, 48 * 60),
    "dry_run": Tunable("dry_run", "bool", "", "on: send and accept nothing, only log"),
    "scope": Tunable("reply_scope", "choice", "", "new_threads: only chats after the bot",
                     choices=("new_threads", "all")),
}

DEFAULTS = {name: getattr(CONFIG, t.field) for name, t in TUNABLES.items()}


def number_text(value: float) -> str:
    return f"{value:g}"


def show(name: str, value: object | None = None) -> str:
    """A value as the owner types it, e.g. 5-15m, 60s, 8-24h."""
    tunable = TUNABLES[name]
    value = getattr(CONFIG, tunable.field) if value is None else value
    if tunable.kind == "bool":
        return "on" if value else "off"
    if tunable.kind == "choice":
        return str(value)
    unit = UNIT_SHOWN.get(tunable.unit, "")
    if tunable.kind == "range":
        low, high = (v / tunable.scale for v in value)
        return f"{number_text(low)}-{number_text(high)}{unit}"
    return f"{number_text(value / tunable.scale)}{unit}"


def parse(name: str, text: str) -> object:
    """The Config value for what the owner typed. ValueError says what is wrong."""
    tunable = TUNABLES[name]
    text = text.strip().lower()
    if tunable.kind == "bool":
        if text in ("on", "1", "true", "yes"):
            return True
        if text in ("off", "0", "false", "no"):
            return False
        raise ValueError("use on or off")
    if tunable.kind == "choice":
        if text not in tunable.choices:
            raise ValueError(f"use {' or '.join(tunable.choices)}")
        return text
    for suffix in UNIT_ALIASES.get(tunable.unit, ()):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break

    def number(part: str) -> float:
        try:
            value = float(part)
        except ValueError:
            raise ValueError(f"{part!r} is not a number") from None
        if not tunable.low <= value <= tunable.high:
            raise ValueError(f"use {number_text(tunable.low)} to {number_text(tunable.high)}"
                             f"{UNIT_SHOWN.get(tunable.unit, '')}")
        return value

    if tunable.kind == "range":
        low_text, dash, high_text = text.partition("-")
        low = number(low_text)
        high = number(high_text) if dash else low
        if low > high and not tunable.wraps:
            raise ValueError("the first number is above the second")
        return (low * tunable.scale, high * tunable.scale)
    value = number(text)
    if tunable.kind == "int":
        if value != int(value):
            raise ValueError("use a whole number")
        return int(value)
    return value * tunable.scale


def check(config: Config) -> None:
    """Rules between settings. ValueError says which one breaks."""
    if config.max_backoff_s < config.backoff_s:
        raise ValueError(f"max_backoff below backoff {show('backoff')}")
    if config.poll_s[0] < config.min_check_gap_s:
        raise ValueError(f"poll below check_gap {show('check_gap')}")


def store_text(name: str, value: object) -> str:
    tunable = TUNABLES[name]
    if tunable.kind == "range":
        return f"{value[0]:g}-{value[1]:g}"
    if tunable.kind == "bool":
        return "1" if value else "0"
    return str(value)


def from_store(name: str, text: str) -> object:
    """A stored value back to its Config value. Stored values are already in Config units."""
    tunable = TUNABLES[name]
    if tunable.kind == "range":
        low, _, high = text.partition("-")
        return (float(low), float(high or low))
    if tunable.kind == "bool":
        return text == "1"
    if tunable.kind == "int":
        return int(text)
    if tunable.kind == "number":
        return float(text)
    return text


def apply(name: str, value: object, store=None) -> None:
    """Set one setting now, checked against the others, and keep it if a store is given."""
    tunable = TUNABLES[name]
    old = getattr(CONFIG, tunable.field)
    setattr(CONFIG, tunable.field, value)
    try:
        check(CONFIG)
    except ValueError:
        setattr(CONFIG, tunable.field, old)
        raise
    if store is not None:
        if value == DEFAULTS[name]:
            store.set_meta(PREFIX + name, "")
        else:
            store.set_meta(PREFIX + name, store_text(name, value))


def load(store) -> list[str]:
    """Apply the settings kept in the store. Returns the names that could not be used."""
    bad = []
    for name in TUNABLES:
        text = store.get_meta(PREFIX + name)
        if not text:
            continue
        try:
            apply(name, from_store(name, text))
        except ValueError:
            bad.append(name)
            store.set_meta(PREFIX + name, "")
    return bad


def reset(name: str, store=None) -> None:
    setattr(CONFIG, TUNABLES[name].field, DEFAULTS[name])
    if store is not None:
        store.set_meta(PREFIX + name, "")

