"""Enc Chat commands for the owner, against a fake bot agent. All data is synthetic."""

import asyncio
import http.server
import json
import re
import threading
import time
from types import SimpleNamespace

import pytest

from bot import enc, health


OWNER = "01HZX3K9M2Q4R5S6T7V8W9XYZA"  # a 26 character Enc user id
DIVIDER = re.compile(r" *(?:-{3,}|\*{3,}|_{3,}) *")


def assert_compact(text: str, max_lines: int) -> None:
    """Enc Chat's "Keep bot messages small": the first line is an emoji and a *bold* title,
    no heading, blank line or divider, at most one code box (a table under 32 characters
    wide), and no more lines than the message's budget."""
    lines = text.split("\n")
    assert len(lines) <= max_lines, text
    assert not lines[0][0].isascii() and re.match(r"\S{1,3} \*[^*\s]", lines[0]), lines[0]
    assert all(line.strip() for line in lines), f"blank line in {text!r}"
    assert not any(line.lstrip().startswith("#") for line in lines), f"heading in {text!r}"
    assert not any(DIVIDER.fullmatch(line) for line in lines), f"divider in {text!r}"
    fences = [n for n, line in enumerate(lines) if line.startswith("```")]
    assert len(fences) in (0, 2), text
    if fences:
        rows = lines[fences[0] + 1:fences[1]]
        assert rows and all(len(row) < 32 and len(row.split()) >= 3 for row in rows), rows


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
    assert lines[0] == "🟢 *Running* · no check yet"
    assert lines[1] == "*Since start* 1 accepted · 2 replies"
    assert lines[3] == "*Feeds* replies off · verbose 0"
    assert "<b>" not in sent[-1]
    assert_compact(sent[-1], 4)
    engine.last_ok = time.time() - 300
    asyncio.run(ec.handle("/status"))
    assert sent[-1].startswith("🟢 *Running* · last check 5 min ago\n")


