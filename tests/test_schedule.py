"""How often the bot touches LinkedIn: a few planned checks a day, every few minutes while a
chat goes on, and Chrome closed in between. All data is synthetic.

The second half runs the engine's real loops on an event loop whose clock jumps to the next
timer, so days of the bot's life take a second and every LinkedIn call can be timed."""

import asyncio
import dataclasses
import random
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from bot import engine as engine_module
from bot import metrics, schedule
from bot.config import CONFIG
from bot.engine import Engine, meta_time
from bot.guard import Scope
from bot.linkedin import INBOX_CATEGORIES
from bot.store import Store
from tests.test_engine_flow import ME, THEM, FakeBrain, FakeLinkedIn

HOURS = (8.0, 24.0)
POLL = (120.0, 600.0)
DAY = 86400


def at(day: date, hour: float) -> float:
    return (datetime.combine(day, datetime.min.time()) + timedelta(hours=hour)).timestamp()


def local_hour(moment: float) -> float:
    when = datetime.fromtimestamp(moment)
    return when.hour + when.minute / 60 + when.second / 3600


def days(count: int, first: date = date(2026, 1, 1)):
    return [first + timedelta(days=n) for n in range(count)]


# Planned moments ---------------------------------------------------------------------------


@pytest.mark.parametrize("count, salt, gap_h", [(3, "inbox", 1.6), (2, "invites", 2.4)])
def test_planned_moments_stay_in_the_active_hours_and_apart(count, salt, gap_h):
    for day in days(365):
        plan = schedule.day_plan(day, count, salt, HOURS)
        assert len(plan) == count and plan == sorted(plan)
        for moment in plan:
            assert schedule.within(HOURS, moment) and moment.date() == day
        for first, second in zip(plan, plan[1:]):
            assert (second - first).total_seconds() >= gap_h * 3600 - 1


def test_planned_moments_change_every_day_but_not_on_a_restart():
    first = [schedule.day_plan(d, 3, "inbox", HOURS)[0].time() for d in days(60)]
    assert len(set(first)) == 60  # Never the same time two days running.
    day = date(2026, 1, 7)
    assert schedule.day_plan(day, 3, "inbox", HOURS) == schedule.day_plan(day, 3, "inbox", HOURS)
    assert schedule.day_plan(day, 3, "inbox", HOURS) != schedule.day_plan(day, 3, "x", HOURS)


def test_planned_moments_use_their_whole_window():
    part = (HOURS[1] - HOURS[0]) / 3
    for i in range(3):
        hours = [m.hour + m.minute / 60 + m.second / 3600 for d in days(365)
                 for m in schedule.day_plan(d, 3, "inbox", HOURS)[i:i + 1]]
        low = HOURS[0] + part * (i + schedule.EDGE)
        high = HOURS[0] + part * (i + 1 - schedule.EDGE)
        assert low <= min(hours) < low + 0.1 and high - 0.1 < max(hours) <= high
        quarter = (high - low) / 4
        for q in range(4):  # Spread evenly, not bunched at one time of the window.
            share = sum(low + q * quarter <= h < low + (q + 1) * quarter for h in hours) / 365
            assert 0.17 < share < 0.33, (i, q, share)


def test_hours_past_midnight():
    hours = (18.0, 2.0)
    for day in days(30):
        for moment in schedule.day_plan(day, 3, "inbox", hours):
            assert schedule.within(hours, moment)
    late = at(date(2026, 1, 2), 1.0)  # 1:00 belongs to the day before's active hours.
    following = schedule.planned_after(late, 3, "inbox", hours)
    assert schedule.within(hours, datetime.fromtimestamp(following)) and following > late


# Next inbox check --------------------------------------------------------------------------


def next_check(last_check, now, watch_until=0.0, failed=False, hours=HOURS):
    return schedule.next_inbox_check(last_check, watch_until, failed, now, poll_s=POLL,
                                     per_day=3, hours=hours)


