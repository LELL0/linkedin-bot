"""Enc Chat commands for the owner, against a fake bot agent. All data is synthetic."""

import asyncio
import http.server
import json
import threading
import time
from types import SimpleNamespace

import pytest

from bot import enc, health, tg_mirror


OWNER = "01HZX3K9M2Q4R5S6T7V8W9XYZA"  # a 26 character Enc user id


class FakeAgent:
    """The slice of the bot agent's API the bot uses: messages, commands, and an event stream."""

    def __init__(self, secret: str = "agent-secret") -> None:
        self.secret = secret
        self.sent: list[dict] = []
        self.commands: list[dict] | None = None
        self.events: list[dict] = []
        self.stream_calls = 0
        self.refuse_with = None
        agent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def authorized(self) -> bool:
                if self.headers.get("Authorization") == "Bearer " + agent.secret:
                    return True
                self.reply(401, {"code": "UNAUTHORIZED", "error": "wrong secret"})
                return False

            def reply(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def body(self):
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))

            def do_POST(self):
                if not self.authorized():
                    return
                payload = self.body()
                if self.path == "/v1/messages":
                    if agent.refuse_with:
                        self.reply(409, {"code": agent.refuse_with, "error": "no"})
                        return
                    agent.sent.append(payload)
                    self.reply(200, {"messageId": f"m{len(agent.sent)}"})
                else:
                    self.reply(404, {"code": "NOT_FOUND"})

            def do_PUT(self):
                if not self.authorized():
                    return
                agent.commands = self.body()
                self.reply(200, {"commands": agent.commands})

            def do_GET(self):
                if not self.authorized():
                    return
                agent.stream_calls += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for number, event in enumerate(agent.events, 1):
                    self.wfile.write(f"id: {number}\ndata: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.2)  # the stream drops; the client must reconnect and carry on

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class MemoryStore:
    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def get_meta(self, key: str, default: str = "") -> str:
        return self.meta.get(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


@pytest.fixture
def agent(monkeypatch):
    fake = FakeAgent()
    monkeypatch.setenv("ENC_BOT_URL", fake.url)
    monkeypatch.setenv("ENC_BOT_SECRET", fake.secret)
    monkeypatch.delenv("ENC_BOT_SECRET_FILE", raising=False)
    monkeypatch.setenv("ENC_OWNER", OWNER)
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    yield fake
    fake.close()


def control(monkeypatch):
    """A control whose send() records the text instead of reaching an agent."""
    monkeypatch.setenv("ENC_BOT_SECRET", "s")
    monkeypatch.setenv("ENC_OWNER", OWNER)
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    engine = SimpleNamespace(paused=False, replies_feed=False, state="running", last_ok=0,
                             accepted=1, replies_sent=2, store=MemoryStore())
    engine.set_paused = lambda on: setattr(engine, "paused", on)
    engine.set_replies_feed = lambda on: setattr(engine, "replies_feed", on)
    ec = enc.EncControl(engine)
    sent = []
    ec.mirrored = []  # what the Telegram mirror would get

    async def send(text, telegram=None):
        sent.append(text)
        ec.mirrored.append(text if telegram is None else telegram)

    ec.send = send
    return ec, engine, sent


def command(number, name, args="", sender=OWNER, username="me"):
    return {"type": "command", "id": f"e{number}", "chat": sender, "command": name,
            "args": args, "from": {"id": sender, "username": username, "name": "Me"}}


def test_pause_resume_and_replies_toggle(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    asyncio.run(ec.handle("/pause"))
    assert engine.paused is True
    asyncio.run(ec.handle("/resume"))
    assert engine.paused is False
    asyncio.run(ec.handle("/replies on"))
    assert engine.replies_feed is True
    asyncio.run(ec.handle("/replies"))
    assert engine.replies_feed is False
    asyncio.run(ec.handle("/status"))
    assert sent[-1].startswith("## Status: running\n- ")  # Enc Chat marks, never HTML
    assert "- Since start: 1 accepted, 2 replies" in sent[-1]
    assert "<b>" not in sent[-1]
    assert ec.mirrored[-1].startswith("State: *running*\n")  # Telegram keeps the old text


def test_reply_feed_quotes_with_marks_inert_and_only_when_on(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    asyncio.run(ec.reply_sent("Sam", ["*hi* ```x```"], "hey"))
    assert sent == []
    engine.replies_feed = True
    asyncio.run(ec.reply_sent("Sam", ["*hi* ```x```", "line one\n\n# not a heading"], "_ok_"))
    assert sent[0] == ("### Sam wrote\n> \u2217hi\u2217 '''x'''\n> line one\n> # not a heading\n"
                       "### You replied\n> \uff3fok\uff3f")
    # Telegram keeps the old layout.
    assert ec.mirrored[0].startswith("*Sam*:\n```\n*hi* '''x'''\nline one")


def test_reply_feed_name_cannot_add_marks_or_lines(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    engine.replies_feed = True
    asyncio.run(ec.reply_sent("_Sam_\n## Boss", ["hi"], "hey"))
    asyncio.run(ec.reply_sent("", ["hi"], "hey"))
    assert sent[0].startswith("### \uff3fSam\uff3f ## Boss wrote\n> hi\n")
    assert sent[1].startswith("### Someone wrote\n")


def test_events_become_commands_for_the_owner_only(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    run = lambda event: asyncio.run(ec.on_event(event))  # noqa: E731
    run(command(1, "pause"))
    assert engine.paused is True
    assert not hasattr(engine.store, "enc_owner") and "enc_owner" not in engine.store.meta
    before = len(sent)
    run(command(2, "resume", sender="01HZX3K9M2Q4R5S6T7V8W9XYZB", username="stranger"))
    assert engine.paused is True and len(sent) == before  # a stranger gets nothing, not a reply
    run(command(3, "replies", args="on"))
    assert engine.replies_feed is True
    run({"type": "message", "id": "e4", "from": {"id": OWNER}, "text": "hello"})
    run({"type": "started", "id": "e5", "from": {"id": OWNER}})
    assert sent[-1].startswith("## LinkedIn bot\n- */status* ")
    assert ec.mirrored[-1].startswith("*LinkedIn bot*\n- /status - ")


def test_events_repeat_is_dropped(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    toggles = []
    engine.set_replies_feed = lambda on: toggles.append(on)
    for _ in range(2):
        asyncio.run(ec.on_event(command(1, "replies")))  # no argument: a switch
    assert toggles == [True]


def test_owner_by_id_is_compared_to_the_sender_id_only(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    # The owner's id as a username, or someone else's id: refused.
    asyncio.run(ec.on_event(command(1, "pause", sender="01HZX3K9M2Q4R5S6T7V8W9XYZB",
                                    username=OWNER)))
    assert engine.paused is False
    asyncio.run(ec.on_event(command(2, "pause")))
    assert engine.paused is True


def test_owner_by_username_is_compared_to_the_sender_username_only(monkeypatch):
    monkeypatch.setenv("ENC_OWNER", "@Boss")
    ec, engine, sent = control(monkeypatch)
    monkeypatch.setenv("ENC_OWNER", "@Boss")
    ec = enc.EncControl(engine)
    ec.send = lambda text: asyncio.sleep(0)
    asyncio.run(ec.on_event(command(1, "pause", sender="boss", username="someone")))
    asyncio.run(ec.on_event(command(2, "pause", sender="u9", username="boss2")))
    assert engine.paused is False
    asyncio.run(ec.on_event(command(3, "pause", sender="u9", username="BOSS")))
    assert engine.paused is True


@pytest.mark.parametrize("value", [None, "", "boss", "me", "01HZX3K9M2Q4R5S6T7V8W9XYZ", "@",
                                   "01HZX3K9M2Q4R5S6T7V8W9XYZAB"])
def test_owner_unset_or_malformed_ignores_every_command(monkeypatch, caplog, value):
    ec, engine, sent = control(monkeypatch)
    if value is None:
        monkeypatch.delenv("ENC_OWNER")
    else:
        monkeypatch.setenv("ENC_OWNER", value)
    with caplog.at_level("WARNING"):
        ec = enc.EncControl(engine)
    assert sum("ENC_OWNER_MISSING" in r.getMessage() for r in caplog.records) == 1
    ec.send = lambda text: sent.append(text) or asyncio.sleep(0)
    for number, who in enumerate((OWNER, "me", "boss")):
        asyncio.run(ec.on_event(command(number, "pause", sender=who, username=who)))
    asyncio.run(ec.on_event({"type": "started", "id": "s", "from": {"id": OWNER}}))
    assert engine.paused is False and sent == []
    enc.logging.getLogger().removeHandler(ec.verbose_log)


def test_enc_is_off_without_a_secret(monkeypatch):
    monkeypatch.delenv("ENC_BOT_SECRET", raising=False)
    monkeypatch.delenv("ENC_BOT_SECRET_FILE", raising=False)
    assert enc.client_from_env() is None
    assert enc.notify("x") is False
    ec = enc.EncControl(SimpleNamespace(verbose=0))
    assert not ec.enabled
    asyncio.run(ec.loop())  # returns at once


def test_secret_may_come_from_a_file(monkeypatch, tmp_path):
    path = tmp_path / "secret"
    path.write_text("from-file\n")
    monkeypatch.delenv("ENC_BOT_SECRET", raising=False)
    monkeypatch.setenv("ENC_BOT_SECRET_FILE", str(path))
    monkeypatch.setenv("ENC_BOT_URL", "http://agent.test:1/")
    bot = enc.client_from_env()
    assert bot.secret == "from-file" and bot.url == "http://agent.test:1"
    monkeypatch.delenv("ENC_BOT_URL")
    assert enc.client_from_env().url == "http://enc-bot-agent:8787"


def test_send_posts_to_the_owner_with_the_secret(agent):
    engine = SimpleNamespace(verbose=0)
    ec = enc.EncControl(engine)
    asyncio.run(ec.send("*hello*"))
    assert agent.sent == [{"to": "owner", "text": "*hello*"}]
    assert "parse_mode" not in agent.sent[0] and "silent" not in agent.sent[0]
    agent.refuse_with = "BOT_NOT_STARTED"
    asyncio.run(ec.send("again"))  # a refusal is logged, never raised
    assert len(agent.sent) == 1


def test_alerts_go_through_enc_chat(agent):
    health._last_sent.clear()
    assert enc.notify("LinkedIn bot: signed out") is True
    assert agent.sent[-1]["text"] == "LinkedIn bot: signed out"
    agent.secret = "rotated"
    assert enc.notify("again") is False


def test_stream_commands_reach_the_engine_and_commands_are_registered(agent, monkeypatch):
    agent.events = [command(1, "pause"), command(2, "pause", sender="u2", username="x"),
                    command(3, "status")]
    engine = SimpleNamespace(paused=False, replies_feed=False, state="running", last_ok=0,
                             accepted=0, replies_sent=0, store=MemoryStore())
    engine.set_paused = lambda on: setattr(engine, "paused", on)
    ec = enc.EncControl(engine)

    async def run():
        task = asyncio.create_task(ec.loop())
        for _ in range(100):
            if len(agent.sent) >= 2 and agent.commands:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        ec.verbose_task.cancel()

    asyncio.run(run())
    assert engine.paused is True
    assert "Paused" in agent.sent[0]["text"] and "## Status: paused" in agent.sent[1]["text"]
    assert len(agent.sent) == 2  # the stranger got nothing; the reconnect did not repeat anything
    assert agent.stream_calls >= 1
    names = [c["command"] for c in agent.commands]
    assert names == [c for c, _ in enc.COMMANDS]
    enc.logging.getLogger().removeHandler(ec.verbose_log)


def test_telegram_mirror_sends_while_its_variables_are_set(agent, monkeypatch):
    posts = []
    monkeypatch.setattr(tg_mirror.httpx, "post", lambda url, **kw: posts.append((url, kw["json"])))
    ec = enc.EncControl(SimpleNamespace(verbose=0))
    asyncio.run(ec.send("*one*"))
    assert posts == []  # variables unset: Enc Chat only
    monkeypatch.setenv("TG_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_CHAT_ID", "42")
    asyncio.run(ec.send("*two*"))
    assert [m["text"] for m in agent.sent] == ["*one*", "*two*"]
    assert posts[0][1] == {"chat_id": "42", "text": "*two*", "disable_web_page_preview": True}
    assert "parse_mode" not in posts[0][1]


def test_verbose_sets_a_level_and_sends_only_its_lines(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    engine.verbose = 0
    engine.set_verbose = lambda level: setattr(engine, "verbose", level)
    asyncio.run(ec.handle("/verbose 2"))
    assert engine.verbose == 2 and "Verbose 2" in sent[-1] and "2 min" in sent[-1]
    log = enc.logging.getLogger("bot.engine")
    for text in ("Accepted invitation: {}", "Draft rejected: too long", "alive: accepted=1"):
        ec.verbose_log.emit(log.makeRecord("bot.engine", 20, __file__, 1, text, (), None))
    ec.verbose_log.emit(log.makeRecord("bot.session", 30, __file__, 1, "Accept failed", (), None))
    asyncio.run(ec.send_verbose())
    assert "Accepted invitation" in sent[-1] and "Accept failed" in sent[-1]
    assert "Draft rejected" not in sent[-1] and "alive" not in sent[-1]
    assert sent[-1].startswith("```") and sent[-1].endswith("```")
    count = len(sent)
    asyncio.run(ec.send_verbose())  # nothing new: no empty message
    assert len(sent) == count
    for wrong in ("/verbose loud", "/verbose 6"):
        asyncio.run(ec.handle(wrong))
        assert engine.verbose == 2 and "level from 0 to 5" in sent[-1]
    asyncio.run(ec.handle("/verbose"))
    assert sent[-1].startswith("## Verbose level 2\n")
    assert "\n- */verbose 2 - level 1" in sent[-1] and "\n- /verbose 3 - " in sent[-1]
    assert ec.mirrored[-1].startswith("Verbose level: *2*\n")
    asyncio.run(ec.handle("/verbose off"))
    assert engine.verbose == 0
    asyncio.run(ec.handle("/status"))
    assert "Verbose: 0" in sent[-1]
    enc.logging.getLogger().removeHandler(ec.verbose_log)
    enc.apply_log_level(0)


def test_record_levels_and_telegram_token_never_sent(monkeypatch):
    make = enc.logging.getLogger("x").makeRecord

    def level(name, levelno, text):
        return enc.record_level(make(name, levelno, __file__, 1, text, (), None))

    assert level("bot.session", 40, "boom") == 1
    assert level("bot.engine", 20, "Replied: x") == 2
    assert level("bot.brain", 20, "Draft blocked: x") == 3
    assert level("bot.engine", 20, "alive: accepted=0") == 4
    assert level("bot.engine", 10, "detail") == 5
    assert level("playwright", 20, "x") == 5
    assert level("httpx", 40, "POST https://api.telegram.org/botSECRET/x") == 6
    ec, engine, sent = control(monkeypatch)
    engine.verbose = 5
    monkeypatch.setenv("TG_BOT_TOKEN", "123:FAKETOKEN")
    ec.verbose_log.lines.append((1, "url /bot123:FAKETOKEN/x"))
    asyncio.run(ec.send_verbose())
    assert "***" in sent[-1] and "FAKETOKEN" not in sent[-1]
    enc.logging.getLogger().removeHandler(ec.verbose_log)


def test_catchup_command_checks_days_and_stop(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    started = []
    engine.start_catchup = lambda days, report: started.append(days) or True
    engine.stop_catchup = lambda: False
    asyncio.run(ec.handle("/catchup"))
    asyncio.run(ec.handle("/catchup 99"))
    assert started == [] and "1 to 30" in sent[-1]
    asyncio.run(ec.handle("/catchup 3"))
    assert started == [3.0]
    asyncio.run(ec.handle("/catchup stop"))
    assert "No catch-up" in sent[-1]


def test_commands_fit_the_agent_rules():
    import re
    for name, description in enc.COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name) and 1 <= len(description) <= 120


def test_telegram_mirror_keeps_the_old_texts(agent, monkeypatch):
    posts = []
    monkeypatch.setattr(tg_mirror.httpx, "post", lambda url, **kw: posts.append(kw["json"]["text"]))
    monkeypatch.setenv("TG_BOT_TOKEN", "t")
    monkeypatch.setenv("TG_CHAT_ID", "42")
    engine = SimpleNamespace(paused=False, replies_feed=False, state="running", last_ok=0,
                             accepted=0, replies_sent=0, verbose=0)
    engine.set_paused = lambda on: setattr(engine, "paused", on)
    ec = enc.EncControl(engine)
    for text in ("/help", "/status", "/settings", "/pause"):
        asyncio.run(ec.handle(text))
    enc_texts = [m["text"] for m in agent.sent]
    assert enc_texts[0] == enc.HELP and posts[0] == enc.HELP_TELEGRAM
    assert enc_texts[1].startswith("## Status: running") and posts[1].startswith("State: *running*")
    assert enc_texts[2].startswith("## Settings\n- *poll* = ")
    assert posts[2].startswith("*Settings* (a star marks one changed from the default)\n```")
    assert enc_texts[3] == posts[3] and enc_texts[3].startswith("Paused")  # unchanged texts
    enc.logging.getLogger().removeHandler(ec.verbose_log)


def test_layouts_are_short_lines_and_lists(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    lines = enc.HELP.split("\n")
    assert lines[0] == "## LinkedIn bot" and len(lines) == len(enc.COMMANDS) + 1
    assert all(line.startswith("- */") for line in lines[1:])
    asyncio.run(ec.handle("/stats"))
    assert sent[-1].startswith("## Stats\n```\n") and sent[-1].endswith("\n```")
    assert ec.mirrored[-1].startswith("```\n")
    asyncio.run(ec.handle("/verbose loud"))
    assert sent[-1].startswith("Use /verbose with a level from 0 to 5.\n## Verbose level 0\n")


def test_set_error_quoting_the_owner_stays_inert(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    engine.change_setting = lambda name, value: None
    asyncio.run(ec.handle("/set poll *x*"))
    assert sent[-1] == "Not changed: '\u2217x\u2217' is not a number."
    assert ec.mirrored[-1] == "Not changed: '*x*' is not a number."


def test_alert_text_is_a_heading_and_one_safe_line():
    assert enc.alert_text("Signed out or checkpoint.") == "## Alert\nSigned out or checkpoint."
    text = enc.alert_text("- pushed back\n```\n_boom_ [x](https://e.com)")
    assert text == "## Alert\n\u200b- pushed back ''' \uff3fboom\uff3f [x] (https://e.com)"


def test_catchup_text_lists_names_safely_and_counts_the_rest():
    text = enc.catchup_text(["Sam", "*Lynn*", "1. Ana\n# x", ""], 3, (600.0, 1800.0))
    assert text.split("\n") == [
        "## Catch-up: 4 conversation(s)",
        "Unanswered messages from the last 3 days:",
        "1. Sam",
        "2. \u2217Lynn\u2217",
        "3. 1. Ana # x",
        "4. ?",
        "Answering one every 10-30 min. /catchup stop cancels.",
    ]
    many = enc.catchup_text([f"P{n}" for n in range(25)], 1.5, (60.0, 60.0))
    assert "20. P19" in many and "21." not in many and "…and 5 more." in many
