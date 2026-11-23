"""Telegram commands for the owner. All data is synthetic."""

import asyncio
from types import SimpleNamespace

from bot import telegram


def control(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_CHAT_ID", "42")
    engine = SimpleNamespace(paused=False, replies_feed=False, state="running", last_ok=0,
                             accepted=1, replies_sent=2)
    engine.set_paused = lambda on: setattr(engine, "paused", on)
    engine.set_replies_feed = lambda on: setattr(engine, "replies_feed", on)
    tg = telegram.TelegramControl(engine)
    sent = []

    async def send(text):
        sent.append(text)

    tg.send = send
    return tg, engine, sent


def test_pause_resume_and_replies_toggle(monkeypatch):
    tg, engine, sent = control(monkeypatch)
    asyncio.run(tg.handle("/pause"))
    assert engine.paused is True
    asyncio.run(tg.handle("/resume"))
    assert engine.paused is False
    asyncio.run(tg.handle("/replies on"))
    assert engine.replies_feed is True
    asyncio.run(tg.handle("/replies"))
    assert engine.replies_feed is False
    asyncio.run(tg.handle("/status"))
    assert "running" in sent[-1] and "1 accepted, 2 replies" in sent[-1]


def test_reply_feed_is_escaped_and_only_when_on(monkeypatch):
    tg, engine, sent = control(monkeypatch)
    asyncio.run(tg.reply_sent("Sam", ["<b>hi</b>"], "hey"))
    assert sent == []
    engine.replies_feed = True
    asyncio.run(tg.reply_sent("Sam", ["<b>hi</b>"], "hey"))
    assert "&lt;b&gt;hi&lt;/b&gt;" in sent[0] and "hey" in sent[0]


def test_strangers_are_ignored(monkeypatch):
    tg, engine, sent = control(monkeypatch)
    calls = []

    async def fake_call(method, http_timeout=20, **params):
        calls.append(method)
        if method == "getUpdates":
            if calls.count("getUpdates") > 1:
                raise asyncio.CancelledError
            return [{"update_id": 1, "message": {"chat": {"id": 7}, "text": "/pause"}}]
        return True

    tg.call = fake_call
    try:
        asyncio.run(tg.loop())
    except asyncio.CancelledError:
        pass
    assert engine.paused is False and sent == []


def test_verbose_sets_a_level_and_sends_only_its_lines(monkeypatch):
    tg, engine, sent = control(monkeypatch)
    tg.token = "123:FAKETOKEN"
    engine.verbose = 0
    engine.set_verbose = lambda level: setattr(engine, "verbose", level)
    asyncio.run(tg.handle("/verbose 2"))
    assert engine.verbose == 2 and "Verbose 2" in sent[-1] and "2 min" in sent[-1]
    log = telegram.logging.getLogger("bot.engine")
    for text in ("Accepted invitation: {}", "Draft rejected: too long", "alive: accepted=1"):
        tg.verbose_log.emit(log.makeRecord("bot.engine", 20, __file__, 1, text, (), None))
    tg.verbose_log.emit(log.makeRecord("bot.session", 30, __file__, 1, "Accept failed", (), None))
    asyncio.run(tg.send_verbose())
    assert "Accepted invitation" in sent[-1] and "Accept failed" in sent[-1]
    assert "Draft rejected" not in sent[-1] and "alive" not in sent[-1]
    count = len(sent)
    asyncio.run(tg.send_verbose())  # nothing new: no empty message
    assert len(sent) == count
    for wrong in ("/verbose loud", "/verbose 6"):
        asyncio.run(tg.handle(wrong))
        assert engine.verbose == 2 and "level from 0 to 5" in sent[-1]
    asyncio.run(tg.handle("/verbose"))
    assert "Verbose level: <b>2</b>" in sent[-1]
    asyncio.run(tg.handle("/verbose off"))
    assert engine.verbose == 0
    asyncio.run(tg.handle("/status"))
    assert "Verbose: 0" in sent[-1]
    telegram.logging.getLogger().removeHandler(tg.verbose_log)
    telegram.apply_log_level(0)


def test_record_levels_and_token_never_sent(monkeypatch):
    make = telegram.logging.getLogger("x").makeRecord

    def level(name, levelno, text):
        return telegram.record_level(make(name, levelno, __file__, 1, text, (), None))

    assert level("bot.session", 40, "boom") == 1
    assert level("bot.engine", 20, "Replied: x") == 2
    assert level("bot.brain", 20, "Draft blocked: x") == 3
    assert level("bot.engine", 20, "alive: accepted=0") == 4
    assert level("bot.engine", 10, "detail") == 5
    assert level("playwright", 20, "x") == 5
    assert level("httpx", 40, "POST https://api.telegram.org/botSECRET/x") == 6
    tg, engine, sent = control(monkeypatch)
    engine.verbose = 5
    tg.token = "123:FAKETOKEN"
    tg.verbose_log.lines.append((1, "url /bot123:FAKETOKEN/x"))
    asyncio.run(tg.send_verbose())
    assert "***" in sent[-1]
    telegram.logging.getLogger().removeHandler(tg.verbose_log)


def test_catchup_command_checks_days_and_stop(monkeypatch):
    tg, engine, sent = control(monkeypatch)
    started = []
    engine.start_catchup = lambda days, report: started.append(days) or True
    engine.stop_catchup = lambda: False
    asyncio.run(tg.handle("/catchup"))
    asyncio.run(tg.handle("/catchup 99"))
    assert started == [] and "1 to 30" in sent[-1]
    asyncio.run(tg.handle("/catchup 3"))
    assert started == [3.0]
    asyncio.run(tg.handle("/catchup stop"))
    assert "No catch-up" in sent[-1]
