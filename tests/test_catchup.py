"""/catchup: answering older unanswered messages on request, never on its own."""

import asyncio

from bot import engine as engine_module
from tests.test_enc import assert_style
from tests.test_engine_flow import ME, now_ms, settle, sim  # noqa: F401 - fixture

DAY = 86400


def run(bot, days):
    reports = []

    async def report(text, details=None):
        reports.append(text)

    async def go():
        assert bot.start_catchup(days, report)
        await bot.catchup_task

    asyncio.run(go())
    for text in reports:
        assert_style(text, 3)
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
    assert reports[-1] == "✅ *LinkedIn · catch-up done* · 1 of 1 answered"
    lines = reports[0].split("\n")
    assert lines[0] == "⏳ *LinkedIn · catch-up, 1 to answer* · last 3 days"
    assert lines[1].startswith("*In order* ") and lines[2].startswith("*Pace* one every ")


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
    assert run(bot, 2) == ["✅ *LinkedIn · catch-up done* · nothing unanswered, last 2 days"]


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
    assert reports[-1] == ("🟡 *LinkedIn · catch-up stopped* · daily reply limit reached\n"
                           "*So far* 1 answered · 1 left · /catchup again tomorrow")


def test_stop_reports_the_progress_and_the_catchup_says_nothing_more(sim, monkeypatch):  # noqa: F811
    bot = sim()
    fast(monkeypatch)
    bot.api.add("a", at=now_ms(-DAY))
    bot.api.add("b", at=now_ms(-DAY + 10))
    reports = []
    answer = bot.answer_thread

    async def report(text, details=None):
        reports.append(text)

    async def go():
        second = asyncio.Event()

        async def hold_the_second(thread, noticed_at, trigger):
            if bot.api.sent:
                second.set()
                await asyncio.Event().wait()  # until cancelled
            await answer(thread, noticed_at, trigger)

        bot.answer_thread = hold_the_second
        assert bot.stop_catchup() is None
        assert bot.start_catchup(3, report)
        await second.wait()
        progress = bot.stop_catchup()
        await asyncio.gather(bot.catchup_task, return_exceptions=True)
        return progress

    assert asyncio.run(go()) == (1, 2)
    assert len(reports) == 1 and reports[0].startswith("⏳ *LinkedIn · catch-up, 2 to answer*")


def test_prompt_mentions_a_late_answer_only_when_late():
    from bot.brain import build_prompt
    from bot.models import Message

    old = Message("m1", "them", now_ms(-DAY), "hi")
    new = Message("m2", "them", now_ms(-60), "hi")
    assert "late reply" in build_prompt([old], [old], ME, "Lynn")
    assert "late reply" not in build_prompt([new], [new], ME, "Lynn")
