"""Request pacing that keeps the account from looking like a bot: night hours, accept
delays, daily limits, and stopping every loop once LinkedIn rejects the session."""

import asyncio
import dataclasses
import time
from datetime import datetime

from bot import engine as engine_module
from bot.config import CONFIG
from bot.linkedin import AuthError
from bot.models import Invitation
from tests.test_engine_flow import now_ms, sim  # noqa: F401 - fixture


def _config(monkeypatch, **fields):
    config = dataclasses.replace(engine_module.CONFIG, **fields)
    monkeypatch.setattr(engine_module, "CONFIG", config)
    return config


def test_active_hours_plain_and_past_midnight(monkeypatch):
    _config(monkeypatch, active_hours=(8.0, 24.0))
    assert engine_module.in_active_hours(datetime(2026, 1, 1, 9, 0))
    assert not engine_module.in_active_hours(datetime(2026, 1, 1, 3, 0))
    _config(monkeypatch, active_hours=(8.0, 2.0))
    assert engine_module.in_active_hours(datetime(2026, 1, 1, 1, 0))
    assert not engine_module.in_active_hours(datetime(2026, 1, 1, 5, 0))


def test_defaults_are_slow():
    assert CONFIG.poll_s == (300.0, 900.0)
    assert CONFIG.min_check_gap_s >= 60
    assert CONFIG.max_accepts_per_day == 0


def _invite(urn, sent_at):
    return Invitation(urn=urn, invitation_id=urn, shared_secret="s", kind="CONNECTION",
                      sent_at=sent_at, inviter_id=f"p-{urn}", inviter_name="Sam")


def test_invites_once_a_day_in_the_morning(monkeypatch):
    _config(monkeypatch, invite_window=(9.0, 11.0))
    day = datetime(2026, 1, 1)
    at = engine_module.invite_time(day.date())
    assert 9 <= at <= 11
    assert not engine_module.invites_due("", day.replace(hour=8, minute=59))
    assert engine_module.invites_due("", day.replace(hour=11, minute=30))
    assert not engine_module.invites_due("2026-01-01", day.replace(hour=11, minute=30))


def test_daily_round_accepts_every_pending_invite(sim, monkeypatch):  # noqa: F811
    bot = sim()
    bot.api.invitations = [_invite(str(i), now_ms(-5)) for i in range(25)]
    asyncio.run(bot.check_invitations())
    assert len(bot.api.accepted) == 25


def test_optional_daily_accept_limit(sim, monkeypatch):  # noqa: F811
    bot = sim()
    _config(monkeypatch, max_accepts_per_day=1)
    monkeypatch.setattr(engine_module, "alert", lambda text: None)
    bot.api.invitations = [_invite("a", now_ms(-5)), _invite("b", now_ms(-5))]
    asyncio.run(bot.check_invitations())
    assert len(bot.api.accepted) == 1


def test_daily_reply_limit(sim, monkeypatch):  # noqa: F811
    from tests.test_engine_flow import settle

    bot = sim()
    _config(monkeypatch, max_replies_per_day=1)
    bot.api.add("t1")
    bot.api.add("t2")
    asyncio.run(settle(bot))
    assert len(bot.api.sent) == 1


def test_auth_error_stops_every_loop_and_backs_off(sim, monkeypatch):  # noqa: F811
    bot = sim()
    alerts = []
    monkeypatch.setattr(engine_module, "alert", alerts.append)

    async def recover():
        assert bot.state == "needs_login"  # Both loops are held while a person signs in.
        assert not await bot.may_call()
        bot.state = "running"

    bot.recover = recover
    monkeypatch.setattr(engine_module.asyncio, "sleep", _instant)
    asyncio.run(bot.on_error(AuthError("list invitations: HTTP 401"), "invitation check"))
    assert bot.backoff_until > time.time() + 250
    assert any("pushed back" in a for a in alerts)
    assert not asyncio.run(bot.may_call())  # Still backing off: no request.


def test_rate_limit_backs_off_longer_each_time(sim, monkeypatch):  # noqa: F811
    bot = sim()
    monkeypatch.setattr(engine_module, "alert", lambda text: None)
    asyncio.run(bot.on_error(RuntimeError("list threads: HTTP 429"), "inbox check"))
    first = bot.backoff_until - time.time()
    bot.backoff_until = 0
    asyncio.run(bot.on_error(RuntimeError("list threads: HTTP 999"), "inbox check"))
    assert bot.backoff_until - time.time() > first


async def _instant(*_):
    return None
