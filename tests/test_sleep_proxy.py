"""Chrome only opens in active hours and through the proxy, only when a check needs it, and
closes between checks and for the night."""

import asyncio
import dataclasses
import socket
import time
from types import SimpleNamespace

from bot import engine as engine_module
from bot import session as session_module
from bot.config import CONFIG
from bot.engine import Engine


class FakeSession:
    def __init__(self):
        self.open = False
        self.starts = 0
        self.stops = 0

    async def start(self):
        self.starts += 1
        self.open = True

    async def stop(self):
        self.stops += 1
        self.open = False


def make_bot(monkeypatch, **config):
    conf = dataclasses.replace(CONFIG, **config)
    monkeypatch.setattr(engine_module, "CONFIG", conf)
    alerts: list[str] = []
    monkeypatch.setattr(engine_module, "alert", lambda *args: alerts.append(" ".join(args)))
    monkeypatch.setattr(engine_module, "write_status", lambda *a, **k: None)
    bot = Engine.__new__(Engine)
    bot.session = FakeSession()
    bot.state, bot.last_ok, bot.accepted, bot.replies_sent = "starting", 0.0, 0, 0
    bot.workers, bot.catchup_task, bot.started = {}, None, True
    bot.busy, bot.watch_until, bot.paused, bot.backoff_until = 0, 0.0, False, 0.0
    bot.next_check = bot.next_invites = time.time() + 3 * 3600
    bot.opening = asyncio.Lock()
    meta: dict[str, str] = {}
    bot.store = SimpleNamespace(get_meta=lambda key, default="": meta.get(key, default),
                                set_meta=meta.__setitem__)
    bot.capped = set()

    async def signed_in():
        bot.state = "running"

    bot.wait_for_session = signed_in
    return bot, alerts


def test_proxy_problem_reports_a_closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # Bound but not listening: connections are refused.
        assert "does not answer" in session_module.proxy_problem(f"socks5://127.0.0.1:{port}", 1)


def test_proxy_problem_accepts_a_listening_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        assert session_module.proxy_problem(f"socks5://127.0.0.1:{sock.getsockname()[1]}") == ""


def test_proxy_problem_refuses_a_malformed_value():
    assert "must look like" in session_module.proxy_problem("172.17.0.1:1080")


def test_webrtc_is_kept_inside_the_proxy():
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in \
        session_module.browser_args("socks5://h:1", chromium=True)
    assert not any("webrtc" in a for a in session_module.browser_args("", chromium=True))


def test_wake_waits_for_the_proxy_and_never_opens_without_it(monkeypatch):
    bot, alerts = make_bot(monkeypatch, proxy="socks5://172.17.0.1:1080", active_hours=(0.0, 24.0))
    answers = iter(["down", "down", ""])
    monkeypatch.setattr(engine_module, "proxy_problem", lambda proxy: next(answers))
    waits: list[str] = []

    async def idle(state, seconds):
        assert not bot.session.open
        waits.append(state)

    bot.idle = idle
    asyncio.run(bot.wake())
    assert waits == ["no_proxy", "no_proxy"]
    assert bot.session.starts == 1 and bot.state == "running"
    assert len(alerts) == 1


def test_wake_waits_for_active_hours(monkeypatch):
    bot, _ = make_bot(monkeypatch, proxy="")
    hours = iter([False, False, True])
    monkeypatch.setattr(engine_module, "in_active_hours", lambda: next(hours))
    waits: list[str] = []

    async def idle(state, seconds):
        waits.append(state)

    bot.idle = idle
    asyncio.run(bot.wake())
    assert waits == ["asleep", "asleep"] and bot.session.starts == 1


def run_browser_loop_once(monkeypatch, bot):
    async def one_tick(seconds):
        if one_tick.done:
            raise asyncio.CancelledError
        one_tick.done = True

    one_tick.done = False
    monkeypatch.setattr(engine_module.asyncio, "sleep", one_tick)
    try:
        asyncio.run(bot.browser_loop())
    except asyncio.CancelledError:
        pass