def test_reply_feed_quotes_with_marks_inert_and_only_when_on(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    asyncio.run(ec.reply_sent("Sam", ["*hi* ```x```"], "hey"))
    assert sent == []
    engine.replies_feed = True
    asyncio.run(ec.reply_sent("Sam", ["*hi* ```x```", "line one\n\n# not a heading"], "_ok_"))
    # One quoted line per message, the reply on one line: nothing from outside adds a mark.
    assert sent[0] == ("💬 *Sam*\n> \u2217hi\u2217 '''x'''\n> line one # not a heading\n"
                       "↩️ *Reply* \uff3fok\uff3f")
    assert_compact(sent[0], 4)


def test_reply_feed_name_cannot_add_marks_or_lines(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    engine.replies_feed = True
    asyncio.run(ec.reply_sent("_Sam_\n## Boss", ["hi"], "hey"))
    asyncio.run(ec.reply_sent("", ["hi"], "hey"))
    asyncio.run(ec.reply_sent("Ana", ["", " "], "line one\n- line two"))
    assert sent[0] == "💬 *\uff3fSam\uff3f ## Boss*\n> hi\n↩️ *Reply* hey"
    assert sent[1].startswith("💬 *Someone*\n")
    assert sent[2] == "💬 *Ana*\n> …\n↩️ *Reply* line one - line two"


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
    assert sent[-1] == enc.HELP and enc.HELP.startswith("🤖 *LinkedIn bot* · commands\n")


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
    assert agent.sent[0]["text"].startswith("⏸️ *Paused* · no accepts or replies until /resume")
    status = agent.sent[1]["text"]
    assert status.startswith("⏸️ *Paused* · ") and "\n*Since start* " in status
    assert len(agent.sent) == 2  # the stranger got nothing; the reconnect did not repeat anything
    assert agent.stream_calls >= 1
    names = [c["command"] for c in agent.commands]
    assert names == [c for c, _ in enc.COMMANDS]
    enc.logging.getLogger().removeHandler(ec.verbose_log)


def test_nothing_goes_to_telegram_even_with_its_old_variables_set(agent, monkeypatch):
    import pathlib

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
    assert engine.verbose == 2 and "Verbose 2" in sent[-1] and "2 min" in sent[-1]
    log = enc.logging.getLogger("bot.engine")
    for text in ("Accepted invitation: {}", "Draft rejected: too long", "alive: accepted=1"):
        ec.verbose_log.emit(log.makeRecord("bot.engine", 20, __file__, 1, text, (), None))
    ec.verbose_log.emit(log.makeRecord("bot.session", 30, __file__, 1, "Accept failed", (), None))
    asyncio.run(ec.send_verbose())
    lines = sent[-1].split("\n")
    assert lines[0] == "📜 *Log* · 2 lines"
    assert re.fullmatch(r"- \d\d:\d\d:\d\d engine: Accepted invitation: \{\}", lines[1])
    assert re.fullmatch(r"- \d\d:\d\d:\d\d ⚠️ session: Accept failed", lines[2])
    assert "Draft rejected" not in sent[-1] and "alive" not in sent[-1]
    assert_compact(sent[-1], 3)
    count = len(sent)
    asyncio.run(ec.send_verbose())  # nothing new: no empty message
    assert len(sent) == count
    for wrong in ("/verbose loud", "/verbose 6"):
        asyncio.run(ec.handle(wrong))
        assert engine.verbose == 2 and "level from 0 to 5" in sent[-1]
    assert sent[-1].startswith("❔ *Verbose 2* stays · use a level from 0 to 5\n*Levels* 0 off · ")
    asyncio.run(ec.handle("/verbose"))
    assert sent[-1] == ("📜 *Verbose 2* · warnings, accepts and replies · log every 2 min\n"
                        "*Levels* 0 off · 1 warnings · 2 + accepts, replies · 3 all but heartbeat"
                        " · 4 + heartbeat · 5 + debug\n_/verbose 0-5 changes it._")
    asyncio.run(ec.handle("/verbose off"))
    assert engine.verbose == 0
    assert sent[-1] == "📜 *Verbose 0* · off, only alerts"
    asyncio.run(ec.handle("/status"))
    assert "verbose 0" in sent[-1]
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
    assert re.fullmatch(r"\d\d:\d\d:\d\d 🔴 engine: Check failed \(ValueError: bad \*thing\*\n"
                        r"second line\)", line)
    text = enc.verbose_message([line, "- # **x**", "y" * 500])
    lines = text.split("\n")
    assert lines[0] == "📜 *Log* · 3 lines" and len(lines) == 4
    assert lines[1].startswith("- ") and "\u2217thing\u2217 second line" in lines[1]
    assert lines[2] == "- - # \u2217\u2217x\u2217\u2217"  # inert, on its line
    assert len(lines[3]) == 2 + enc.VERBOSE_LINE
    cut = enc.verbose_message([f"line {n}" for n in range(1000)], limit=100)
    assert cut.startswith("📜 *Log* · 9 lines · 991 earlier left out\n- line 991\n")


def test_catchup_command_checks_days_and_stop(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    started = []
    engine.start_catchup = lambda days, report: started.append(days) or True
    engine.stop_catchup = lambda: False
    asyncio.run(ec.handle("/catchup"))
    asyncio.run(ec.handle("/catchup 99"))
    assert started == [] and "/catchup 1-30" in sent[-1]
    assert_compact(sent[-1], 1)
    asyncio.run(ec.handle("/catchup 3"))
    assert started == [3.0]
    asyncio.run(ec.handle("/catchup stop"))
    assert "No catch-up" in sent[-1]


def test_commands_fit_the_agent_rules():
    import re
    for name, description in enc.COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name) and 1 <= len(description) <= 120


def test_commands_reach_enc_chat_through_the_agent(agent, monkeypatch):
    engine = SimpleNamespace(paused=False, replies_feed=False, state="running", last_ok=0,
                             accepted=0, replies_sent=0, verbose=0)
    engine.set_paused = lambda on: setattr(engine, "paused", on)
    ec = enc.EncControl(engine)
    for text in ("/help", "/status", "/settings", "/pause"):
        asyncio.run(ec.handle(text))
    enc_texts = [m["text"] for m in agent.sent]
    assert enc_texts[0] == enc.HELP
    assert enc_texts[1].startswith("🟢 *Running* · ")
    assert enc_texts[2].startswith("⚙️ *Settings* · ") and "\n*Timing* poll " in enc_texts[2]
    assert enc_texts[3].startswith("⏸️ *Paused*")
    enc.logging.getLogger().removeHandler(ec.verbose_log)


# Every command, and the line budget of its answer.
EVERY_COMMAND = [
    ("/help", 5), ("/status", 4), ("/stats", 1), ("/pause", 1), ("/resume", 1),
    ("/replies on", 1), ("/replies off", 1), ("/replies loud", 1), ("/verbose", 3),
    ("/verbose 2", 1), ("/verbose 9", 2), ("/verbose 0", 1), ("/catchup", 1),
    ("/catchup 3", 1), ("/catchup 3", 1), ("/catchup stop", 1), ("/catchup stop", 1),
    ("/settings", 6), ("/set", 1), ("/set poll", 1), ("/set poll 6-12", 1),
    ("/set poll x", 1), ("/set dry_run on", 2), ("/set dry_run off", 1),
    ("/set scope all", 2), ("/interval 5-15", 1), ("/reset", 1), ("/reset poll", 1),
    ("/reset all", 1), ("/nope", 1),
]


def test_every_answer_is_compact(monkeypatch, tmp_path):
    import dataclasses

    from bot import settings

    ec, engine, sent = control(monkeypatch)
    engine.verbose = 0
    engine.set_verbose = lambda level: setattr(engine, "verbose", level)
    engine.change_setting = lambda name, value: (settings.reset(name) if value is None
                                                 else settings.apply(name, value))
    going = []
    engine.start_catchup = lambda days, report: not going and (going.append(days) or True)
    engine.stop_catchup = lambda: bool(going) and (going.clear() or True)
    monkeypatch.setattr(enc, "CONFIG", dataclasses.replace(enc.CONFIG,
                                                           metrics_path=tmp_path / "none.jsonl"))
    assert all(f"/{c}" in enc.HELP for c, _ in enc.COMMANDS if c != "help")
    for text, budget in EVERY_COMMAND:
        before = len(sent)
        asyncio.run(ec.handle(text))
        assert len(sent) == before + 1, text
        assert_compact(sent[-1], budget)
    assert sent[2] == "📊 *Stats* · no accepts or replies yet"
    enc.logging.getLogger().removeHandler(ec.verbose_log)
    enc.apply_log_level(0)


def test_settings_lists_every_setting_grouped():
    text = enc.EncControl.settings_text()
    for name in enc.settings.TUNABLES:
        assert f" {name} " in text
    lines = text.split("\n")
    assert lines[0] == "⚙️ *Settings* · all default"
    assert [line.split(" ")[0] for line in lines[1:-1]] == ["*Timing*", "*Hours*", "*Limits*",
                                                           "*Mode*"]
    assert_compact(text, 6)


def test_stats_are_parts_and_one_small_table():
    entries = [
        {"at": 1.7e9, "kind": "accept", "total_s": 3.0, "detect_s": 2.5, "action_s": 0.5,
         "via": "rest", "trigger": "realtime"},
        {"at": 1.7e9 + 60, "kind": "reply", "total_s": 200.0, "since_last_s": 100.0,
         "messages": 2, "detect_s": 1.0, "quiet_s": 30.0, "draft_s": 4.0, "check_s": 1.2,
         "send_s": 3.0, "outcome": "question_blocked", "model": "*m*"},
    ] + [{"at": 1.7e9 + 120 + n, "kind": "reply", "total_s": 9000.0, "outcome": "draft_sent"}
         for n in range(6)]
    text = enc.stats_text(entries)
    lines = text.split("\n")
    assert lines[0] == "📊 *Stats* · 1 accept · 7 replies to 8 messages"
    assert lines[1] == "*Accepts* invite to accept 3.0s median, 3.0s p95 · noticed 2.5s · call 0.5s"
    assert lines[2] == "*Accepts via* rest 1 · seen by realtime 1"
    assert lines[3] == ("*Replies* first message to reply 2.5 h median, 2.5 h p95 · "
                        "last to reply 1.7 min")
    assert lines[4] == ("*Reply steps* noticed 1.0s · quiet 30s · draft 4.0s · check 1.2s · "
                        "send 3.0s")
    assert lines[5] == ("*Outcomes* draft_sent 6, question_blocked 1 · "
                        "models unknown 6, ∗m∗ 1")
    assert lines[6] == "```" and lines[-1] == "```" and len(lines) == 8 + enc.STATS_LATEST
    assert lines[7].endswith(" reply   2.5h sent")
    assert_compact(text, 8 + enc.STATS_LATEST)
    only_accepts = enc.stats_text(entries[:1]).split("\n")
    assert only_accepts[0] == "📊 *Stats* · 1 accept · 0 replies to 0 messages"
    assert only_accepts[3] == "```" and only_accepts[4].endswith(" accept  3.0s rest")


def test_set_error_quoting_the_owner_stays_inert(monkeypatch):
    ec, engine, sent = control(monkeypatch)
    engine.change_setting = lambda name, value: None
    asyncio.run(ec.handle("/set poll *x*"))
    assert sent[-1] == "\u274c *Not changed* · '\u2217x\u2217' is not a number"


def test_alert_text_is_one_safe_line():
    assert (enc.alert_text("Signed out or checkpoint", "sign in again", "🔴")
            == "🔴 *Signed out or checkpoint* · sign in again")
    assert enc.alert_text("Stuck") == "⚠️ *Stuck*"
    text = enc.alert_text("- pushed back\n```", "_boom_ [x](https://e.com)\n# x")
    assert text == "\u26a0\ufe0f *- pushed back '''* · \uff3fboom\uff3f [x] (https://e.com) # x"
    assert_compact(text, 1)


def test_catchup_text_lists_names_safely_and_counts_the_rest():
    text = enc.catchup_text(["Sam", "*Lynn*", "1. Ana\n# x", ""], 3, (600.0, 1800.0))
    assert text.split("\n") == [
        "🔁 *Catch-up · 4 conversations* · last 3 days",
        "*In order* Sam · ∗Lynn∗ · 1. Ana # x · ?",
        "*Pace* one every 10-30 min · /catchup stop cancels",
    ]
    assert_compact(text, 3)
    many = enc.catchup_text([f"P{n}" for n in range(25)], 1.5, (60.0, 60.0))
    assert "· P19 · and 5 more\n" in many and "P20" not in many
    assert enc.catchup_text(["Sam"], 1, (60.0, 60.0)).startswith("🔁 *Catch-up · 1 conversation* ·")
