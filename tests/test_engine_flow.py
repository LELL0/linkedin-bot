"""Runs the real engine against a simulated LinkedIn, so the logic is tested with no network."""

import asyncio
import dataclasses
import time
from types import SimpleNamespace

import pytest

from bot import engine as engine_module
from bot import metrics
from bot.config import CONFIG
from bot.engine import Engine
from bot.guard import Scope
from bot.models import Invitation, Message, Thread
from bot.store import Store

ME, THEM = "ACoAAme", "ACoAAthem"


def now_ms(offset_s: float = 0) -> int:
    return int((time.time() + offset_s) * 1000)


class FakeLinkedIn:
    def __init__(self):
        self.threads: dict[str, list[Message]] = {}
        self.sent: list[tuple[str, str]] = []
        self.send_failures = 0
        self.invitations: list[Invitation] = []
        self.accepted: list[str] = []
        self.accept_fails = False
        self.counter = 0

    def add(self, urn, sender=THEM, text="hi", at=None, render_format="DEFAULT"):
        self.counter += 1
        message = Message(f"m{self.counter}", sender, at or now_ms(), text, render_format)
        self.threads.setdefault(urn, []).append(message)
        return message

    def thread(self, urn, **fields):
        last = max((m.delivered_at for m in self.threads.get(urn, [])), default=0)
        return Thread(urn, last, fields.get("group", False), fields.get("categories", []),
                      [ME, THEM], fields.get("org", False), {THEM: "Sam"})

    async def list_threads(self, category):
        return [self.thread(urn) for urn in self.threads] if category == "PRIMARY_INBOX" else []

    async def list_messages(self, urn, count=30):
        return list(self.threads[urn][-count:])

    async def send_reply(self, urn, text):
        if self.send_failures:
            self.send_failures -= 1
            raise RuntimeError("send reply: HTTP 500")
        self.sent.append((urn, text))
        self.add(urn, sender=ME, text=text)

    async def mark_read(self, urn):
        pass

    async def show_typing(self, urn):
        pass

    async def list_invitations(self):
        return list(self.invitations)

    async def accept(self, invitation):
        if self.accept_fails:
            return ""
        self.accepted.append(invitation.urn)
        return "rest"


class FakeBrain:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.during_draft = None

    async def reply(self, history, pending, self_id, sender, trace=None):
        self.calls.append([m.text for m in pending])
        if self.during_draft:
            action, self.during_draft = self.during_draft, None
            action()
        return "sure"


@pytest.fixture
def sim(tmp_path, monkeypatch):
    config = dataclasses.replace(
        CONFIG, dry_run=False, debounce_quiet_s=0.05, debounce_max_s=0.4,
        metrics_path=tmp_path / "metrics.jsonl",
    )
    monkeypatch.setattr(engine_module, "CONFIG", config)
    monkeypatch.setattr(metrics, "CONFIG", config)
    monkeypatch.setattr(engine_module, "alert", lambda text: None)
    monkeypatch.setattr(engine_module, "write_status", lambda *a, **k: None)

    def build(api=None, mode="new_threads", store=None):
        bot = Engine.__new__(Engine)
        bot.api = api or FakeLinkedIn()
        bot.brain = FakeBrain()
        bot.store = store or Store(tmp_path / "state.db")
        bot.scope = Scope(mode, now_ms(-3600), ME)
        bot.session = SimpleNamespace(realtime_events=0)
        bot.workers, bot.recheck, bot.seen_activity = {}, set(), {}
        bot.hot_until, bot.last_ok, bot.trigger, bot.invite_trigger = 0.0, time.time(), "poll", "poll"
        bot.replies_sent = bot.accepted = 0
        bot.state = "running"
        return bot

    return build


async def settle(bot, rounds=3):
    """Poll a few times and wait for every worker to finish."""
    for _ in range(rounds):
        await bot.check_threads()
        while bot.workers:
            await asyncio.gather(*bot.workers.values())
        await asyncio.sleep(0.01)


def test_single_message_gets_one_reply(sim):
    bot = sim()
    bot.api.add("t")
    asyncio.run(settle(bot))
    assert len(bot.api.sent) == 1


def test_polling_again_never_answers_twice(sim):
    bot = sim()
    bot.api.add("t")
    asyncio.run(settle(bot, rounds=6))
    assert len(bot.api.sent) == 1


def test_burst_gets_one_reply_covering_all(sim):
    bot = sim()
    for text in ("hey", "quick one", "you around?"):
        bot.api.add("t", text=text)
    asyncio.run(settle(bot))
    assert len(bot.api.sent) == 1
    assert bot.brain.calls[-1] == ["hey", "quick one", "you around?"]


def test_message_arriving_while_drafting_is_included(sim):
    bot = sim()
    bot.api.add("t", text="first")
    bot.brain.during_draft = lambda: bot.api.add("t", text="second")
    asyncio.run(settle(bot))
    assert len(bot.api.sent) == 1
    assert bot.brain.calls[-1] == ["first", "second"]


def test_message_recalled_while_drafting_gets_no_reply(sim):
    bot = sim()
    message = bot.api.add("t")

    def recall():
        message.render_format = "RECALLED"

    bot.brain.during_draft = recall
    asyncio.run(settle(bot))
    assert bot.api.sent == []


