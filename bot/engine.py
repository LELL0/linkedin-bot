"""Main loop: look for invitations and messages a few times a day, follow a chat closely
while it goes on, and answer each thread once it goes quiet."""

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import date, datetime

from . import metrics, schedule, settings
from .brain import Brain, render_message
from .config import CONFIG
from .guard import Scope, in_scope, predates_start, unanswered
from .health import alert, stale_poll_s, start_watchdog, write_status
from .linkedin import INBOX_CATEGORIES, MESSAGE_PAGE, AuthError, LinkedIn
from .models import Message, Thread
from .session import Session, proxy_problem
from .store import Store
from .enc import (EncControl, catchup_details, catchup_limit_text, catchup_text, days_text,
                  duration, title)

log = logging.getLogger(__name__)

MAX_REGENERATIONS = 2
SEND_ATTEMPTS = 3
HEARTBEAT_S = 60
# Chrome stays open when the next check or invitation round is this close, rather than
# closing and opening again minutes later.
KEEP_OPEN_S = 600
# Answers that mean "slow down" rather than "signed out".
PUSHBACK = re.compile(r"HTTP (429|999|5\d\d)\b")
# A start this soon after other starts waits before Chrome opens LinkedIn: 1 minute after one
# start in the last hour, doubling with each further one up to 30 minutes. A crash or restart
# loop would otherwise load LinkedIn every few seconds.
RESTART_WINDOW_S = 3600.0
RESTART_PAUSE_S = (60.0, 1800.0)
# Laptop sleep: the wall clock moved this much more than the event loop's clock.
SLEPT_S = 60.0
# The daily limits, as the alert names them, and what happens until the next day.
LIMITS = {
    "replies": ("reply", "the rest go tomorrow"),
    "accepts": ("accept", "the rest go tomorrow"),
    "checks": ("check", "planned ones only now"),
    "opens": ("open", "closed until tomorrow"),
}


def restart_delay(starts: list[float], now: float) -> float:
    """How long a start waits before it opens LinkedIn, given the earlier starts."""
    recent = [s for s in starts if 0 <= now - s < RESTART_WINDOW_S]
    if not recent:
        return 0.0
    first, longest = RESTART_PAUSE_S
    return min(first * 2 ** (len(recent) - 1), longest)


def in_active_hours(now: datetime | None = None) -> bool:
    """Whether the bot may work now. A range like 8-2 runs past midnight."""
    return schedule.within(CONFIG.active_hours, now or datetime.now())


def meta_time(store: Store, key: str) -> float:
    """A time kept in the store's meta (written with repr, so exact), or 0 when there is none."""
    try:
        return float(store.get_meta(key, "0") or 0)
    except ValueError:
        return 0.0