def test_chrome_closes_at_night(monkeypatch):
    bot, _ = make_bot(monkeypatch, proxy="")
    bot.state, bot.session.open = "running", True
    monkeypatch.setattr(engine_module, "in_active_hours", lambda: False)
    run_browser_loop_once(monkeypatch, bot)
    assert bot.session.stops == 1 and bot.state == "asleep"


def test_chrome_stays_open_while_a_reply_is_pending(monkeypatch):
    bot, _ = make_bot(monkeypatch, proxy="")
    bot.state, bot.session.open, bot.workers = "running", True, {"t": object()}
    monkeypatch.setattr(engine_module, "in_active_hours", lambda: False)
    run_browser_loop_once(monkeypatch, bot)
    assert bot.session.stops == 0 and bot.state == "running"


def open_in_the_day(monkeypatch, **fields):
    bot, _ = make_bot(monkeypatch, proxy="")
    bot.state, bot.session.open = "running", True
    for name, value in fields.items():
        setattr(bot, name, value)
    monkeypatch.setattr(engine_module, "in_active_hours", lambda: True)
    run_browser_loop_once(monkeypatch, bot)
    return bot


def test_chrome_closes_between_checks(monkeypatch):
    bot = open_in_the_day(monkeypatch)
    assert bot.session.stops == 1 and bot.state == "resting"


def test_chrome_stays_open_while_a_chat_is_followed(monkeypatch):
    bot = open_in_the_day(monkeypatch, watch_until=time.time() + 3600)
    assert bot.session.stops == 0 and bot.state == "running"


def test_chrome_stays_open_during_a_check(monkeypatch):
    bot = open_in_the_day(monkeypatch, busy=1)
    assert bot.session.stops == 0


def test_chrome_stays_open_when_the_next_round_is_minutes_away(monkeypatch):
    bot = open_in_the_day(monkeypatch, next_invites=time.time() + 300)
    assert bot.session.stops == 0


def test_chrome_closes_when_a_chat_ended(monkeypatch):
    bot = open_in_the_day(monkeypatch, watch_until=time.time() - 1)
    assert bot.session.stops == 1


def ready_bot(monkeypatch, hours=True, **fields):
    bot, _ = make_bot(monkeypatch, proxy="")
    for name, value in fields.items():
        setattr(bot, name, value)
    monkeypatch.setattr(engine_module, "in_active_hours", lambda: hours)
    calls = []

    async def may_call():  # Lets ready() go round twice, then return.
        calls.append(bot.session.open)
        return len(calls) > 1

    bot.may_call = may_call
    asyncio.run(bot.ready())
    return bot


def test_ready_opens_chrome_for_a_check(monkeypatch):
    bot = ready_bot(monkeypatch)
    assert bot.session.starts == 1 and bot.state == "running"


def test_ready_never_opens_chrome_while_paused_backing_off_or_at_night(monkeypatch):
    assert ready_bot(monkeypatch, paused=True).session.starts == 0
    assert ready_bot(monkeypatch, backoff_until=time.time() + 600).session.starts == 0
    assert ready_bot(monkeypatch, hours=False).session.starts == 0


def test_lock_from_a_gone_container_is_removed(tmp_path):
    import os
    os.symlink("oldcontainer-32", tmp_path / "SingletonLock")
    os.symlink("x", tmp_path / "SingletonCookie")
    assert session_module.clear_stale_lock(tmp_path)
    assert not os.path.lexists(tmp_path / "SingletonLock")
    assert not os.path.lexists(tmp_path / "SingletonCookie")


def test_own_lock_is_left_alone(tmp_path):
    import os
    os.symlink(f"{socket.gethostname()}-32", tmp_path / "SingletonLock")
    assert not session_module.clear_stale_lock(tmp_path)
    assert os.path.lexists(tmp_path / "SingletonLock")


def test_no_lock_is_fine(tmp_path):
    assert not session_module.clear_stale_lock(tmp_path)


def test_without_a_proxy_chrome_ignores_the_system_proxy():
    assert "--no-proxy-server" in session_module.browser_args("", chromium=False)
    assert "--no-proxy-server" not in session_module.browser_args("socks5://h:1", chromium=False)