def test_no_chat_means_the_next_planned_check():
    day = date(2026, 1, 5)
    plan = [m.timestamp() for m in schedule.day_plan(day, 3, "inbox", HOURS)]
    assert next_check(plan[0], plan[0]) == plan[1]
    assert next_check(plan[2], plan[2]) == schedule.day_plan(
        day + timedelta(days=1), 3, "inbox", HOURS)[0].timestamp()


def test_a_chat_is_checked_every_two_to_ten_minutes():
    now = at(date(2026, 1, 5), 12)
    for _ in range(500):
        due = next_check(now, now, watch_until=now + 3600)
        assert POLL[0] <= due - now <= POLL[1]


def test_a_failed_check_is_tried_again_in_minutes():
    now = at(date(2026, 1, 5), 12)
    due = next_check(now, now, failed=True)
    assert schedule.RETRY_S[0] <= due - now <= schedule.RETRY_S[1]


def test_a_check_missed_while_off_runs_soon_but_not_at_once():
    now = at(date(2026, 1, 5), 15)
    due = next_check(now - 6 * 3600, now)
    assert schedule.LATE_S[0] <= due - now <= schedule.LATE_S[1]
    assert schedule.LATE_S[0] <= next_check(0.0, now) - now <= schedule.LATE_S[1]  # First start


def test_nothing_is_ever_planned_at_night():
    rng = random.Random(1)
    for _ in range(3000):
        last = at(date(2026, 1, 5), rng.uniform(0, 48))
        now = last + rng.uniform(0, 12 * 3600)
        due = next_check(last, now, watch_until=last + rng.choice([0, 3600, 9000]),
                         failed=rng.random() < 0.2)
        assert due > now and schedule.within(HOURS, datetime.fromtimestamp(due))


def test_invitation_rounds():
    day = date(2026, 1, 5)
    plan = [m.timestamp() for m in schedule.day_plan(day, 2, "invites", HOURS)]
    assert schedule.next_invite_round(plan[0], plan[0], per_day=2, hours=HOURS) == plan[1]
    first_start = schedule.next_invite_round(0.0, at(day, 3), per_day=2, hours=HOURS)
    assert first_start == plan[0]  # Started at night: waits for the first planned round.


# The engine's loops on a virtual clock --------------------------------------------------------


class VirtualLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock jumps to the next timer whenever nothing is ready to run."""

    def __init__(self):
        super().__init__()
        self.now = 0.0

    def time(self):
        return self.now

    def _run_once(self):
        if not self._ready and self._scheduled:
            self.now = max(self.now, self._scheduled[0]._when)
        super()._run_once()


class Recorder:
    def __init__(self, start: float):
        self.start = start
        self.loop: VirtualLoop | None = None
        self.calls: list[tuple[float, str, bool]] = []  # time, call, Chrome open
        self.checks: list[float] = []
        self.opened: list[float] = []
        self.closed: list[float] = []

    def clock(self) -> float:
        return self.start + (self.loop.time() if self.loop else 0.0)


class FakeChrome:
    def __init__(self, rec: Recorder):
        self.rec = rec
        self.open = False
        self.realtime_events = 0

    async def start(self):
        self.open = True
        self.rec.opened.append(self.rec.clock())

    async def stop(self):
        if self.open:
            self.rec.closed.append(self.rec.clock())
        self.open = False


class TimedLinkedIn(FakeLinkedIn):
    """Every call is timed, and refused unless Chrome is open."""

    def __init__(self, rec: Recorder, chrome: FakeChrome):
        super().__init__()
        self.rec, self.chrome = rec, chrome
        self.sent_at: list[float] = []

    def note(self, call: str):
        self.rec.calls.append((self.rec.clock(), call, self.chrome.open))
        assert self.chrome.open, f"{call} with Chrome closed"

    async def list_threads(self, category, before=None):
        self.note("list_threads")
        if category == INBOX_CATEGORIES[0]:
            self.rec.checks.append(self.rec.clock())
        return await super().list_threads(category, before)

    async def list_messages(self, urn, count=30):
        self.note("list_messages")
        return await super().list_messages(urn, count)

    async def send_reply(self, urn, text):
        self.note("send_reply")
        self.sent_at.append(self.rec.clock())
        self.sent.append((urn, text))
        self.add(urn, sender=ME, text=text, at=int(self.rec.clock() * 1000))

    async def mark_read(self, urn):
        self.note("mark_read")

    async def show_typing(self, urn):
        self.note("show_typing")

    async def list_invitations(self):
        self.note("list_invitations")
        return await super().list_invitations()


START = at(date(2026, 1, 5), 6)  # A Monday at 6:00, before the active hours.


@pytest.fixture
def life(tmp_path, monkeypatch):
    """Run the bot's loops for some virtual days. Returns what LinkedIn saw."""
    config = dataclasses.replace(
        CONFIG, dry_run=False, active_hours=HOURS, inbox_checks=3, invite_rounds=2,
        poll_s=POLL, watch_s=7200.0, min_check_gap_s=60.0, reply_delay_s=(25.0, 150.0),
        debounce_quiet_s=3.0, debounce_max_s=9.0, proxy="", max_replies_per_day=40,
        metrics_path=tmp_path / "metrics.jsonl",
    )
    for module in (engine_module, metrics):
        monkeypatch.setattr(module, "CONFIG", config)
    monkeypatch.setattr(engine_module, "alert", lambda *a, **k: None)
    monkeypatch.setattr(engine_module, "write_status", lambda *a, **k: None)
    store_path = tmp_path / "state.db"
    rec = Recorder(START)
    clock = SimpleNamespace(time=rec.clock, monotonic=rec.clock)
    monkeypatch.setattr(engine_module, "time", clock)
    monkeypatch.setattr(engine_module, "in_active_hours", lambda now=None: schedule.within(
        HOURS, now or datetime.fromtimestamp(rec.clock())))

    def build(api=None):
        """An engine as Engine.__init__ makes it, with fakes for Chrome and LinkedIn."""
        bot = Engine.__new__(Engine)
        bot.session = api.chrome if api else FakeChrome(rec)
        bot.api = api or TimedLinkedIn(rec, bot.session)
        bot.brain, bot.store = FakeBrain(), Store(store_path)
        bot.scope = Scope("new_threads", int((START - 3600) * 1000), ME)
        bot.workers, bot.recheck, bot.seen_activity, bot.capped = {}, set(), {}, set()
        bot.last_ok, bot.checked_at, bot.trigger = rec.clock(), 0.0, "poll"
        bot.replies_sent = bot.accepted = bot.failures = bot.busy = 0
        bot.state, bot.started, bot.paused, bot.asleep = "resting", True, False, False
        bot.recovering, bot.opening = asyncio.Lock(), asyncio.Lock()
        bot.poll_now, bot.invites_replan = asyncio.Event(), asyncio.Event()
        bot.backoff_until, bot.check_failed = 0.0, False
        bot.last_check = meta_time(bot.store, "last_check_at")
        bot.last_invites = meta_time(bot.store, "invites_at")
        bot.watch_until = meta_time(bot.store, "watch_until")
        bot.next_check = bot.next_invites = 0.0
        bot.catchup_since, bot.catchup_task, bot.catchup_progress = {}, None, None
        bot.replies_feed, bot.verbose = False, 0
        bot.enc = SimpleNamespace(reply_sent=_nothing)

        async def signed_in():
            bot.state = "running"

        bot.wait_for_session = signed_in
        return bot

    def run(seconds: float, bot=None, *others):
        bot = bot or build()

        async def main():
            tasks = [asyncio.create_task(c) for c in
                     (bot.poll_loop(), bot.invite_loop(), bot.browser_loop(), *others)]
            await asyncio.sleep(seconds)
            for task in tasks:
                if task.done() and not task.cancelled() and task.exception():
                    raise task.exception()
                task.cancel()
            await asyncio.gather(*tasks, *bot.workers.values(), return_exceptions=True)

        rec.loop = VirtualLoop()  # Its clock starts at 0: rec.start carries the time on.
        try:
            rec.loop.run_until_complete(main())
        finally:
            rec.start = rec.clock()
            rec.loop.close()
            rec.loop = None
        return bot

    return SimpleNamespace(rec=rec, run=run, build=build)


async def _nothing(*args):
    return None


