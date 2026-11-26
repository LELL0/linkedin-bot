"""When the bot looks at LinkedIn, the way a person does: a few times a day, at moments that
change every day, and every few minutes while a chat goes on.

Pure functions of the clock and the settings, so tests can run months of days in a second.
Times are Unix seconds; hours are local, like CONFIG.active_hours.
"""

import hashlib
import random
from datetime import date, datetime, timedelta

# After a check that failed, look again in 5-15 minutes rather than at the next planned time.
RETRY_S = (300.0, 900.0)
# A check missed while the bot was off or signed out runs this long after it can.
LATE_S = (30.0, 180.0)
# Each planned moment keeps this share of its part of the day free on both sides, so two
# moments are never close: with 3 checks in 8-24, at least 1.6 hours apart.
EDGE = 0.15


def within(hours: tuple[float, float], at: datetime) -> bool:
    """Whether [at] falls in the active hours. A range like 8-2 runs past midnight."""
    start, end = hours
    hour = at.hour + at.minute / 60
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def spread(day: date, salt: str) -> float:
    """A number in [0, 1) fixed for the day and salt: a restart keeps the day's plan."""
    digest = hashlib.sha256(f"{day.isoformat()}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def day_plan(day: date, count: int, salt: str, hours: tuple[float, float]) -> list[datetime]:
    """[count] moments in the active hours that start on [day]: the hours cut in equal parts,
    one moment at a random point of each part, away from its edges."""
    start, end = hours
    span = (end - start) % 24 or 24
    part = span / count
    first = datetime.combine(day, datetime.min.time()) + timedelta(hours=start)
    return [
        first + timedelta(hours=part * (i + EDGE + (1 - 2 * EDGE) * spread(day, f"{salt}{i}")))
        for i in range(count)
    ]


def planned_after(moment: float, count: int, salt: str, hours: tuple[float, float]) -> float:
    """The first planned moment after [moment]. A moment within a second of it is the one
    that just ran, give or take rounding, so it is skipped."""
    after = datetime.fromtimestamp(moment + 1)
    day = after.date() - timedelta(days=1)  # Hours past midnight belong to the day before.
    while True:
        for at in day_plan(day, count, salt, hours):
            if at > after:
                return at.timestamp()
        day += timedelta(days=1)


def settle(due: float, now: float, count: int, salt: str, hours: tuple[float, float]) -> float:
    """[due] made possible: a moment already gone becomes a short while from now, and a
    moment outside the active hours becomes the next planned one."""
    if due < now:
        due = now + random.uniform(*LATE_S)
    if not within(hours, datetime.fromtimestamp(due)):
        due = planned_after(due, count, salt, hours)
    return due


def next_inbox_check(last_check: float, watch_until: float, failed: bool, now: float, *,
                     poll_s: tuple[float, float], per_day: int,
                     hours: tuple[float, float]) -> float:
    """When to look at the inbox next.

    After a failed check: again in RETRY_S. While a chat goes on (the last check was before
    [watch_until], which is some time after its latest message): every [poll_s], so the
    check after the end of the chat is the last one. Otherwise: the next of the day's
    [per_day] planned moments."""
    if failed:
        due = last_check + random.uniform(*RETRY_S)
    elif last_check < watch_until:
        due = last_check + random.uniform(*poll_s)
    else:
        # Never look further back than two days: a bot that was off longer checks once.
        due = planned_after(max(last_check, now - 2 * 86400), per_day, "inbox", hours)
    return settle(due, now, per_day, "inbox", hours)


def next_invite_round(last_round: float, now: float, *, per_day: int,
                      hours: tuple[float, float]) -> float:
    """When to handle invitations next: the next of the day's [per_day] planned moments."""
    due = planned_after(max(last_round, now - 2 * 86400), per_day, "invites", hours)
    return settle(due, now, per_day, "invites", hours)
