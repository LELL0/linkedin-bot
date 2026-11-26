"""Liveness for servers: a status file the bot rewrites, a check for Docker, and Enc Chat alerts."""

import json
import logging
import os
import shutil
import subprocess
import threading
import time

from . import enc
from .config import CONFIG
from .schedule import RETRY_S

log = logging.getLogger(__name__)

STALE_STATUS_S = 180  # The bot rewrites the file every minute.


def stale_poll_s() -> float:
    """No successful LinkedIn check for this long means it is stuck. Hours between planned
    checks do not count: the bot keeps last_ok fresh while it waits on purpose after a good
    check. What counts is the wait after a failed check (RETRY_S) or during a chat
    (CONFIG.poll_s, changeable from Enc Chat), so allow the longest plus a check's time."""
    return max(CONFIG.poll_s[1], RETRY_S[1]) + CONFIG.min_check_gap_s + 300


ALERT_REPEAT_S = 3600


def write_status(state: str, last_ok: float, **counts) -> None:
    status = {"at": time.time(), "state": state, "last_ok": last_ok,
              "stale_poll_s": stale_poll_s(), **counts}
    temporary = CONFIG.status_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status))
    temporary.replace(CONFIG.status_path)


def problem(status: dict | None, now: float) -> str | None:
    """Why the bot is unhealthy, or None when it is fine."""
    if status is None:
        return "no status file"
    if now - status.get("at", 0) > STALE_STATUS_S:
        return f"status not updated for {int(now - status.get('at', 0))}s"
    if status.get("state") == "needs_login":
        return "signed out of LinkedIn: log in over VNC"
    # The healthcheck runs in its own process, so it takes the bot's live limit from the file.
    limit = status.get("stale_poll_s") or stale_poll_s()
    if status.get("state") == "running" and now - status.get("last_ok", 0) > limit:
        return f"no successful LinkedIn check for {int(now - status['last_ok'])}s"
    return None


def check() -> int:
    """Exit code for a Docker HEALTHCHECK: 0 healthy, 1 unhealthy."""
    try:
        status = json.loads(CONFIG.status_path.read_text())
    except (OSError, ValueError):
        status = None
    reason = problem(status, time.time())
    print(reason or f"ok: {status.get('state')}, accepted {status.get('accepted', 0)}, "
          f"replies {status.get('replies', 0)}")
    return 1 if reason else 0


_last_sent: dict[str, float] = {}


def alert(state: str, detail: str = "", icon: str = "🟡") -> None:
    """Log, then tell the owner on Enc Chat (servers) or by a desktop notification (laptop).

    [state] is the problem in one to four lowercase words ("sign-in needed"), [detail] what
    it means and what to do, and [icon] a title emoji: 🔴 down until the owner acts, 🟡 the
    bot waits or hit a limit, ❌ one thing failed, ⏳ restarting. The same words within the
    hour are sent once."""
    text = f"{state}: {detail}" if detail else state
    log.error("ALERT: %s", text)
    now = time.time()
    if now - _last_sent.get(text, 0) < ALERT_REPEAT_S:
        return
    _last_sent[text] = now
    whole = f"{enc.title(icon if icon in enc.TITLE_EMOJI else '🟡', enc.inline(state, 60) or 'alert')}"
    if detail.strip():
        whole += "\n" + enc.inline(detail, 3000)
    short = enc.alert_text(state, detail, icon)
    enc.notify(short, whole if whole.replace("\n", enc.DOT) != short else None)
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "-u", "critical", "LinkedIn bot", text],
            check=False,
            capture_output=True,
        )


SLEPT_S = 60.0


def woke_at(wall: float, clock: float, wall_now: float, clock_now: float, woke: float) -> float:
    """When the laptop last woke from sleep: now, if the wall clock moved more than SLEPT_S
    past the monotonic clock (which stands still in sleep) since [wall]/[clock], else [woke].
    A laptop that slept 15 minutes is not a stuck event loop (2026-11-26 20:22)."""
    if (wall_now - wall) - (clock_now - clock) > SLEPT_S:
        log.info("The machine slept for %ds", (wall_now - wall) - (clock_now - clock))
        return wall_now
    return woke


def start_watchdog(get_status_time, limit_s: float = STALE_STATUS_S * 2) -> None:
    """Kill the process if the event loop stops updating the status, so the supervisor restarts it.

    Runs in its own thread, so it still works when the event loop itself is stuck.
    """

    def watch() -> None:
        woke = 0.0
        while True:
            wall, clock = time.time(), time.monotonic()
            time.sleep(30)
            woke = woke_at(wall, clock, time.time(), time.monotonic(), woke)
            stuck = time.time() - max(get_status_time(), woke)
            if stuck > limit_s:
                log.error("Event loop stuck for %ds, exiting for a restart", stuck)
                logging.shutdown()
                os._exit(1)

    threading.Thread(target=watch, name="watchdog", daemon=True).start()