def per_day(moments):
    counts: dict[date, int] = {}
    for moment in moments:
        day = datetime.fromtimestamp(moment).date()
        counts[day] = counts.get(day, 0) + 1
    return counts


def chrome_open_s(rec: Recorder, day: date) -> float:
    start, end = at(day, 0), at(day, 24)
    return sum(max(0.0, min(close, end) - max(open_, start))
               for open_, close in zip(rec.opened, rec.closed))


def test_a_quiet_week(life):
    life.run(7 * DAY)
    rec = life.rec
    week = days(7, date(2026, 1, 5))
    assert per_day(rec.checks) == {d: 3 for d in week}
    rounds = [t for t, call, _ in rec.calls if call == "list_invitations"]
    assert per_day(rounds) == {d: 2 for d in week}
    for moment, call, chrome in rec.calls:
        assert chrome and schedule.within(HOURS, datetime.fromtimestamp(moment)), call
    # Nothing else touches LinkedIn on a quiet day: 3 inbox reads and 2 invitation lists.
    assert {call for _, call, _ in rec.calls} == {"list_threads", "list_invitations"}
    assert per_day([t for t, _, _ in rec.calls]) == {
        d: 3 * len(INBOX_CATEGORIES) + 2 for d in week}
    for first, second in zip(rec.checks, rec.checks[1:]):
        assert second - first >= 1.6 * 3600 - 1
    assert len(rec.opened) == len(rec.closed) <= 5 * 7  # Opened only for checks and rounds.
    for day in week:
        assert chrome_open_s(rec, day) < 20 * 60  # LinkedIn open under 20 minutes a day.
    for close in rec.closed:  # Never open at night.
        assert schedule.within(HOURS, datetime.fromtimestamp(close - 1))


def test_a_chat_is_followed_until_two_quiet_hours(life):
    rec = life.rec
    api_holder = {}

    async def person():
        """Writes at 9:00, then answers each reply 4 minutes later, three times."""
        api = api_holder["api"]
        await asyncio.sleep(at(date(2026, 1, 5), 9) - rec.clock())
        api.add("chat", text="hello", at=int(rec.clock() * 1000))
        for _ in range(3):
            answered = len(api.sent)
            while len(api.sent) == answered:
                await asyncio.sleep(30)
            await asyncio.sleep(240)
            api.add("chat", text="and?", at=int(rec.clock() * 1000))

    bot = life.build()
    api_holder["api"] = bot.api
    life.run(2 * DAY, bot, person())

    incoming = [m.delivered_at / 1000 for m in bot.api.threads["chat"] if m.sender_id == THEM]
    replies = bot.api.sent_at
    assert len(incoming) == len(replies) == 4
    first_plan = schedule.day_plan(date(2026, 1, 5), 3, "inbox", HOURS)[0].timestamp()
    # The first message waits for the first planned check, the others are seen within
    # minutes: at most one 10-minute wait, the reply delay and typing.
    assert replies[0] >= max(first_plan, incoming[0])
    for message, reply in zip(incoming[1:], replies[1:]):
        assert 0 < reply - message <= 600 + 150 + 30 + 60

    end = replies[-1] + 7200
    following = [t for t in rec.checks if replies[0] <= t <= end + 600]
    for first, second in zip(following, following[1:]):
        assert POLL[0] - 1 <= second - first <= POLL[1] + 1  # Every 2-10 minutes.
    assert following[-1] >= end  # Until two hours after the last word.
    planned = {m.timestamp() for d in days(3, date(2026, 1, 5))
               for m in schedule.day_plan(d, 3, "inbox", HOURS)}
    later = [t for t in rec.checks if t > end + 600]
    assert later and all(min(abs(t - p) for p in planned) < 1 for t in later)  # The plan again.
    for moment, call, chrome in rec.calls:
        assert chrome and schedule.within(HOURS, datetime.fromtimestamp(moment)), call


def test_a_restart_neither_repeats_nor_skips_a_check(life):
    rec = life.rec
    life.run(at(date(2026, 1, 5), 14) - START)  # Off at 14:00 ...
    before = len(rec.checks)
    life.run(at(date(2026, 1, 6), 6) - rec.clock())  # ... and on again: the same day.
    assert per_day(rec.checks) == {date(2026, 1, 5): 3}
    assert before == sum(t < at(date(2026, 1, 5), 14) for t in rec.checks)


