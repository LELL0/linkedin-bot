"""Owns the logged-in Chrome window and runs every LinkedIn request from inside the page.

Requests go through the page's own fetch, so they carry the browser's cookies and
fingerprint. The CSRF token is read inside the page and never reaches Python or the logs.
"""

import asyncio
import base64
import json
import logging
import re
import socket
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, Request, async_playwright
from playwright.async_api import Error as PlaywrightError

from .config import CONFIG

log = logging.getLogger(__name__)

HOME_URL = "https://www.linkedin.com/messaging/"
LOGIN_URL = "https://www.linkedin.com/login"
INVITATIONS_URL = "https://www.linkedin.com/mynetwork/invitation-manager/received/"
SIGNED_OUT_MARKERS = ("/login", "/uas/", "/checkpoint/", "/authwall", "/signup")
_TOPIC = re.compile(r"urn:li-realtime:(\w+)")
WAKE_TOPICS = ("messagesTopic", "conversationsTopic", "invitationsTopic", "tabBadgeUpdateTopic")

_FETCH_JS = """
async ({method, url, headers, body}) => {
  const match = document.cookie.match(/JSESSIONID="?([^";]+)"?/);
  if (!match) return {status: 0, text: 'no session cookie'};
  const response = await fetch(url, {
    method,
    body: body ?? undefined,
    credentials: 'include',
    redirect: 'manual',
    headers: {...headers, 'csrf-token': match[1], 'x-restli-protocol-version': '2.0.0'},
  });
  return {status: response.status, text: await response.text()};
}
"""


def proxy_problem(proxy: str, timeout: float = 5.0) -> str:
    """Why the proxy cannot be used now ("" when it answers). Only a TCP connect: the
    tunnel to the owner's home is either up or not."""
    parts = urlsplit(proxy)
    if parts.scheme not in ("socks5", "http", "https") or not parts.hostname or not parts.port:
        return f"BOT_PROXY must look like socks5://host:port, not {proxy!r}"
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=timeout):
            return ""
    except OSError as error:
        return f"proxy {parts.hostname}:{parts.port} does not answer ({type(error).__name__})"


def browser_args(proxy: str, chromium: bool) -> list[str]:
    args = ["--disable-blink-features=AutomationControlled"]
    if chromium:
        args.append("--disable-dev-shm-usage")
    if proxy:
        # WebRTC sends UDP around a proxy, which would show the server's own address.
        args.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    return args


@dataclass
class ApiResponse:
    status: int
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.text)