class Engine:
    def __init__(self) -> None:
        self.session = Session()
        self.api = LinkedIn(self.session)
        self.store = Store(CONFIG.db_path)
        for name in settings.load(self.store):
            log.warning("Stored setting %s is not valid any more, back to its default", name)
        self.poll_now = asyncio.Event()  # Set when a chat starts or a timing setting changes.
        self.invites_replan = asyncio.Event()  # Set when the invitation settings change.
        self.brain: Brain | None = None
        self.scope: Scope | None = None
        self.workers: dict[str, asyncio.Task] = {}
        self.recheck: set[str] = set()
        self.seen_activity: dict[str, int] = {}
        # Fine as of this time: the last good check, or a wait on purpose after one. The
        # health check and the watchdog judge stalls on it.
        self.last_ok = time.time()
        self.checked_at = 0.0  # Last inbox check that worked.
        self.trigger = "poll"  # What started the current check: poll or realtime.
        self.replies_sent = 0
        self.accepted = 0
        self.state = "starting"
        self.recovering = asyncio.Lock()
        self.opening = asyncio.Lock()
        self.backoff_until = 0.0  # No LinkedIn request before this time.
        self.failures = 0  # Push-backs in a row; each one doubles the wait.
        # Kept across restarts, so a restart neither repeats a check nor forgets a chat.
        self.last_check = meta_time(self.store, "last_check_at")
        self.last_invites = meta_time(self.store, "invites_at")
        self.watch_until = meta_time(self.store, "watch_until")  # Following a chat until then.
        self.check_failed = False
        self.next_check = self.next_invites = 0.0
        self.busy = 0  # Checks and invitation rounds going on now; Chrome stays open for them.
        self.capped: set[str] = set()
        # /catchup: thread urn -> answer messages after this time (ms), whatever the scope.
        self.catchup_since: dict[str, int] = {}
        self.catchup_task: asyncio.Task | None = None  # Daily limits already announced today.
        self.catchup_progress: tuple[int, int] | None = None  # answered, of how many
        self.asleep = False
        self.started = False  # The first wake is done and the bot knows who it is.
        # Owner settings from Enc Chat, kept across restarts.
        self.paused = self.store.get_meta("paused") == "1"
        self.replies_feed = self.store.get_meta("replies_feed") == "1"
        verbose = self.store.get_meta("verbose", "0")
        self.verbose = int(verbose) if verbose.isdigit() else 0
        self.enc = EncControl(self)

    async def run(self) -> None:
        self.heartbeat = time.time()
        start_watchdog(lambda: self.heartbeat)
        self.session.on_closed = self.on_browser_closed
        await asyncio.gather(
            self.start_and_poll(), self.invite_loop(), self.maintenance_loop(),
            self.browser_loop(), self.enc.loop(),
        )

    async def start_and_poll(self) -> None:
        await self.pace_restarts()
        await self.open_chrome()
        await self.api.load_self()
        self.scope = Scope(CONFIG.reply_scope, self.store.watermark(), self.api.self_id)
        self.brain = Brain(self.api.first_name, self.api.headline)
        log.info("Running. scope=%s dry_run=%s", self.scope.mode, CONFIG.dry_run)
        self.started = True
        await self.poll_loop()

    async def pace_restarts(self) -> None:
        """Wait before the first LinkedIn request when the bot started several times within
        the hour (restart_delay). The starts are kept in the store across restarts."""
        try:
            starts = [float(s) for s in json.loads(self.store.get_meta("starts", "[]"))]
        except (TypeError, ValueError):
            starts = []
        now = time.time()
        wait = restart_delay(starts, now)
        recent = [s for s in starts if 0 <= now - s < RESTART_WINDOW_S][-9:]
        self.store.set_meta("starts", json.dumps([*recent, now + wait]))
        if wait:
            log.warning("Started %d times within the hour: waiting %ds before opening LinkedIn",
                        len(recent) + 1, wait)
            await self.idle("starting", wait)

    async def idle(self, state: str, seconds: float) -> None:
        """Wait without touching LinkedIn, still writing the status for the watchdog."""
        self.state = state
        deadline = time.time() + seconds
        while (left := deadline - time.time()) > 0:
            self.write_status()
            await asyncio.sleep(min(30.0, left))
        self.write_status()

    async def wake(self) -> None:
        """Open Chrome on LinkedIn: only within active hours, and only through the proxy
        when one is set. A person's laptop is not on LinkedIn at 3 a.m., nor from a
        datacenter."""
        while True:
            if not in_active_hours():
                await self.idle("asleep", 60)
            elif CONFIG.max_opens_per_day and self.at_limit("opens", CONFIG.max_opens_per_day):
                await self.idle("resting", 600)  # Then the night, then a new day's count.
            else:
                break
        warned = False
        while CONFIG.proxy and (problem := await asyncio.to_thread(proxy_problem, CONFIG.proxy)):
            if not warned:
                if problem.startswith("BOT_PROXY"):
                    alert("proxy setting wrong", problem, "🔴")
                else:  # The bot never falls back to the server's own address.
                    alert("home tunnel down", "start it so LinkedIn can open", "🔴")
                log.warning("Not opening LinkedIn: %s", problem)
                warned = True
            await self.idle("no_proxy", 60)
        if warned:
            log.info("Proxy answers again, opening LinkedIn")
        self.count("opens")
        await self.session.start()
        await self.wait_for_session()

    async def open_chrome(self) -> None:
        """Open Chrome unless it is open; the loops share one window."""
        async with self.opening:
            if not self.session.open:
                await self.wake()

    async def ready(self) -> None:
        """Hold until LinkedIn may be called: within active hours, not paused, no back-off,
        and Chrome open and signed in. Chrome opens here, only when a check, an invitation
        round or a catch-up needs it."""
        while True:
            if not self.session.open and in_active_hours() and not self.paused \
                    and time.time() >= self.backoff_until:
                try:
                    await self.open_chrome()
                except Exception:
                    log.exception("Could not open Chrome, trying again in 5 minutes")
                    await self.session.stop()
                    await self.idle("resting", 300)
            if await self.may_call():
                return

    def needs_chrome(self) -> bool:
        """Whether Chrome should stay open: a check, round, reply or catch-up is going on,
        a chat is being followed, or the next check is minutes away. At night only a
        reply or a check already under way keeps it."""
        busy = self.busy or self.workers or (self.catchup_task and not self.catchup_task.done())
        if busy or not in_active_hours():
            return bool(busy)
        now = time.time()
        soon = any(0 <= due - now <= KEEP_OPEN_S for due in (self.next_check, self.next_invites))
        return now < self.watch_until or soon

    async def browser_loop(self) -> None:
        """Close Chrome when nothing needs it: between checks, and for the night. A person
        does not keep LinkedIn open all day to read messages three times."""
        while True:
            await asyncio.sleep(60)
            if not self.started or not self.session.open or self.state != "running":
                continue  # start_and_poll opens Chrome the first time.
            if self.needs_chrome():
                continue
            if in_active_hours():
                log.info("Closing Chrome until the next check")
                self.state = "resting"
            else:
                log.info("Outside active hours %s: closing Chrome until then", CONFIG.active_hours)
                self.state = "asleep"
            self.write_status()
            await self.session.stop()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.store.set_meta("paused", "1" if paused else "0")
        log.info("Paused" if paused else "Resumed")

    def set_replies_feed(self, on: bool) -> None:
        self.replies_feed = on
        self.store.set_meta("replies_feed", "1" if on else "0")

    def set_verbose(self, level: int) -> None:
        self.verbose = level
        self.store.set_meta("verbose", str(level))

    async def wait_for_session(self) -> None:
        """Hold until a person has fixed a logout or checkpoint."""
        warned = False
        while not (await self.session.has_auth_cookie() and self.session.signed_in):
            if not warned:
                alert("sign-in needed", "sign in again over VNC", "🔴")
                warned = True
            self.state = "needs_login"
            self.write_status()
            await asyncio.sleep(5)
        self.state = "running"
        self.last_ok = time.time()
        self.write_status()

    def write_status(self) -> None:
        self.heartbeat = time.time()
        try:
            write_status(
                self.state, self.last_ok, accepted=self.accepted, replies=self.replies_sent
            )
        except OSError as error:
            log.warning("Could not write status: %s", error)

    async def may_call(self) -> bool:
        """Whether LinkedIn may be called now. If not, waits a little and returns False.

        Both loops ask this first, so a signed-out session, a push-back or the night
        stops every request, not only the ones of the loop that noticed it."""
        if self.state != "running":
            await asyncio.sleep(5)  # wait_for_session is waiting for a person.
            return False
        if self.paused or time.time() < self.backoff_until or not in_active_hours():
            self.last_ok = time.time()  # Quiet on purpose: not a stall.
            if not self.paused and not in_active_hours() and not self.asleep:
                log.info("Outside active hours %s: no requests until then", CONFIG.active_hours)
            self.asleep = not in_active_hours()
            await asyncio.sleep(min(30.0, max(1.0, self.backoff_until - time.time())))
            return False
        self.asleep = False
        return True

    def push_back(self, reason: str) -> None:
        """Stop every request for a while, longer each time it happens in a row."""
        self.failures += 1
        wait = min(CONFIG.backoff_s * 2 ** (self.failures - 1), CONFIG.max_backoff_s)
        wait *= random.uniform(1.0, 1.3)
        self.backoff_until = time.time() + wait
        log.warning("LinkedIn pushed back (%s), no requests for %ds", reason, wait)
        alert("on hold", f"LinkedIn pushed back · back in {duration(round(wait / 60) * 60)}",
              "🟡")

    async def on_error(self, error: Exception, what: str) -> None:
        if isinstance(error, AuthError):
            log.error("Auth problem on %s: %s", what, error)
            async with self.recovering:
                if self.state != "running":
                    return  # The other loop is already recovering.
                self.state = "needs_login"
                await self.recover()
                self.push_back(f"session rejected: {error}")
        elif PUSHBACK.search(str(error)):
            log.warning("%s: %s", what, error)
            if time.time() >= self.backoff_until:
                self.push_back(str(error))
        else:
            log.exception("%s failed", what)

    async def poll_loop(self) -> None:
        while True:
            await self.sleep_until_next_poll()
            await self.ready()
            self.busy += 1
            try:
                gap = CONFIG.min_check_gap_s - (time.time() - self.last_check)
                if gap > 0:
                    await asyncio.sleep(gap)
                self.last_check = time.time()
                self.store.set_meta("last_check_at", repr(self.last_check))
                self.count("checks")
                try:
                    await self.check_threads()
                    self.last_ok = self.checked_at = time.time()
                    self.failures = 0
                    self.check_failed = False
                except Exception as error:
                    self.check_failed = True
                    await self.on_error(error, "inbox check")
            finally:
                self.busy -= 1

    def plan_check(self) -> float:
        capped = bool(CONFIG.max_checks_per_day) and self.at_limit(
            "checks", CONFIG.max_checks_per_day)
        self.next_check = schedule.next_inbox_check(
            self.last_check, self.watch_until, self.check_failed, time.time(),
            poll_s=CONFIG.poll_s, per_day=CONFIG.inbox_checks, hours=CONFIG.active_hours,
            capped=capped,
        )
        return self.next_check

    async def sleep_until_next_poll(self) -> None:
        """Wait for the next inbox check (schedule.next_inbox_check). When a chat starts or a
        timing setting changes, the time is worked out again from the last check."""
        due = self.plan_check()
        while (left := due - time.time()) > 0:
            self.poll_now.clear()
            try:
                await asyncio.wait_for(self.poll_now.wait(), min(left, 60.0))
                due = self.plan_check()
            except TimeoutError:
                pass
            if not self.check_failed:
                self.last_ok = time.time()  # Waiting on purpose after a good check: no stall.

    def watch(self, at: float) -> None:
        """Follow a chat: check every CONFIG.poll_s until CONFIG.watch_s after [at], its
        latest message or reply. A later one moves the end on."""
        until = at + CONFIG.watch_s
        if until <= self.watch_until:
            return
        if self.watch_until <= time.time():
            log.info("A chat is going on: checking every %s until %s after it goes quiet",
                     settings.show("poll"), settings.show("watch"))
        self.watch_until = until
        self.store.set_meta("watch_until", repr(until))
        self.poll_now.set()

    async def invite_loop(self) -> None:
        while not self.started:
            await asyncio.sleep(5)
        while True:
            due = self.plan_invites()
            while (left := due - time.time()) > 0:
                self.invites_replan.clear()
                try:
                    # A minute at most, so laptop sleep does not push the round back.
                    await asyncio.wait_for(self.invites_replan.wait(), min(left, 60.0))
                    due = self.plan_invites()
                except TimeoutError:
                    pass
            await self.ready()
            failed = False
            self.busy += 1
            try:
                await self.check_invitations()
                self.last_invites = time.time()
                self.store.set_meta("invites_at", repr(self.last_invites))
            except Exception as error:
                failed = True
                await self.on_error(error, "invitation check")
            finally:
                self.busy -= 1
            if failed:  # The round is still due: try again later, Chrome may close meanwhile.
                await asyncio.sleep(random.uniform(*schedule.RETRY_S))

    def plan_invites(self) -> float:
        self.next_invites = schedule.next_invite_round(
            self.last_invites, time.time(), per_day=CONFIG.invite_rounds,
            hours=CONFIG.active_hours,
        )
        return self.next_invites

    def change_setting(self, name: str, value: object | None) -> None:
        """Set (or with None, reset) a setting from Enc Chat and keep it across restarts."""
        if value is None:
            settings.reset(name, self.store)
        else:
            settings.apply(name, value, self.store)
        log.info("Setting %s = %s", name, settings.show(name))
        if name == "scope" and self.scope is not None:
            self.scope = Scope(CONFIG.reply_scope, self.scope.watermark, self.scope.self_id)
        if name in ("poll", "check_gap", "checks", "watch", "active_hours"):
            self.poll_now.set()
        if name in ("invites", "active_hours"):
            self.invites_replan.set()
        self.write_status()

    async def recover(self) -> None:
        await self.session.page.goto("https://www.linkedin.com/messaging/", wait_until="domcontentloaded")
        await self.wait_for_session()

    async def unanswered_since(self, days: float) -> list[Thread]:
        """One-to-one threads whose last message is theirs, unanswered, from the last `days`."""
        since = int((time.time() - days * 86400) * 1000)
        found: dict[str, Thread] = {}
        for category in INBOX_CATEGORIES:
            before = None
            for _ in range(10):  # 20 threads a page
                await asyncio.sleep(random.uniform(3, 8) if before else 0)
                threads = await self.api.list_threads(category, before)
                for thread in threads:
                    if thread.last_activity_at >= since:
                        found.setdefault(thread.urn, thread)
                if len(threads) < 20 or min(t.last_activity_at for t in threads) < since:
                    break
                before = min(t.last_activity_at for t in threads)
        result = []
        for thread in found.values():
            if thread.group_chat or thread.has_organization or "SPAM" in thread.categories:
                continue
            if thread.urn in self.workers:
                continue  # Already being answered.
            await asyncio.sleep(random.uniform(2, 5))
            thread.messages = await self.api.list_messages(thread.urn)
            scope = Scope("all", since, self.scope.self_id)
            if unanswered(thread, scope, self.store.is_answered):
                result.append(thread)
        return sorted(result, key=lambda t: t.last_activity_at)

    def start_catchup(self, days: float, report) -> bool:
        """Start answering old unanswered messages. False if a catch-up is already going."""
        if self.catchup_task and not self.catchup_task.done():
            return False
        self.catchup_task = asyncio.create_task(self.catchup(days, report))
        return True

    def stop_catchup(self) -> tuple[int, int] | None:
        """Cancel the catch-up. Its progress (answered, of how many), or None when none is
        going. /catchup stop tells the owner, so the catch-up itself says nothing more."""
        if self.catchup_task and not self.catchup_task.done():
            self.catchup_task.cancel()
            return self.catchup_progress or (0, 0)
        return None

    async def catchup(self, days: float, report) -> None:
        """Answer every unanswered message of the last `days`, one thread at a time with a
        long pause between them, within active hours, daily limits and back-offs."""
        self.catchup_progress = None
        try:
            await self.ready()
            since = int((time.time() - days * 86400) * 1000)
            threads = await self.unanswered_since(days)
            if not threads:
                await report(title("✅", "catch-up done",
                                   f"nothing unanswered, last {days_text(days)}"))
                return
            names = [next(iter(t.names.values()), "?") for t in threads]
            low, high = CONFIG.catchup_gap_s
            await report(catchup_text(names, days, (low, high)),
                         catchup_details(names, days, (low, high)))
            answered = 0
            self.catchup_progress = (answered, len(threads))
            for number, thread in enumerate(threads):
                if number:
                    await asyncio.sleep(random.uniform(low, high))
                await self.ready()
                if self.at_limit("replies", CONFIG.max_replies_per_day):
                    await report(catchup_limit_text(answered, len(threads) - number))
                    return
                before = self.replies_sent
                self.catchup_since[thread.urn] = since
                try:
                    await self.answer_thread(thread, time.time(), "catchup")
                finally:
                    self.catchup_since.pop(thread.urn, None)
                answered += self.replies_sent > before
                self.catchup_progress = (answered, len(threads))
            await report(title("✅", "catch-up done", f"{answered} of {len(threads)} answered"))
        except Exception as error:  # The log has the error; the owner gets what to do.
            await self.on_error(error, "catch-up")
            await report(title("❌", "catch-up failed", "try /catchup again later"))

    def on_browser_closed(self, what: str) -> None:
        """Exit so the supervisor (Docker or run.sh) starts the bot with a fresh Chrome."""
        alert("restarting", f"{what} · opening a new one", "⏳")
        logging.shutdown()
        os._exit(1)  # sys.exit inside a callback would not stop the loop.

    async def maintenance_loop(self) -> None:
        while True:
            wall, clock = time.time(), time.monotonic()
            await asyncio.sleep(HEARTBEAT_S)
            slept = (time.time() - wall) - (time.monotonic() - clock)
            if slept > SLEPT_S:
                # The laptop slept: no check could run, so the gap is not a stall. The next
                # due check runs once, now, never one per missed slot.
                log.info("Woke up after %s asleep", duration(slept))
                self.last_ok = max(self.last_ok, time.time())
            log.info(
                "alive: accepted=%d replies=%d realtime_events=%d topics=%s workers=%d last_ok=%ds ago",
                self.accepted,
                self.replies_sent,
                self.session.realtime_events,
                dict(self.session.realtime_topics),
                len(self.workers),
                time.time() - self.last_ok,
            )
            self.write_status()
            stalled = time.time() - self.last_ok
            limit = stale_poll_s()
            if self.state == "running" and stalled > limit:
                # The same words every minute, so the alert comes once an hour.
                alert("inbox checks failing", f"none worked for over {duration(limit)}", "🔴")
            if self.state == "running" and stalled > 2 * limit:
                # Exit so the supervisor (systemd or Docker) starts a fresh browser.
                alert("restarting", f"no inbox check for {duration(stalled)}", "⏳")
                logging.shutdown()
                os._exit(1)  # sys.exit inside a task may not stop the loop.
            # No page reloads: Chrome opens fresh when checks start again, and reloads all
            # day would show LinkedIn a tab nobody looks at.

    @staticmethod
    def day() -> date:
        return datetime.fromtimestamp(time.time()).date()

    def today(self, kind: str) -> int:
        value = self.store.get_meta(f"{kind}:{self.day().isoformat()}", "0")
        return int(value) if value.isdigit() else 0

    def count(self, kind: str) -> None:
        self.store.set_meta(f"{kind}:{self.day().isoformat()}", str(self.today(kind) + 1))

    def at_limit(self, kind: str, limit: int) -> bool:
        """Whether today's limit (LIMITS) is used up. Tells the owner once a day."""
        if self.today(kind) < limit:
            return False
        key = f"{kind}:{self.day().isoformat()}"
        if key not in self.capped:
            self.capped.add(key)
            noun, then = LIMITS.get(kind, (kind, "the rest go tomorrow"))
            alert(f"daily {noun} limit", f"{limit} today · {then}", "🟡")
        return True

    async def check_invitations(self) -> None:
        """Accept every pending person-to-person invite, a short pause between each. The
        list returns 20 at a time, so it is read again until nothing is left to accept."""
        for _ in range(10):
            if not await self.accept_round():
                return

    async def accept_round(self) -> int:
        checked_at = time.time()
        tried = done = 0
        for invitation in await self.api.list_invitations():
            if self.store.is_seen_invite(invitation.urn) or invitation.kind != "CONNECTION":
                continue
            if invitation.sent_at <= self.scope.watermark:
                self.store.add_seen_invite(invitation.urn)  # Older than the bot: leave it alone.
                continue
            if CONFIG.max_accepts_per_day and self.at_limit(
                "accepts", CONFIG.max_accepts_per_day
            ):
                return 0
            if tried:
                await asyncio.sleep(random.uniform(*CONFIG.accept_gap_s))
            tried += 1
            if CONFIG.dry_run:
                log.info("DRY RUN: would accept an invitation")
                done += 1
            elif via := await self.api.accept(invitation):
                self.accepted += 1
                now, sent = time.time(), invitation.sent_at / 1000
                timing = {
                    "total_s": round(now - sent, 2),
                    "detect_s": round(checked_at - sent, 2),
                    "action_s": round(now - checked_at, 2),
                    "via": via,
                }
                self.count("accepts")
                done += 1
                metrics.record("accept", **timing)
                log.info("Accepted invitation: %s", timing)
            else:
                alert("accept failed", "one invite · it tries again later", "❌")
                continue
            self.store.add_accepted(invitation.inviter_id)
            self.store.add_seen_invite(invitation.urn)
        return done

    async def check_threads(self) -> None:
        for category in INBOX_CATEGORIES:
            for thread in await self.api.list_threads(category):
                changed = self.seen_activity.get(thread.urn) != thread.last_activity_at
                if not changed or thread.last_activity_at <= self.scope.watermark:
                    continue
                self.seen_activity[thread.urn] = thread.last_activity_at
                worker = self.workers.get(thread.urn)
                if worker is None or worker.done():
                    self.workers[thread.urn] = asyncio.create_task(
                        self.answer_thread(thread, time.time(), self.trigger)
                    )
                else:
                    self.recheck.add(thread.urn)  # Arrived mid-answer: look again afterwards.

    async def answer_thread(self, thread: Thread, noticed_at: float, trigger: str) -> None:
        try:
            await self._answer_thread(thread, noticed_at, trigger)
        except AuthError as error:
            log.error("Auth problem while answering: %s", error)
            self.seen_activity.pop(thread.urn, None)  # Retry on the next poll.
        except Exception:
            log.exception("Answering a thread failed")
            self.seen_activity.pop(thread.urn, None)
        finally:
            self.workers.pop(thread.urn, None)
            if thread.urn in self.recheck:
                self.recheck.discard(thread.urn)
                self.seen_activity.pop(thread.urn, None)

    async def pending_for(self, thread: Thread) -> tuple[list[Message], list[Message]]:
        history = await self.api.list_messages(thread.urn)
        since = self.catchup_since.get(thread.urn)
        if since is not None:  # Asked for with /catchup: older threads count too.
            thread.messages = history
            if thread.group_chat or thread.has_organization or "SPAM" in thread.categories:
                return history, []
            scope = Scope("all", since, self.scope.self_id)
            return history, unanswered(thread, scope, self.store.is_answered)
        pre_existing = not self.store.is_new_thread(thread.urn) and predates_start(
            history, self.scope.watermark, MESSAGE_PAGE
        )
        if history and not pre_existing:
            self.store.add_new_thread(thread.urn)  # Stays new once the page fills up.
        thread.messages = history
        if not in_scope(thread, self.scope, pre_existing, self.store.is_accepted):
            return history, []
        pending = unanswered(thread, self.scope, self.store.is_answered)
        if pre_existing and self.scope.mode != "all":
            # An old thread opened by an accept only counts from that moment on.
            since = max((self.store.accepted_at(p) for p in thread.participants), default=0)
            pending = [m for m in pending if m.delivered_at > since]
        return history, pending

    async def wait_for_quiet(self, thread: Thread) -> None:
        """Let a burst finish so it gets one reply, without waiting past the limit."""
        started = time.monotonic()
        while time.monotonic() - started < CONFIG.debounce_max_s:
            _, pending = await self.pending_for(thread)
            if not pending:
                return
            silence = time.time() - pending[-1].delivered_at / 1000
            if silence >= CONFIG.debounce_quiet_s:
                return
            await asyncio.sleep(min(1.0, CONFIG.debounce_quiet_s - silence + 0.05))

    async def _answer_thread(self, thread: Thread, noticed_at: float, trigger: str) -> None:
        if self.at_limit("replies", CONFIG.max_replies_per_day):
            self.seen_activity.pop(thread.urn, None)  # Answered once the limit resets.
            return
        # A person does not answer the second a message lands. Later messages in this wait
        # are merged by wait_for_quiet.
        await asyncio.sleep(random.uniform(*CONFIG.reply_delay_s))
        await self.wait_for_quiet(thread)
        quiet_at = time.time()
        history, pending = await self.pending_for(thread)
        if not pending:
            return
        self.watch(pending[-1].delivered_at / 1000)  # A chat may be going on: follow it.
        await self.api.mark_read(thread.urn)
        await self.api.show_typing(thread.urn)

        trace = {"trigger": trigger, "redrafts": -1}
        for _ in range(MAX_REGENERATIONS + 1):
            trace["redrafts"] += 1
            others = [p for p in thread.participants if p != self.scope.self_id]
            sender = thread.names.get(others[0], "") if others else ""
            reply = await self.brain.reply(history, pending, self.scope.self_id, sender, trace)
            history, latest = await self.pending_for(thread)
            if not latest:
                return  # Recalled, or answered by hand in the meantime.
            if {m.urn for m in latest} == {m.urn for m in pending}:
                break
            pending = latest  # More arrived while thinking, so answer those too.

        if CONFIG.reply_delay_s[1] > 0:  # Typing takes time; the indicator is on.
            await asyncio.sleep(min(30.0, len(reply) / 8) * random.uniform(0.7, 1.2))
        first = pending[0].delivered_at / 1000
        trace |= {"detect_s": round(noticed_at - first, 2), "quiet_s": round(quiet_at - noticed_at, 2)}
        await self.send(thread, reply, pending, trace)

    async def send(self, thread: Thread, reply: str, pending: list[Message], trace: dict) -> None:
        if CONFIG.dry_run:
            log.info("DRY RUN: would send %d chars: %s", len(reply), trace)
            self.store.mark_answered([m.urn for m in pending])
            self.watch(time.time())
            return
        # Checked again here, and counted before sending, because other threads may have
        # replied while this one was waiting and drafting.
        if self.at_limit("replies", CONFIG.max_replies_per_day):
            self.seen_activity.pop(thread.urn, None)
            return
        self.count("replies")
        started = time.time()
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                await self.api.send_reply(thread.urn, reply)
                self.store.mark_answered([m.urn for m in pending])
                self.replies_sent += 1
                now = time.time()
                self.watch(now)  # The answer may come soon: keep checking every few minutes.
                trace |= {
                    "total_s": round(now - pending[0].delivered_at / 1000, 2),
                    "since_last_s": round(now - pending[-1].delivered_at / 1000, 2),
                    "send_s": round(now - started, 2),
                    "messages": len(pending),
                    "chars": len(reply),
                    "attempts": attempt,
                }
                metrics.record("reply", **trace)
                log.info("Replied: %s", trace)
                others = [p for p in thread.participants if p != self.scope.self_id]
                sender = thread.names.get(others[0], "") if others else ""
                await self.enc.reply_sent(
                    sender, [render_message(m) for m in pending], reply
                )
                return
            except RuntimeError as error:
                log.warning("Send attempt %d failed: %s", attempt, error)
                await asyncio.sleep(attempt)
        alert("reply not sent", f"failed {SEND_ATTEMPTS} times · retried next check", "❌")
        raise RuntimeError("Send failed")