def test_owner_answering_by_hand_stops_the_bot(sim):
    bot = sim()
    bot.api.add("t")
    bot.brain.during_draft = lambda: bot.api.add("t", sender=ME, text="typed by hand")
    asyncio.run(settle(bot))
    assert bot.api.sent == []


def test_never_writes_first_in_a_thread_where_we_spoke_last(sim):
    bot = sim()
    bot.api.add("t", sender=ME)
    asyncio.run(settle(bot))
    assert bot.api.sent == []


def test_old_thread_stays_untouched_by_default(sim):
    bot = sim()
    bot.api.add("t", at=now_ms(-7200))
    bot.api.add("t", text="new message from an old contact")
    asyncio.run(settle(bot))
    assert bot.api.sent == []


def test_old_thread_is_answered_in_scope_all(sim):
    bot = sim(mode="all")
    bot.api.add("t", at=now_ms(-7200))
    bot.api.add("t", text="new message")
    asyncio.run(settle(bot))
    assert len(bot.api.sent) == 1


def test_accepted_person_is_answered_only_for_messages_after_the_accept(sim):
    bot = sim()
    bot.api.add("t", at=now_ms(-7200))
    bot.api.add("t", text="sent before the invite", at=now_ms(-1200))
    bot.api.invitations = [invite("i1", now_ms(-2))]
    asyncio.run(bot.check_invitations())
    restarted = sim(api=bot.api, store=bot.store)
    asyncio.run(settle(restarted))
    assert bot.api.sent == []
    time.sleep(0.01)
    bot.api.add("t", text="sent after the invite")
    asyncio.run(settle(restarted))
    assert len(bot.api.sent) == 1
    assert restarted.brain.calls[-1] == ["sent after the invite"]


@pytest.mark.parametrize("fields", [{"group": True}, {"org": True}, {"categories": ["SPAM"]}])
def test_groups_companies_and_spam_are_ignored(sim, fields):
    bot = sim(mode="all")
    bot.api.add("t")
    original = bot.api.thread
    bot.api.thread = lambda urn, **_: original(urn, **fields)
    asyncio.run(settle(bot))
    assert bot.api.sent == []


def test_failed_sends_retry_and_deliver_once(sim, monkeypatch):
    bot = sim()
    bot.api.add("t")
    bot.api.send_failures = 1
    monkeypatch.setattr(engine_module, "SEND_ATTEMPTS", 2)
    asyncio.run(settle(bot))
    assert len(bot.api.sent) == 1


def test_all_sends_failing_is_retried_on_a_later_poll(sim, monkeypatch):
    bot = sim()
    bot.api.add("t")
    bot.api.send_failures = 2
    monkeypatch.setattr(engine_module, "SEND_ATTEMPTS", 2)
    asyncio.run(settle(bot, rounds=4))
    assert len(bot.api.sent) == 1


def test_restart_does_not_answer_again(sim):
    bot = sim()
    bot.api.add("t")
    asyncio.run(settle(bot))
    restarted = sim(api=bot.api, store=bot.store)
    asyncio.run(settle(restarted))
    assert len(bot.api.sent) == 1


def test_restart_answers_what_arrived_while_down(sim):
    bot = sim()
    bot.api.add("t")
    asyncio.run(settle(bot))
    bot.api.add("t", text="sent while the laptop was off")
    restarted = sim(api=bot.api, store=bot.store)
    asyncio.run(settle(restarted))
    assert len(bot.api.sent) == 2


def test_long_new_thread_stays_in_scope_when_the_page_fills(sim):
    bot = sim()
    for index in range(20):
        bot.api.add("t", text=f"message {index}")
        asyncio.run(settle(bot, rounds=1))
    assert len(bot.api.sent) == 20


def test_every_reply_is_recorded_in_metrics(sim):
    bot = sim()
    bot.api.add("t")
    asyncio.run(settle(bot))
    entries = metrics.load(engine_module.CONFIG.metrics_path)
    assert [e["kind"] for e in entries] == ["reply"]
    assert entries[0]["total_s"] >= 0 and entries[0]["messages"] == 1


def invite(urn, sent_at, kind="CONNECTION"):
    return Invitation(urn, urn, "secret", THEM, sent_at, kind, "Sam")


def test_new_invite_is_accepted_once(sim):
    bot = sim()
    bot.api.invitations = [invite("i1", now_ms(-2))]
    asyncio.run(bot.check_invitations())
    asyncio.run(bot.check_invitations())
    assert bot.api.accepted == ["i1"]
    assert bot.store.is_accepted(THEM)


def test_invite_older_than_the_first_start_is_left_alone(sim):
    bot = sim()
    bot.api.invitations = [invite("old", now_ms(-7200))]
    asyncio.run(bot.check_invitations())
    assert bot.api.accepted == []


def test_other_invite_types_are_left_alone(sim):
    bot = sim()
    bot.api.invitations = [invite("event", now_ms(-2), kind="OTHER")]
    asyncio.run(bot.check_invitations())
    assert bot.api.accepted == []


def test_failed_accept_is_tried_again(sim):
    bot = sim()
    bot.api.invitations = [invite("i1", now_ms(-2))]
    bot.api.accept_fails = True
    asyncio.run(bot.check_invitations())
    bot.api.accept_fails = False
    asyncio.run(bot.check_invitations())
    assert bot.api.accepted == ["i1"]
