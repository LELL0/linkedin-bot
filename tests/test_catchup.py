"""/catchup: answering older unanswered messages on request, never on its own."""

import asyncio

from bot import engine as engine_module
from tests.test_engine_flow import ME, now_ms, settle, sim  # noqa: F401 - fixture

DAY = 86400


def run(bot, days):
    reports = []

    async def report(text):
        reports.append(text)

    async def go():
        assert bot.start_catchup(days, report)
        await bot.catchup_task

    asyncio.run(go())
    return reports


def fast(monkeypatch):
    async def instant(*_):
        return None

    monkeypatch.setattr(engine_module.asyncio, "sleep", instant)


def test_old_messages_wait_for_the_command(sim, monkeypatch):  # noqa: F811
    bot = sim()
    fast(monkeypatch)
    bot.api.add("old", at=now_ms(-DAY))  # Before the bot started: normal polling skips it.
    asyncio.run(settle(bot))
    assert bot.api.sent == []
    reports = run(bot, 3)
    assert [urn for urn, _ in bot.api.sent] == ["old"]
    assert "1 conversation" in reports[0] and "1 of 1 answered" in reports[-1]


def test_only_unanswered_within_the_days(sim, monkeypatch):  # noqa: F811
    bot = sim()
    fast(monkeypatch)
    bot.api.add("recent", at=now_ms(-DAY))
    bot.api.add("too_old", at=now_ms(-5 * DAY))
    bot.api.add("answered", at=now_ms(-DAY))
    bot.api.add("answered", sender=ME, text="hey", at=now_ms(-DAY + 60))
    run(bot, 3)
    assert [urn for urn, _ in bot.api.sent] == ["recent"]


def test_nothing_to_answer_is_reported(sim, monkeypatch):  # noqa: F811
    bot = sim()
    fast(monkeypatch)
    assert "no unanswered messages" in run(bot, 2)[0]


def test_daily_reply_limit_stops_the_catchup(sim, monkeypatch):  # noqa: F811
    import dataclasses

    bot = sim()
    fast(monkeypatch)
    monkeypatch.setattr(engine_module, "CONFIG",
                        dataclasses.replace(engine_module.CONFIG, max_replies_per_day=1))
    bot.api.add("a", at=now_ms(-DAY))
    bot.api.add("b", at=now_ms(-DAY + 10))
    reports = run(bot, 3)
    assert len(bot.api.sent) == 1
    assert "daily reply limit" in reports[-1]


def test_prompt_mentions_a_late_answer_only_when_late():
    from bot.brain import build_prompt
    from bot.models import Message

    old = Message("m1", "them", now_ms(-DAY), "hi")
    new = Message("m2", "them", now_ms(-60), "hi")
    assert "late reply" in build_prompt([old], [old], ME, "Lynn")
    assert "late reply" not in build_prompt([new], [new], ME, "Lynn")
