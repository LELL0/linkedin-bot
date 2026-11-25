"""Enc Chat commands for the owner, against a fake bot agent. All data is synthetic."""

import ast
import asyncio
import http.server
import json
import pathlib
import re
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot import enc, health
from bot.marks import BLOCK_START


OWNER = "01HZX3K9M2Q4R5S6T7V8W9XYZA"  # a 26 character Enc user id
DIVIDER = re.compile(r" *(?:-{3,}|\*{3,}|_{3,}) *")
TITLE = re.compile(r"(?P<emoji>🟢|🟡|🔴|✅|❌|🆕|ℹ️|⏳) \*LinkedIn · (?P<state>[^*\n]+)\*"
                   r"(?: · (?P<facts>.+))?")
ONE_LINERS = ("Use /", "I don't know ", "I only take commands")
PROBLEM = re.compile(r"🟡|🔴|❌|fail|down|not sent|sign-in|limit", re.IGNORECASE)


def assert_style(text: str, max_lines: int) -> None:
    """The Enc Chat bot style (sdk/node/README.md "Keep bot messages small", v2 of
    2026-11-25): a title line `<emoji> *LinkedIn · state*` with at most three facts, or a
    one-line usage or refusal; every line at most 70 characters, a part's line at most three
    facts; no heading, blank line, divider or code box; never 🟢 on a message that lists a
    problem; and no more lines than the message's budget."""
    lines = text.split("\n")
    assert len(lines) <= max_lines, text
    assert all(len(line) <= enc.LINE_MAX for line in lines), \
        [line for line in lines if len(line) > enc.LINE_MAX]
    assert all(line.strip() for line in lines), f"blank line in {text!r}"
    assert not any(line.lstrip().startswith("#") for line in lines), f"heading in {text!r}"
    assert not any(DIVIDER.fullmatch(line) for line in lines), f"divider in {text!r}"
    assert "```" not in text, f"code box in {text!r}"
    if lines[0].startswith(ONE_LINERS):
        assert len(lines) == 1, text
        return
    title = TITLE.fullmatch(lines[0])
    assert title, f"not a title: {lines[0]!r}"
    assert (title["facts"] or "").count(enc.DOT) <= 2, lines[0]
    assert all(line.count(enc.DOT) <= 2 for line in lines[1:] if line.startswith("*")), text
    if title["emoji"] == "🟢":
        assert not PROBLEM.search(text), f"🟢 on a problem: {text!r}"


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
    yield fake
    fake.close()


def control(monkeypatch):
    """A control whose send() records the text instead of reaching an agent."""
    monkeypatch.setenv("ENC_BOT_SECRET", "s")
    monkeypatch.setenv("ENC_OWNER", OWNER)
    engine = SimpleNamespace(paused=False, replies_feed=False, state="running", last_ok=0,
                             accepted=1, replies_sent=2, store=MemoryStore())
    engine.set_paused = lambda on: setattr(engine, "paused", on)
    engine.set_replies_feed = lambda on: setattr(engine, "replies_feed", on)
    ec = enc.EncControl(engine)
    sent = []

    async def send(text):
        sent.append(text)

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
    lines = sent[-1].split("\n")  # Enc Chat marks, never HTML
    assert lines[0] == "🟢 *LinkedIn · running* · no check yet"
    assert lines[1].startswith("*Inbox* every ")
    assert lines[2] == "*Since start* 1 accept · 2 replies"
    assert lines[3] == "*In this chat* replies off · log off"
    assert "<b>" not in sent[-1]
    assert_style(sent[-1], 4)
    engine.last_ok = time.time() - 300
    asyncio.run(ec.handle("/status"))
    assert sent[-1].startswith("🟢 *LinkedIn · running* · checked 5m ago\n")


