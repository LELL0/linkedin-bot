"""Main loop: watch for invitations and messages, and answer each thread once it goes quiet."""

import asyncio
import logging
import random
import subprocess
import time

from . import metrics
from .brain import Brain
from .config import CONFIG
from .guard import Scope, in_scope, predates_start, unanswered
from .linkedin import INBOX_CATEGORIES, MESSAGE_PAGE, AuthError, LinkedIn
from .models import Message, Thread
from .session import Session
from .store import Store

log = logging.getLogger(__name__)

MAX_REGENERATIONS = 2
SEND_ATTEMPTS = 3
PAGE_REFRESH_S = 45 * 60
HEARTBEAT_S = 60


def alert(text: str) -> None:
    log.error("ALERT: %s", text)
    subprocess.run(
        ["notify-send", "-u", "critical", "LinkedIn bot", text], check=False, capture_output=True
    )


class Engine:
    def __init__(self) -> None:
        self.session = Session()
        self.api = LinkedIn(self.session)
        self.store = Store(CONFIG.db_path)
        self.brain: Brain | None = None
        self.scope: Scope | None = None
        self.workers: dict[str, asyncio.Task] = {}
        self.recheck: set[str] = set()
        self.seen_activity: dict[str, int] = {}
        self.hot_until = 0.0
        self.last_ok = time.time()
        self.trigger = "poll"  # What started the current check: poll or realtime.
        self.replies_sent = 0
        self.accepted = 0

    async def run(self) -> None:
        await self.session.start()
        await self.wait_for_session()
        await self.api.load_self()
        self.scope = Scope(CONFIG.reply_scope, self.store.watermark(), self.api.self_id)
        self.brain = Brain(self.api.first_name, self.api.headline)
        log.info("Running. scope=%s dry_run=%s", self.scope.mode, CONFIG.dry_run)
        await asyncio.gather(self.poll_loop(), self.maintenance_loop())

    async def wait_for_session(self) -> None:
        """Hold until a person has fixed a logout or checkpoint."""
        warned = False
        while not (await self.session.has_auth_cookie() and self.session.signed_in):
            if not warned:
                alert("Signed out or checkpoint. Sign in again in the bot's Chrome window.")
                warned = True
            await asyncio.sleep(5)

    async def poll_loop(self) -> None:
        while True:
            try:
                await self.check_invitations()
                await self.check_threads()
                self.last_ok = time.time()
            except AuthError as error:
                log.error("Auth problem: %s", error)
                await self.recover()
            except Exception:
                log.exception("Poll failed")
            await self.sleep_until_next_poll()

    async def sleep_until_next_poll(self) -> None:
        hot = time.time() < self.hot_until
        delay = CONFIG.hot_poll_s if hot else CONFIG.idle_poll_s * random.uniform(0.8, 1.2)
        try:
            await asyncio.wait_for(self.session.wake.wait(), timeout=delay)
            self.trigger = "realtime"
        except TimeoutError:
            self.trigger = "poll"
        self.session.wake.clear()

    async def recover(self) -> None:
        await self.session.page.goto("https://www.linkedin.com/messaging/", wait_until="domcontentloaded")
        await self.wait_for_session()

    async def maintenance_loop(self) -> None:
        last_refresh = time.time()
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            log.info(
                "alive: accepted=%d replies=%d realtime_events=%d topics=%s workers=%d last_ok=%ds ago",
                self.accepted,
                self.replies_sent,
                self.session.realtime_events,
                dict(self.session.realtime_topics),
                len(self.workers),
                time.time() - self.last_ok,
            )
            if time.time() - self.last_ok > 180:
                alert("No successful poll for 3 minutes.")
            idle = not self.workers and time.time() > self.hot_until
            if idle and time.time() - last_refresh > PAGE_REFRESH_S:
                await self.session.page.reload(wait_until="domcontentloaded")
                last_refresh = time.time()

    async def check_invitations(self) -> None:
        checked_at = time.time()
        for invitation in await self.api.list_invitations():
            if self.store.is_seen_invite(invitation.urn) or invitation.kind != "CONNECTION":
                continue
            if invitation.sent_at <= self.scope.watermark:
                self.store.add_seen_invite(invitation.urn)  # Older than the bot: leave it alone.
                continue
            if CONFIG.dry_run:
                log.info("DRY RUN: would accept an invitation")
            elif via := await self.api.accept(invitation):
                self.accepted += 1
                now, sent = time.time(), invitation.sent_at / 1000
                timing = {
                    "total_s": round(now - sent, 2),
                    "detect_s": round(checked_at - sent, 2),
                    "action_s": round(now - checked_at, 2),
                    "via": via,
                    "trigger": self.trigger,
                }
                metrics.record("accept", **timing)
                log.info("Accepted invitation: %s", timing)
            else:
                alert("Could not accept an invitation.")
                continue
            self.store.add_accepted(invitation.inviter_id)
            self.store.add_seen_invite(invitation.urn)

    async def check_threads(self) -> None:
        for category in INBOX_CATEGORIES:
            for thread in await self.api.list_threads(category):
                changed = self.seen_activity.get(thread.urn) != thread.last_activity_at
                if not changed or thread.last_activity_at <= self.scope.watermark:
                    continue
                self.seen_activity[thread.urn] = thread.last_activity_at
                self.hot_until = time.time() + CONFIG.hot_window_s
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
        pre_existing = not self.store.is_new_thread(thread.urn) and predates_start(
            history, self.scope.watermark, MESSAGE_PAGE
        )
        if history and not pre_existing:
            self.store.add_new_thread(thread.urn)  # Stays new once the page fills up.
        thread.messages = history
        if not in_scope(thread, self.scope, pre_existing, self.store.is_accepted):
            return history, []
        return history, unanswered(thread, self.scope, self.store.is_answered)

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
        await self.wait_for_quiet(thread)
        quiet_at = time.time()
        history, pending = await self.pending_for(thread)
        if not pending:
            return
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

        first = pending[0].delivered_at / 1000
        trace |= {"detect_s": round(noticed_at - first, 2), "quiet_s": round(quiet_at - noticed_at, 2)}
        await self.send(thread, reply, pending, trace)

    async def send(self, thread: Thread, reply: str, pending: list[Message], trace: dict) -> None:
        if CONFIG.dry_run:
            log.info("DRY RUN: would send %d chars: %s", len(reply), trace)
            self.store.mark_answered([m.urn for m in pending])
            return
        started = time.time()
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                await self.api.send_reply(thread.urn, reply)
                self.store.mark_answered([m.urn for m in pending])
                self.replies_sent += 1
                now = time.time()
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
                return
            except RuntimeError as error:
                log.warning("Send attempt %d failed: %s", attempt, error)
                await asyncio.sleep(attempt)
        alert("Could not send a reply after retries.")
        raise RuntimeError("Send failed")