# Daily caps and restarts (2026-11-27): no chat, bug or restart loop makes the account
# busier than a person who keeps LinkedIn open on and off all day.


def test_capped_check_goes_back_to_the_plan():
    now = at(date(2026, 1, 5), 12)
    planned = schedule.planned_after(now, 3, "inbox", HOURS)
    for failed, watch in ((True, 0.0), (False, now + 3600)):
        due = schedule.next_inbox_check(now, watch, failed, now, poll_s=POLL, per_day=3,
                                        hours=HOURS, capped=True)
        assert due == planned


def test_a_chat_all_day_stays_under_the_daily_check_limit(life, monkeypatch):
    rec = life.rec
    monkeypatch.setattr(engine_module.CONFIG, "max_checks_per_day", 40)
    api_holder = {}

    async def chatty():
        """Writes again 4 minutes after each reply, from 8:30 until midnight."""
        api = api_holder["api"]
        await asyncio.sleep(at(date(2026, 1, 5), 8.5) - rec.clock())
        while rec.clock() < at(date(2026, 1, 6), 0):
            api.add("chat", text="and?", at=int(rec.clock() * 1000))
            answered = len(api.sent)
            while len(api.sent) == answered and rec.clock() < at(date(2026, 1, 6), 0):
                await asyncio.sleep(30)
            await asyncio.sleep(240)

    bot = life.build()
    api_holder["api"] = bot.api
    life.run(2 * DAY, bot, chatty())
    counts = per_day(rec.checks)
    assert counts[date(2026, 1, 5)] <= 40 + 3  # The limit, then only the planned checks.
    assert counts[date(2026, 1, 6)] <= 40 + 3
    assert per_day(bot.api.sent_at)[date(2026, 1, 5)] <= 40  # MAX_REPLIES_PER_DAY
    reads = [t for t, call, _ in rec.calls if call in ("list_threads", "list_messages")]
    assert max(per_day(reads).values()) < 400
    for moment, call, chrome in rec.calls:
        assert chrome and schedule.within(HOURS, datetime.fromtimestamp(moment)), call


def test_linkedin_opens_at_most_the_daily_limit(life, monkeypatch):
    rec = life.rec
    monkeypatch.setattr(engine_module.CONFIG, "max_opens_per_day", 2)
    life.run(3 * DAY)
    assert max(per_day(rec.opened).values()) <= 2
    for opened in rec.opened:  # Never opened at night, also after waiting out the limit.
        assert schedule.within(HOURS, datetime.fromtimestamp(opened))


def test_restart_delay_doubles_within_the_hour():
    assert engine_module.restart_delay([], 1000.0) == 0
    assert engine_module.restart_delay([1000.0 - 3700], 1000.0) == 0  # Over an hour ago.
    assert engine_module.restart_delay([990.0], 1000.0) == 60
    assert engine_module.restart_delay([980.0, 990.0], 1000.0) == 120
    assert engine_module.restart_delay([float(x) for x in range(990, 1000)], 1000.0) == 1800


def test_a_restart_loop_waits_longer_each_time(tmp_path, monkeypatch):
    clock = [at(date(2026, 1, 5), 10)]
    monkeypatch.setattr(engine_module, "time",
                        SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    store = Store(tmp_path / "state.db")
    waits = []

    async def idle(state, seconds):
        waits.append(seconds)
        clock[0] += seconds

    for _ in range(7):  # Crashes 5 seconds after each start.
        bot = Engine.__new__(Engine)
        bot.store, bot.idle = store, idle
        asyncio.run(bot.pace_restarts())
        clock[0] += 5
    assert waits == [60, 120, 240, 480, 960, 1800]
    clock[0] += 2 * 3600  # Ran fine for two hours, then one restart: no wait.
    bot = Engine.__new__(Engine)
    bot.store, bot.idle = store, idle
    asyncio.run(bot.pace_restarts())
    assert len(waits) == 6
