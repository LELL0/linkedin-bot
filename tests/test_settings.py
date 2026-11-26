"""Settings changed from Enc Chat. All data is synthetic."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from bot import engine as engine_module
from bot import enc, settings
from bot.config import CONFIG


class MemoryStore:
    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def get_meta(self, key: str, default: str = "") -> str:
        return self.meta.get(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


@pytest.fixture(autouse=True)
def defaults():
    for name in settings.TUNABLES:
        settings.reset(name)
    yield
    for name in settings.TUNABLES:
        settings.reset(name)


def test_parse_uses_the_unit_the_owner_types():
    assert settings.parse("poll", "5-15") == (300.0, 900.0)
    assert settings.parse("poll", "2-4 min") == (120.0, 240.0)
    assert settings.parse("poll", "10m") == (600.0, 600.0)
    assert settings.parse("reply_delay", "30-60s") == (30.0, 60.0)
    assert settings.parse("active_hours", "8-2") == (8.0, 2.0)
    assert settings.parse("backoff", "10") == 600.0
    assert settings.parse("max_replies", "25") == 25
    assert settings.parse("dry_run", "on") is True
    assert settings.parse("scope", "ALL") == "all"


@pytest.mark.parametrize("name, text", [
    ("poll", "15-5"),  # reversed
    ("poll", "0.5"),  # below 1 min
    ("poll", "300s"),  # wrong unit, not read as 300 min
    ("poll", "fast"),
    ("max_replies", "2.5"),
    ("max_replies", "-1"),
    ("dry_run", "maybe"),
    ("scope", "everyone"),
    ("active_hours", "8-25"),
])
def test_parse_refuses_bad_values(name, text):
    with pytest.raises(ValueError):
        settings.parse(name, text)


def test_show_round_trips():
    for name in settings.TUNABLES:  # shown as typed: 5-15m, 60s, 8-24h
        text = settings.show(name)
        assert " " not in text
        assert settings.parse(name, text) == getattr(CONFIG, settings.TUNABLES[name].field)
    assert settings.show("poll") == "2-10m" and settings.show("check_gap") == "60s"


@pytest.mark.parametrize("name, text, error", [
    ("poll", "0.5", "use 1 to 240m"),
    ("poll", "15-5", "the first number is above the second"),
    ("poll", "fast", "'fast' is not a number"),
    ("max_replies", "2.5", "use a whole number"),
    ("dry_run", "maybe", "use on or off"),
    ("scope", "everyone", "use new_threads or all"),
])
def test_parse_errors_say_what_to_type(name, text, error):
    with pytest.raises(ValueError) as caught:
        settings.parse(name, text)
    assert str(caught.value) == error


def test_apply_is_kept_and_loaded_after_a_restart():
    store = MemoryStore()
    settings.apply("poll", (120.0, 240.0), store)
    settings.apply("dry_run", True, store)
    assert CONFIG.poll_s == (120.0, 240.0)
    settings.reset("poll")
    settings.reset("dry_run")
    assert settings.load(store) == []
    assert CONFIG.poll_s == (120.0, 240.0) and CONFIG.dry_run is True


def test_rules_between_settings_are_checked_and_nothing_changes():
    with pytest.raises(ValueError, match="check_gap"):
        settings.apply("poll", (30.0, 60.0))
    assert CONFIG.poll_s == settings.DEFAULTS["poll"]
    with pytest.raises(ValueError, match="max_backoff"):
        settings.apply("max_backoff", 60.0)


def test_broken_stored_value_falls_back_to_default():
    store = MemoryStore()
    store.meta["setting:poll"] = "10-20"  # 10 s: below check_gap
    assert settings.load(store) == ["poll"]
    assert CONFIG.poll_s == settings.DEFAULTS["poll"] and store.meta["setting:poll"] == ""


def control(monkeypatch):
    monkeypatch.setenv("ENC_BOT_SECRET", "s")
    store = MemoryStore()
    changes = []

    def change_setting(name, value):
        changes.append(name)
        if value is None:
            settings.reset(name, store)
        else:
            settings.apply(name, value, store)

    engine = SimpleNamespace(paused=False, replies_feed=False, state="running", last_ok=0,
                             accepted=0, replies_sent=0, change_setting=change_setting)
    tg = enc.EncControl(engine)
    sent = []

    async def send(text):
        sent.append(text)

    tg.send = send
    return tg, store, changes, sent


def test_enc_set_interval_reset(monkeypatch):
    tg, store, _changes, sent = control(monkeypatch)
    asyncio.run(tg.handle("/interval 2-4"))
    assert CONFIG.poll_s == (120.0, 240.0)
    assert sent[-1] == ("✅ *LinkedIn · poll now 2-4m* · kept after restarts\n"
                        "/reset poll goes back to 2-10m")
    asyncio.run(tg.handle("/set max_replies 30"))
    assert CONFIG.max_replies_per_day == 30 and store.meta["setting:max_replies"] == "30"
    assert sent[-1] == ("✅ *LinkedIn · max_replies now 30* · kept after restarts\n"
                        "/reset max_replies goes back to 40")
    asyncio.run(tg.handle("/settings"))
    assert sent[-1].startswith("ℹ️ *LinkedIn · settings* · 2 changed\n")
    assert "\n*Inbox* checks 3 · watch 2h · poll *2-4m*\n" in sent[-1]
    assert "\n*Replies* reply_delay 25-150s · max_replies *30* · " in sent[-1]
    assert "\n*Mode* active_hours 8-24h · dry_run off · scope " in sent[-1]  # unchanged: plain
    assert "\n*Defaults* poll 2-10m · max_replies 40\n" in sent[-1]
    asyncio.run(tg.handle("/set poll"))
    assert sent[-1] == ("ℹ️ *LinkedIn · poll 2-4m* · random wait between checks in a chat\n"
                        "Default 2-10m · /set poll 2-4m changes it")
    asyncio.run(tg.handle("/set poll 15-5"))
    assert sent[-1] == "❌ *LinkedIn · poll not changed* · the first number is above the second"
    assert CONFIG.poll_s == (120.0, 240.0)
    asyncio.run(tg.handle("/set check_gap 3600"))
    assert sent[-1] == "❌ *LinkedIn · check_gap not changed* · poll below check_gap 3600s"
    asyncio.run(tg.handle("/set max_backoff 1"))
    assert sent[-1] == "❌ *LinkedIn · max_backoff not changed* · max_backoff below backoff 5m"
    asyncio.run(tg.handle("/set nope 1"))
    assert sent[-1] == "I don't know the setting nope · /settings lists them"
    asyncio.run(tg.handle("/reset poll"))
    assert sent[-1] == "✅ *LinkedIn · poll back to 2-10m*"
    asyncio.run(tg.handle("/set poll 2-4"))
    asyncio.run(tg.handle("/reset all"))
    assert CONFIG.poll_s == settings.DEFAULTS["poll"] and CONFIG.max_replies_per_day == 40
    assert sent[-1] == "✅ *LinkedIn · all settings default* · poll, max_replies had changed"


def test_new_interval_cuts_a_long_wait_short(monkeypatch):
    """While a chat goes on, a new poll interval replaces the wait already going."""
    engine = engine_module.Engine.__new__(engine_module.Engine)
    engine.last_check, engine.check_failed = time.time() + 0.5, False
    engine.watch_until = time.time() + 7200
    monkeypatch.setattr(settings.CONFIG, "active_hours", (0.0, 24.0))

    async def run():
        engine.poll_now = asyncio.Event()
        settings.apply("poll", (3600.0, 3600.0))
        task = asyncio.create_task(engine.sleep_until_next_poll())
        await asyncio.sleep(0.05)
        assert not task.done()
        monkeypatch.setattr(settings.CONFIG, "min_check_gap_s", 0.0)
        settings.apply("poll", (0.0, 0.0))
        engine.poll_now.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(run())