def test_pause_and_resume_say_when_nothing_changes(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    asyncio.run(ec.handle("/resume"))
    assert sent[-1] == "ℹ️ *LinkedIn · not paused* · /status shows what it does"
    asyncio.run(ec.handle("/pause"))
    assert sent[-1] == ("✅ *LinkedIn · paused* · no accepts or replies until /resume\n"
                        "Messages meanwhile get answered then, unless you reply first")
    asyncio.run(ec.handle("/pause"))
    assert sent[-1] == "ℹ️ *LinkedIn · already paused* · /resume starts it again"
    asyncio.run(ec.handle("/status"))
    assert sent[-1].startswith("🟡 *LinkedIn · paused* · /resume starts it again\n"
                               "*Inbox* no check yet · every ")


@pytest.mark.parametrize("state, first", [
    ("needs_login", "🔴 *LinkedIn · sign-in needed* · sign in again over VNC"),
    ("no_proxy", "🔴 *LinkedIn · home tunnel down* · start it so LinkedIn can open"),
    ("asleep", "ℹ️ *LinkedIn · asleep* · no check yet"),
    ("starting", "⏳ *LinkedIn · starting* · no check yet"),
    ("*odd*", "🟡 *LinkedIn · \u2217odd\u2217* · no check yet"),
])
def test_status_emoji_matches_the_state(monkeypatch, state, first):
    ec, engine, sent = control(monkeypatch)
    engine.state = state
    asyncio.run(ec.handle("/status"))
    assert sent[-1].split("\n")[0] == first
    assert_style(sent[-1], 5)


def test_status_in_dry_run_is_not_green(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    monkeypatch.setattr(enc.settings.CONFIG, "dry_run", True)
    asyncio.run(ec.handle("/status"))
    lines = sent[-1].split("\n")
    assert lines[0] == "🟡 *LinkedIn · running, dry run* · no check yet"
    assert lines[1] == "Nothing is sent or accepted · /set dry_run off"
    assert_style(sent[-1], 5)


def test_reply_feed_quotes_with_marks_inert_and_only_when_on(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    asyncio.run(ec.reply_sent("Sam", ["*hi* ```x```"], "hey"))
    assert sent == []
    engine.replies_feed = True
    asyncio.run(ec.reply_sent("Sam", ["*hi* ```x```", "line one\n\n# not a heading"], "_ok_"))
    # One quoted line per short message, the reply after: nothing from outside adds a mark.
    assert sent[0] == ("🆕 *LinkedIn · replied to Sam*\n> \u2217hi\u2217 '''x'''\n"
                       "> line one # not a heading\n↩️ *Reply* \uff3fok\uff3f")
    assert_style(sent[0], 4)


def test_reply_feed_name_cannot_add_marks_or_lines(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    engine.replies_feed = True
    asyncio.run(ec.reply_sent("_Sam_\n## Boss", ["hi"], "hey"))
    asyncio.run(ec.reply_sent("", ["hi"], "hey"))
    asyncio.run(ec.reply_sent("Ana", ["", " "], "line one\n- line two"))
    assert sent[0] == "🆕 *LinkedIn · replied to \uff3fSam\uff3f ## Boss*\n> hi\n↩️ *Reply* hey"
    assert sent[1].startswith("🆕 *LinkedIn · replied to someone*\n")
    assert sent[2] == "🆕 *LinkedIn · replied to Ana*\n> …\n↩️ *Reply* line one - line two"


def test_reply_feed_wraps_long_text_into_short_lines(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    engine.replies_feed = True
    long = " ".join(f"word{n}" for n in range(200))
    reply = "Sure, " + "this is a fairly long reply " * 4 + "- 1. # > ```"
    asyncio.run(ec.reply_sent("Sam", [long, "short", long, long], reply))
    lines = sent[0].split("\n")
    quotes = [line for line in lines if line.startswith("> ")]
    assert len(quotes) == enc.QUOTES and quotes[-1] == "> …"
    assert quotes[0].startswith("> word0 word1 ") and quotes[2].endswith("…")
    assert quotes[3] == "> short"
    at = next(n for n, line in enumerate(lines) if line.startswith("↩️ *Reply* Sure, "))
    assert 1 < len(lines) - at <= enc.REPLY_LINES
    assert not any(BLOCK_START.match(line) for line in lines[at + 1:])  # no list or heading
    assert_style(sent[0], 1 + enc.QUOTES + enc.REPLY_LINES)


def test_wrap_keeps_lines_short_and_marks_a_cut():
    assert enc.wrap("a b c", 70, 3) == ["a b c"]
    assert enc.wrap("aaaa bbbb cccc", 9, 3) == ["aaaa bbbb", "cccc"]
    assert enc.wrap("x" * 25, 10, 2) == ["x" * 10, "x" * 9 + "…"]
    assert enc.wrap("aaaa bbbb cccc", 9, 2, first=4) == ["aaaa", "bbbb cccc"]
    assert enc.wrap("aaaa bbbb cccc dddd", 9, 1, first=6) == ["aaaa…"]


def test_events_become_commands_for_the_owner_only(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    run = lambda event: asyncio.run(ec.on_event(event))  # noqa: E731
    run(command(1, "pause"))
    assert engine.paused is True
    assert not hasattr(engine.store, "enc_owner") and "enc_owner" not in engine.store.meta
    before = len(sent)
    run(command(2, "resume", sender="01HZX3K9M2Q4R5S6T7V8W9XYZB", username="stranger"))
    run({"type": "message", "id": "e2b", "from": {"id": "01HZX3K9M2Q4R5S6T7V8W9XYZB"},
         "text": "hi"})
    assert engine.paused is True and len(sent) == before  # a stranger gets nothing, not a reply
    run(command(3, "replies", args="on"))
    assert engine.replies_feed is True
    run({"type": "message", "id": "e4", "from": {"id": OWNER}, "text": "*hello*"})
    assert sent[-1] == "I only take commands · /help lists them"
    run({"type": "button", "id": "e4b", "from": {"id": OWNER}, "data": "x"})
    run({"type": "started", "id": "e5", "from": {"id": OWNER}})
    assert sent[-1] == enc.HELP and enc.HELP.startswith("ℹ️ *LinkedIn · help* · ")
    assert sent[-2] == "I only take commands · /help lists them"


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
    asyncio.run(ec.on_event({"type": "message", "id": "m", "from": {"id": OWNER}}))
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
    assert agent.sent[0]["text"].startswith(
        "✅ *LinkedIn · paused* · no accepts or replies until /resume")
    status = agent.sent[1]["text"]
    assert status.startswith("🟡 *LinkedIn · paused* · ") and "\n*Since start* " in status
    assert len(agent.sent) == 2  # the stranger got nothing; the reconnect did not repeat anything
    assert agent.stream_calls >= 1
    # The "/" menu is exactly the /help list: same names, same words, same order.
    assert agent.commands == [{"command": name, "description": text}
                              for name, _, text in enc.COMMANDS]
    enc.logging.getLogger().removeHandler(ec.verbose_log)


def test_help_lists_exactly_the_menu():
    lines = enc.HELP.split("\n")
    listed = [re.fullmatch(r"/([a-z_]+)(?: [^·]+)? · (.+)", line).groups() for line in lines[1:]]
    assert listed == [(name, text) for name, _, text in enc.COMMANDS]
    assert lines[0] == "ℹ️ *LinkedIn · help* · I accept invites and answer messages for you"
    assert "/verbose 0-5 · " in enc.HELP and "/catchup 1-30 · " in enc.HELP
    assert_style(enc.HELP, 1 + len(enc.COMMANDS))


def test_commands_fit_the_agent_rules():
    names = [name for name, _, _ in enc.COMMANDS]
    assert len(set(names)) == len(names) and not set(names) & set(enc.HIDDEN)
    for name, argument, description in enc.COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name)
        assert 1 <= len(description) <= 35 and len(description) <= 120
        assert description[0].isupper() and not description.endswith(".")
        assert "  " not in argument and argument == argument.strip()


def test_hidden_old_names_still_work(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    changes = []
    engine.change_setting = lambda name, value: changes.append((name, value))
    asyncio.run(ec.handle("/start"))
    assert sent[-1] == enc.HELP
    asyncio.run(ec.handle("/interval 6-12"))
    assert changes == [("poll", (360.0, 720.0))]
    assert "/interval" not in enc.HELP and "/start" not in enc.HELP


def test_unknown_command_is_one_line_and_inert(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    asyncio.run(ec.handle("/nope"))
    assert sent[-1] == "I don't know /nope · /help lists what I can do"
    asyncio.run(ec.handle("/*x*"))
    assert sent[-1] == "I don't know /\u2217x\u2217 · /help lists what I can do"


def test_nothing_goes_to_telegram_even_with_its_old_variables_set(agent, monkeypatch):
    import httpx

    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(health.shutil, "which", lambda name: None)
    monkeypatch.setenv("TG_BOT_TOKEN", "123:FAKETOKEN")
    monkeypatch.setenv("TG_CHAT_ID", "42")
    ec = enc.EncControl(SimpleNamespace(verbose=0))
    asyncio.run(ec.send("*one*"))
    health._last_sent.clear()
    health.alert("signed out")
    assert calls == [] and len(agent.sent) == 2  # both went to Enc Chat only
    source = " ".join(p.read_text() for p in pathlib.Path(enc.__file__).parent.glob("*.py"))
    assert "api.telegram.org" not in source and "TG_BOT_TOKEN" not in source
    enc.logging.getLogger().removeHandler(ec.verbose_log)


def test_verbose_sets_a_level_and_sends_only_its_lines(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    engine.verbose = 0
    engine.set_verbose = lambda level: setattr(engine, "verbose", level)
    asyncio.run(ec.handle("/verbose 2"))
    assert engine.verbose == 2
    assert sent[-1] == ("✅ *LinkedIn · log level 2* · warnings, accepts and replies\n"
                        "Sent every 2m · /verbose 0 stops it")
    log = enc.logging.getLogger("bot.engine")
    for text in ("Accepted invitation: {}", "Draft rejected: too long", "alive: accepted=1"):
        ec.verbose_log.emit(log.makeRecord("bot.engine", 20, __file__, 1, text, (), None))
    ec.verbose_log.emit(log.makeRecord("bot.session", 30, __file__, 1, "Accept failed", (), None))
    asyncio.run(ec.send_verbose())
    lines = sent[-1].split("\n")
    assert lines[0] == "🟡 *LinkedIn · log, 1 problem* · 2 lines"
    assert re.fullmatch(r"- \d\d:\d\d Accepted invitation: \{\}", lines[1])
    assert re.fullmatch(r"- 🟡 \d\d:\d\d Accept failed", lines[2])
    assert "Draft rejected" not in sent[-1] and "alive" not in sent[-1]
    assert_style(sent[-1], 3)
    count = len(sent)
    asyncio.run(ec.send_verbose())  # nothing new: no empty message
    assert len(sent) == count
    for wrong in ("/verbose loud", "/verbose 6"):
        asyncio.run(ec.handle(wrong))
        assert engine.verbose == 2 and sent[-1] == "Use /verbose 0-5 · it stays at 2"
    asyncio.run(ec.handle("/verbose"))
    assert sent[-1] == ("ℹ️ *LinkedIn · log level 2* · warnings, accepts and replies\n"
                        "*Levels* 0 off · 1 warnings · 2 + accepts, replies\n"
                        "*More levels* 3 all but heartbeat · 4 + heartbeat · 5 + debug\n"
                        "/verbose 0-5 changes it · sent every 2m")
    asyncio.run(ec.handle("/verbose off"))
    assert engine.verbose == 0
    assert sent[-1] == "✅ *LinkedIn · log off* · only alerts come here"
    asyncio.run(ec.handle("/status"))
    assert "· log off" in sent[-1]
    enc.logging.getLogger().removeHandler(ec.verbose_log)
    enc.apply_log_level(0)


def test_record_levels(monkeypatch):
    make = enc.logging.getLogger("x").makeRecord

    def level(name, levelno, text):
        return enc.record_level(make(name, levelno, __file__, 1, text, (), None))

    assert level("bot.session", 40, "boom") == 1
    assert level("bot.engine", 20, "Replied: x") == 2
    assert level("bot.brain", 20, "Draft blocked: x") == 3
    assert level("bot.engine", 20, "alive: accepted=0") == 4
    assert level("bot.engine", 10, "detail") == 5
    assert level("playwright", 20, "x") == 5
    assert level("httpx", 40, "POST https://ai.example/v1/chat") == 6


def test_log_lines_are_short_one_line_items():
    import sys

    log = enc.logging.getLogger("bot.engine")
    try:
        raise ValueError("bad *thing*\nsecond line")
    except ValueError:
        record = log.makeRecord("bot.engine", 40, __file__, 1, "Check failed", (), sys.exc_info())
    line = enc.ChatLogFormatter().format(record)
    assert re.fullmatch(r"🔴 \d\d:\d\d Check failed \(ValueError: bad \*thing\*\n"
                        r"second line\)", line)
    # The time is the owner's (Beirut, UTC+2 in November); another library keeps its name.
    other = log.makeRecord("playwright", 20, __file__, 1, "page loaded", (), None)
    other.created = datetime(2026, 11, 25, 16, 40, tzinfo=timezone.utc).timestamp()
    assert enc.ChatLogFormatter().format(other) == "18:40 playwright: page loaded"
    text = enc.verbose_message([line, "- # **x**", "y" * 500])
    lines = text.split("\n")
    assert lines[0] == "🟡 *LinkedIn · log, 1 problem* · 3 lines" and len(lines) == 4
    assert lines[1].startswith("- 🔴 ") and "\u2217thing\u2217 second line" in lines[1]
    assert lines[2] == "- - # \u2217\u2217x\u2217\u2217"  # inert, on its line
    assert len(lines[3]) == enc.LINE_MAX and lines[3].endswith("…")
    cut = enc.verbose_message([f"line {n}" for n in range(1000)], limit=100)
    assert cut.startswith("ℹ️ *LinkedIn · log* · 9 lines · 991 older left out\n- line 991\n")
    assert_style(cut, 10)


def test_catchup_command_checks_days_and_stop(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    started = []
    engine.start_catchup = lambda days, report: started.append(days) or True
    engine.stop_catchup = lambda: None
    asyncio.run(ec.handle("/catchup"))
    asyncio.run(ec.handle("/catchup 99"))
    assert started == [] and sent[-1] == "Use /catchup 1-30 (days back) or /catchup stop"
    asyncio.run(ec.handle("/catchup 3"))
    assert started == [3.0] and sent[-1] == "⏳ *LinkedIn · catch-up* · looking back 3 days…"
    asyncio.run(ec.handle("/catchup 1"))
    assert sent[-1].endswith(" · looking back 1 day…")
    asyncio.run(ec.handle("/catchup stop"))
    assert sent[-1] == "ℹ️ *LinkedIn · no catch-up running*"
    engine.stop_catchup = lambda: (2, 5)
    asyncio.run(ec.handle("/catchup stop"))
    assert sent[-1] == "✅ *LinkedIn · catch-up stopped* · 2 of 5 answered"
    engine.stop_catchup = lambda: (0, 0)
    asyncio.run(ec.handle("/catchup stop"))
    assert sent[-1] == "✅ *LinkedIn · catch-up stopped* · before it began"
    engine.start_catchup = lambda days, report: False
    asyncio.run(ec.handle("/catchup 3"))
    assert sent[-1] == "❌ *LinkedIn · catch-up already running* · /catchup stop ends it"


def test_commands_reach_enc_chat_through_the_agent(agent, monkeypatch):
    engine = SimpleNamespace(paused=False, replies_feed=False, state="running", last_ok=0,
                             accepted=0, replies_sent=0, verbose=0)
    engine.set_paused = lambda on: setattr(engine, "paused", on)
    ec = enc.EncControl(engine)
    for text in ("/help", "/status", "/settings", "/pause"):
        asyncio.run(ec.handle(text))
    enc_texts = [m["text"] for m in agent.sent]
    assert enc_texts[0] == enc.HELP
    assert enc_texts[1].startswith("🟢 *LinkedIn · running* · ")
    assert enc_texts[2].startswith("ℹ️ *LinkedIn · settings* · ")
    assert "\n*Inbox* poll " in enc_texts[2]
    assert enc_texts[3].startswith("✅ *LinkedIn · paused*")
    enc.logging.getLogger().removeHandler(ec.verbose_log)


# Every command, and the line budget of its answer.
EVERY_COMMAND = [
    ("/help", 1 + len(enc.COMMANDS)), ("/status", 4), ("/stats", 1), ("/pause", 2),
    ("/pause", 1), ("/status", 4), ("/resume", 1), ("/resume", 1), ("/replies on", 1),
    ("/replies off", 1), ("/replies loud", 1), ("/verbose", 4), ("/verbose 2", 2),
    ("/verbose 9", 1), ("/verbose 0", 1), ("/catchup", 1), ("/catchup 3", 1), ("/catchup 3", 1),
    ("/catchup stop", 1), ("/catchup stop", 1), ("/settings", 8), ("/set", 1), ("/set nope", 1),
    ("/set poll", 2), ("/set poll 6-12", 2), ("/set poll x", 1), ("/set poll 0.2", 1),
    ("/set max_replies 2.5", 1), ("/set dry_run on", 2), ("/status", 5), ("/settings", 9),
    ("/set dry_run off", 2), ("/set scope all", 2), ("/interval 5-15", 2), ("/reset", 1),
    ("/reset nope", 1), ("/reset poll", 1), ("/reset all", 1), ("/nope", 1), ("/start", 12),
]


def test_every_answer_follows_the_style(monkeypatch, tmp_path):
    import dataclasses

    from bot import settings

    ec, engine, sent = control(monkeypatch)
    engine.verbose = 0
    engine.set_verbose = lambda level: setattr(engine, "verbose", level)
    engine.change_setting = lambda name, value: (settings.reset(name) if value is None
                                                 else settings.apply(name, value))
    going = []
    engine.start_catchup = lambda days, report: not going and (going.append(days) or True)
    engine.stop_catchup = lambda: (going.clear() or (1, 3)) if going else None
    monkeypatch.setattr(enc, "CONFIG", dataclasses.replace(enc.CONFIG,
                                                           metrics_path=tmp_path / "none.jsonl"))
    for text, budget in EVERY_COMMAND:
        before = len(sent)
        asyncio.run(ec.handle(text))
        assert len(sent) == before + 1, text
        assert_style(sent[-1], budget)
    assert sent[2] == "ℹ️ *LinkedIn · stats* · no accepts or replies yet"
    for name in settings.TUNABLES:
        settings.reset(name)
    enc.logging.getLogger().removeHandler(ec.verbose_log)
    enc.apply_log_level(0)


def test_set_answers_explain_and_warn(monkeypatch):
    from bot import settings

    ec, engine, sent = control(monkeypatch)
    engine.change_setting = lambda name, value: (settings.reset(name) if value is None
                                                 else settings.apply(name, value))
    try:
        asyncio.run(ec.handle("/set dry_run"))
        assert sent[-1] == ("ℹ️ *LinkedIn · dry_run off* · on: send and accept nothing, only log\n"
                            "Default off · /set dry_run on changes it")
        asyncio.run(ec.handle("/set dry_run on"))
        assert sent[-1] == ("🟡 *LinkedIn · dry run on* · nothing is sent or accepted\n"
                            "/set dry_run off ends it")
        asyncio.run(ec.handle("/settings"))
        assert sent[-1].startswith("🟡 *LinkedIn · settings* · 1 changed\n")
        assert "\n*Mode* dry_run *on* · scope " in sent[-1]
        assert "\n*Defaults* dry_run off\n" in sent[-1]
        asyncio.run(ec.handle("/set scope all"))
        assert sent[-1].startswith("🟡 *LinkedIn · scope all* · may answer chats you started")
        asyncio.run(ec.handle("/reset all"))
        assert sent[-1] == "✅ *LinkedIn · all settings default* · dry_run, scope had changed"
        for name in ("poll", "check_gap", "reply_delay", "max_replies"):
            engine.change_setting(name, settings.parse(name, {"poll": "60-120", "check_gap": "30",
                                                              "reply_delay": "1"}.get(name, "9")))
        asyncio.run(ec.handle("/reset all"))
        assert sent[-1] == "✅ *LinkedIn · all settings default* · 4 had changed"
    finally:
        for name in settings.TUNABLES:
            settings.reset(name)


def test_settings_lists_every_setting_grouped():
    text = enc.EncControl.settings_text()
    for name in enc.settings.TUNABLES:
        assert f" {name} " in text
    lines = text.split("\n")
    assert lines[0] == "ℹ️ *LinkedIn · settings* · all default"
    assert [line.split(" ")[0] for line in lines[1:-1]] == [
        "*Inbox*", "*Pauses*", "*Hours*", "*Limits*", "*Back-off*", "*Mode*"]
    assert lines[-1] == "/set name explains · /set name value changes · /reset name undoes"
    assert_style(text, 8)


def test_stats_are_a_few_short_parts():
    now = 1.7e9 + 7200
    entries = [
        {"at": 1.7e9, "kind": "accept", "total_s": 3.0, "detect_s": 2.5, "action_s": 0.5,
         "via": "rest", "trigger": "realtime"},
        {"at": 1.7e9 + 60, "kind": "reply", "total_s": 200.0, "since_last_s": 100.0,
         "messages": 2, "detect_s": 1.0, "quiet_s": 30.0, "draft_s": 4.0, "check_s": 1.2,
         "send_s": 3.0, "outcome": "question_blocked", "model": "*m*"},
    ] + [{"at": 1.7e9 + 120 + n, "kind": "reply", "total_s": 9000.0, "outcome": "draft_sent"}
         for n in range(6)]
    text = enc.stats_text(entries, now=now)
    assert text.split("\n") == [
        "ℹ️ *LinkedIn · stats* · 1 accept · 7 replies",
        "*Latest* reply 117m ago · accept 2h ago",
        "*Time to accept* typical 3.0s · 95% within 3.0s",
        "*Time to reply* typical 2h 30m · 95% within 2h 30m",
        "*Reply steps* seen after 1.0s · waited 30s · AI 4.0s",
        "*Written* 6 by the AI · 1 canned",
    ]
    assert_style(text, 6)
    assert enc.stats_text(entries[:1], now=now).split("\n") == [
        "ℹ️ *LinkedIn · stats* · 1 accept · 0 replies",
        "*Latest* accept 2h ago",
        "*Time to accept* typical 3.0s · 95% within 3.0s",
    ]


def test_durations_and_ages_read_the_same_everywhere():
    assert [enc.duration(s) for s in (0, 4.24, 42, 252, 7500, 7200, 273600, 90061)] == [
        "0.0s", "4.2s", "42s", "4m 12s", "2h 5m", "2h", "3d 4h", "1d 1h"]
    assert [enc.ago(s) for s in (45, 300, 25200, 259200, -5)] == [
        "45s ago", "5m ago", "7h ago", "3d ago", "0s ago"]
    assert enc.minutes_range(90, 300) == "1.5-5m" and enc.minutes_range(60, 60) == "1m"
    assert enc.days_text(1) == "1 day" and enc.days_text(1.5) == "1.5 days"


def at(*parts) -> float:
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def test_owner_time_follows_beirut_summer_time_at_both_edges():
    clock = lambda *parts: enc.owner_time(at(*parts)).strftime("%a %d %b %H:%M %z")  # noqa: E731
    # 2026: summer time from Sunday 29 March 00:00 to Sunday 25 October 00:00, local.
    assert clock(2026, 3, 28, 21, 59) == "Sat 28 Mar 23:59 +0200"
    assert clock(2026, 3, 28, 22, 0) == "Sun 29 Mar 01:00 +0300"
    assert clock(2026, 10, 24, 20, 59) == "Sat 24 Oct 23:59 +0300"
    assert clock(2026, 10, 24, 21, 0) == "Sat 24 Oct 23:00 +0200"
    assert clock(2026, 11, 25, 16, 40) == "Wed 25 Nov 18:40 +0200"
    assert clock(2027, 7, 1, 12, 0) == "Thu 01 Jul 15:00 +0300"
    assert enc.last_sunday(2027, 3).isoformat() == "2027-03-28"
    assert enc.last_sunday(2027, 10).isoformat() == "2027-10-31"


def test_owner_time_agrees_with_the_tz_database():
    try:
        from zoneinfo import ZoneInfo

        beirut = ZoneInfo("Asia/Beirut")
    except Exception:  # noqa: BLE001 - no tz database on this machine
        pytest.skip("no tz database")
    for year in range(2025, 2031):
        for month, day in ((3, 20), (10, 20)):
            start = at(year, month, day)
            for hour in range(0, 14 * 24, 1):
                moment = start + hour * 3600
                want = datetime.fromtimestamp(moment, beirut)
                assert enc.owner_time(moment).utcoffset() == want.utcoffset(), want


def test_set_error_quoting_the_owner_stays_inert(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    engine.change_setting = lambda name, value: None
    asyncio.run(ec.handle("/set poll *x*"))
    assert sent[-1] == "\u274c *LinkedIn · poll not changed* · '\u2217x\u2217' is not a number"


def test_alert_text_is_one_safe_line():
    assert (enc.alert_text("sign-in needed", "sign in again over VNC", "🔴")
            == "🔴 *LinkedIn · sign-in needed* · sign in again over VNC")
    assert enc.alert_text("stuck") == "🟡 *LinkedIn · stuck*"
    assert enc.alert_text("stuck", "", "🟠") == "🟡 *LinkedIn · stuck*"  # not a title emoji
    text = enc.alert_text("- pushed back\n```", "_boom_ [x](https://e.com)\n# x")
    assert text == "🟡 *LinkedIn · - pushed back '''* · \uff3fboom\uff3f [x] (https://e.com) # x"
    assert_style(text, 1)
    long = enc.alert_text("x", "word " * 40, "❌")
    assert len(long) <= enc.LINE_MAX and long.endswith("…")


def test_catchup_text_lists_names_safely_and_counts_the_rest():
    text = enc.catchup_text(["Sam", "*Lynn*", "1. Ana\n# x", ""], 3, (600.0, 1800.0))
    assert text.split("\n") == [
        "⏳ *LinkedIn · catch-up, 4 to answer* · last 3 days",
        "*In order* Sam, ∗Lynn∗, 1. Ana # x, ?",
        "*Pace* one every 10-30m · /catchup stop ends it",
    ]
    assert_style(text, 3)
    many = enc.catchup_text([f"Person {n}" for n in range(25)], 1.5, (60.0, 60.0))
    lines = many.split("\n")
    assert lines[0].endswith(" · last 1.5 days") and lines[2].startswith("*Pace* one every 1m ")
    assert lines[1].startswith("*In order* Person 0, Person 1, ") and lines[1].endswith(" more")
    shown = lines[1].count("Person")
    assert lines[1].endswith(f" and {25 - shown} more")
    assert_style(many, 3)
    assert enc.catchup_text(["Sam"], 1, (60.0, 60.0)).startswith(
        "⏳ *LinkedIn · catch-up, 1 to answer* · last 1 day\n*In order* Sam\n")


def test_every_alert_call_uses_a_title_emoji_and_lowercase_words():
    source = (pathlib.Path(enc.__file__).parent / "engine.py").read_text()
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "alert"]
    assert len(calls) >= 10
    for call in calls:
        assert len(call.args) == 3, ast.unparse(call)
        state, _, emoji = call.args
        assert isinstance(emoji, ast.Constant) and emoji.value in enc.TITLE_EMOJI
        words = state.value if isinstance(state, ast.Constant) else ast.unparse(state)
        assert words[:1].islower() or words.startswith("f'"), words


def test_engine_alerts_fit_one_line(monkeypatch):
    """The alerts from the engine's own code paths, with fake parts around them."""
    import dataclasses

    from bot import engine as engine_module

    sent = []
    monkeypatch.setattr(health.enc, "notify", sent.append)
    monkeypatch.setattr(health.shutil, "which", lambda name: None)
    health._last_sent.clear()

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(engine_module.asyncio, "sleep", nothing)
    monkeypatch.setattr(engine_module, "CONFIG", dataclasses.replace(
        engine_module.CONFIG, proxy="socks5://172.17.0.1:1080", dry_run=False,
        max_accepts_per_day=0))
    monkeypatch.setattr(engine_module, "in_active_hours", lambda *a: True)
    answers = iter(["proxy 172.17.0.1:1080 does not answer (ConnectionRefusedError)", ""])
    monkeypatch.setattr(engine_module, "proxy_problem", lambda proxy: next(answers))
    bot = SimpleNamespace(idle=nothing, session=SimpleNamespace(start=nothing),
                          wait_for_session=nothing)
    asyncio.run(engine_module.Engine.wake(bot))

    cookies = iter([False, True])

    async def has_cookie():
        return next(cookies)

    bot = SimpleNamespace(session=SimpleNamespace(has_auth_cookie=has_cookie, signed_in=True),
                          write_status=lambda: None)
    asyncio.run(engine_module.Engine.wait_for_session(bot))
    engine_module.Engine.push_back(SimpleNamespace(failures=1, backoff_until=0), "HTTP 429")
    engine_module.Engine.at_limit(SimpleNamespace(today=lambda kind: 40, capped=set()),
                                  "replies", 40)
    monkeypatch.setattr(engine_module.os, "_exit", lambda code: None)
    monkeypatch.setattr(engine_module.logging, "shutdown", lambda: None)
    engine_module.Engine.on_browser_closed(SimpleNamespace(), "Bot tab closed")

    async def refuse(*args):
        raise RuntimeError("HTTP 500")

    store = SimpleNamespace(is_seen_invite=lambda urn: False, add_seen_invite=lambda urn: None,
                            add_accepted=lambda person: None)
    invitation = SimpleNamespace(urn="i", kind="CONNECTION", sent_at=time.time() * 1000,
                                 inviter_id="p")

    async def invitations():
        return [invitation]

    bot = SimpleNamespace(api=SimpleNamespace(list_invitations=invitations, accept=nothing,
                                              send_reply=refuse),
                          store=store, scope=SimpleNamespace(watermark=0), seen_activity={},
                          at_limit=lambda *a: False, count=lambda kind: None)
    asyncio.run(engine_module.Engine.accept_round(bot))
    with pytest.raises(RuntimeError):
        asyncio.run(engine_module.Engine.send(bot, SimpleNamespace(urn="t"), "hi", [], {}))
    assert [text.split(" · ")[1].removesuffix("*") for text in sent] == [
        "home tunnel down", "sign-in needed", "on hold", "daily reply limit", "restarting",
        "accept failed", "reply not sent"]
    assert sent[0] == "🔴 *LinkedIn · home tunnel down* · start it so LinkedIn can open"
    assert re.fullmatch(r"🟡 \*LinkedIn · on hold\* · LinkedIn pushed back · back in 1[0-3]m",
                        sent[2])  # the second push-back in a row: 10m, plus up to 30%
    for text in sent:
        assert "…" not in text, text  # nothing was cut to fit
        assert_style(text, 1)
