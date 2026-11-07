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
