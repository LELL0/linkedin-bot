"""Liveness for servers: a status file the bot rewrites, a check for Docker, and Telegram alerts."""

import json
import logging
import os
import shutil
import subprocess
import threading
import time

import httpx

from .config import CONFIG

log = logging.getLogger(__name__)

STALE_STATUS_S = 180  # The bot rewrites the file every minute.


def stale_poll_s() -> float:
    """No successful LinkedIn check for this long means it is stuck. The bot waits up to
    CONFIG.poll_s between checks on purpose (changeable from Telegram), so allow the longest
    wait plus a check's time."""
    return CONFIG.poll_s[1] + CONFIG.min_check_gap_s + 300


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


def alert(text: str) -> None:
    """Log, then tell a person by Telegram (servers) or a desktop notification (laptop)."""
    log.error("ALERT: %s", text)
    now = time.time()
    if now - _last_sent.get(text, 0) < ALERT_REPEAT_S:
        return
    _last_sent[text] = now
    token, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if token and chat:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": f"LinkedIn bot: {text}"},
                timeout=10,
            )
        except httpx.HTTPError as error:
            log.warning("Telegram alert failed: %s", type(error).__name__)
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "-u", "critical", "LinkedIn bot", text],
            check=False,
            capture_output=True,
        )


def start_watchdog(get_status_time, limit_s: float = STALE_STATUS_S * 2) -> None:
    """Kill the process if the event loop stops updating the status, so the supervisor restarts it.

    Runs in its own thread, so it still works when the event loop itself is stuck.
    """

    def watch() -> None:
        while True:
            time.sleep(30)
            stuck = time.time() - get_status_time()
            if stuck > limit_s:
                log.error("Event loop stuck for %ds, exiting for a restart", stuck)
                logging.shutdown()
                os._exit(1)

    threading.Thread(target=watch, name="watchdog", daemon=True).start()