class Session:
    def __init__(self) -> None:
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.wake = asyncio.Event()
        self.invite_wake = asyncio.Event()
        self.realtime_events = 0
        self.realtime_topics: Counter[str] = Counter()
        # Header values copied from the site's own traffic, so they follow deploys.
        self.learned_headers: dict[str, str] = {}
        self._playwright = None
        # Called when Chrome or the main tab closes without stop(), e.g. closed by hand over VNC.
        self.on_closed: Callable[[str], None] | None = None
        self._stopping = False

    async def start(self, url: str = HOME_URL) -> None:
        CONFIG.profile_dir.mkdir(parents=True, exist_ok=True)
        self._stopping = False
        self.realtime_events = 0
        self.realtime_topics.clear()
        self._playwright = await async_playwright().start()
        channel = None if CONFIG.browser_channel == "chromium" else CONFIG.browser_channel
        self.context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(CONFIG.profile_dir),
            channel=channel,
            headless=False,
            no_viewport=True,
            chromium_sandbox=CONFIG.chromium_sandbox,
            # Playwright adds --enable-automation, which sets navigator.webdriver and shows
            # the "controlled by automated software" bar. A person's Chrome has neither.
            ignore_default_args=["--enable-automation"],
            args=browser_args(CONFIG.proxy, chromium=channel is None),
            # Chrome never falls back to a direct connection when a fixed proxy fails.
            proxy={"server": CONFIG.proxy} if CONFIG.proxy else None,
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.on("request", self._learn_from_request)
        self.context.on("close", lambda _: self._closed("Chrome closed"))
        self.page.on("close", lambda _: self._closed("Bot tab closed"))
        await self._tap_realtime()
        await self.page.goto(url, wait_until="domcontentloaded")

    def _closed(self, what: str) -> None:
        if self._stopping or self.on_closed is None:
            return
        self.on_closed(what)

    async def stop(self) -> None:
        self._stopping = True
        if self.context:
            await self.context.close()
        if self._playwright:
            await self._playwright.stop()
        self.context = self.page = self._playwright = None

    @property
    def open(self) -> bool:
        return self.page is not None

    async def _tap_realtime(self) -> None:
        """Wake the main loop on realtime events the page receives. Only topic names are kept."""
        cdp = await self.context.new_cdp_session(self.page)
        await cdp.send("Network.enable")

        def on_payload(data: str) -> None:
            self.realtime_events += 1
            self.realtime_topics.update(_TOPIC.findall(data) or ["no_topic"])
            if any(topic in data for topic in WAKE_TOPICS):
                self.wake.set()
                self.invite_wake.set()

        async def stream(request_id: str) -> None:
            try:
                await cdp.send("Network.streamResourceContent", {"requestId": request_id})
                log.info("Realtime stream tapped")
            except PlaywrightError as error:
                log.warning("Could not tap the realtime stream: %s", type(error).__name__)

        def on_response(params: dict) -> None:
            if "/realtime/connect" in params["response"]["url"]:
                asyncio.create_task(stream(params["requestId"]))

        def on_data(params: dict) -> None:
            if params.get("data"):
                on_payload(base64.b64decode(params["data"]).decode("utf-8", "ignore"))

        cdp.on("Network.eventSourceMessageReceived", lambda p: on_payload(p.get("data", "")))
        cdp.on("Network.responseReceived", on_response)
        cdp.on("Network.dataReceived", on_data)

    def _learn_from_request(self, request: Request) -> None:
        if "/voyager/api/" not in request.url:
            return
        for name in ("x-li-track", "x-li-page-instance"):
            value = request.headers.get(name)
            if value:
                self.learned_headers[name] = value

    @property
    def signed_in(self) -> bool:
        return not any(marker in self.page.url for marker in SIGNED_OUT_MARKERS)

    async def has_auth_cookie(self) -> bool:
        cookies = await self.context.cookies("https://www.linkedin.com")
        return any(cookie["name"] == "li_at" for cookie in cookies)

    async def call(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/json",
        content_type: str | None = None,
        body: str | None = None,
    ) -> ApiResponse:
        headers = {"accept": accept, **self.learned_headers}
        if content_type:
            headers["content-type"] = content_type
        request = {"method": method, "url": path, "headers": headers, "body": body}
        try:
            result = await self.page.evaluate(_FETCH_JS, request)
        except PlaywrightError as error:
            # A POST may already have reached LinkedIn, so repeating it could send a reply twice.
            # The caller re-reads the thread instead, which shows whether it went through.
            if method != "GET" or "context was destroyed" not in str(error):
                raise
            # The page navigated mid-call, so wait for it to settle and try once more.
            await self.page.wait_for_load_state("domcontentloaded")
            result = await self.page.evaluate(_FETCH_JS, request)
        return ApiResponse(status=result["status"], text=result["text"])

    async def accept_via_page(self, full_name: str) -> bool:
        """Last resort: click Accept on the invitations page, in a separate tab.

        Clicks only if the full name matches exactly one button, so an older pending
        invite from someone with a similar name is never accepted by mistake.
        """
        if not full_name:
            return False
        tab = await self.context.new_page()
        try:
            await tab.goto(INVITATIONS_URL, wait_until="domcontentloaded")
            buttons = tab.get_by_role("button", name=accept_label(full_name))
            await buttons.first.wait_for(timeout=8000)
            if await buttons.count() != 1:
                log.warning("Accept button not unique, leaving the invite")
                return False
            await buttons.click(timeout=8000)
            await asyncio.sleep(1.5)
            return True
        except PlaywrightError as error:
            log.warning("Accept button failed: %s", type(error).__name__)
            return False
        finally:
            await tab.close()


def accept_label(full_name: str) -> re.Pattern:
    return re.compile(rf"^Accept {re.escape(full_name)}(?!\w)")


async def login() -> None:
    """Open the dedicated profile and wait for the user to sign in by hand."""
    if CONFIG.proxy and (problem := proxy_problem(CONFIG.proxy)):
        raise SystemExit(f"Not opening LinkedIn: {problem}. Start the home tunnel first.")
    session = Session()
    await session.start(LOGIN_URL)
    print("Sign in to LinkedIn in the Chrome window. This waits up to 15 minutes.", flush=True)
    for _ in range(900):
        if await session.has_auth_cookie() and session.signed_in:
            print("Signed in. Session saved to the bot profile.", flush=True)
            await asyncio.sleep(3)
            await session.stop()
            return
        await asyncio.sleep(1)
    await session.stop()
    raise SystemExit("Timed out waiting for sign-in.")
