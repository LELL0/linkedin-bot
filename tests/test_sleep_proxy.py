"""Chrome only opens in active hours and through the proxy, and closes for the night."""

import asyncio
import dataclasses
import socket

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
    conf = dataclasses.replace(CONFIG, wake_jitter_s=(0.0, 0.0), **config)
    monkeypatch.setattr(engine_module, "CONFIG", conf)
    alerts: list[str] = []
    monkeypatch.setattr(engine_module, "alert", alerts.append)
    monkeypatch.setattr(engine_module, "write_status", lambda *a, **k: None)
    bot = Engine.__new__(Engine)
    bot.session = FakeSession()
    bot.state, bot.last_ok, bot.accepted, bot.replies_sent = "starting", 0.0, 0, 0
    bot.workers, bot.catchup_task, bot.started = {}, None, True

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


def test_chrome_opens_again_in_the_morning(monkeypatch):
    bot, _ = make_bot(monkeypatch, proxy="")
    bot.state = "asleep"
    monkeypatch.setattr(engine_module, "in_active_hours", lambda: True)

    async def idle(state, seconds):
        pass

    bot.idle = idle
    run_browser_loop_once(monkeypatch, bot)
    assert bot.session.starts == 1 and bot.state == "running"
