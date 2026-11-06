"""Owns the logged-in Chrome window and runs every LinkedIn request from inside the page.

Requests go through the page's own fetch, so they carry the browser's cookies and
fingerprint. The CSRF token is read inside the page and never reaches Python or the logs.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from playwright.async_api import BrowserContext, Page, Request, async_playwright
from playwright.async_api import Error as PlaywrightError

from .config import CONFIG

log = logging.getLogger(__name__)

HOME_URL = "https://www.linkedin.com/messaging/"
LOGIN_URL = "https://www.linkedin.com/login"
INVITATIONS_URL = "https://www.linkedin.com/mynetwork/invitation-manager/received/"
SIGNED_OUT_MARKERS = ("/login", "/uas/", "/checkpoint/", "/authwall", "/signup")
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
        self.realtime_events = 0
        # Header values copied from the site's own traffic, so they follow deploys.
        self.learned_headers: dict[str, str] = {}
        self._playwright = None

    async def start(self, url: str = HOME_URL) -> None:
        CONFIG.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self.context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(CONFIG.profile_dir),
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.on("request", self._learn_from_request)
        await self._tap_realtime()
        await self.page.goto(url, wait_until="domcontentloaded")

    async def stop(self) -> None:
        if self.context:
            await self.context.close()
        if self._playwright:
            await self._playwright.stop()

    async def _tap_realtime(self) -> None:
        """Wake the main loop on any server-sent event the page receives. Payloads are ignored."""
        cdp = await self.context.new_cdp_session(self.page)
        await cdp.send("Network.enable")

        def on_event(params: dict) -> None:
            self.realtime_events += 1
            if any(topic in params.get("data", "") for topic in WAKE_TOPICS):
                self.wake.set()

        cdp.on("Network.eventSourceMessageReceived", on_event)

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

    async def accept_via_page(self, first_name: str) -> bool:
        """Last resort: click Accept on the invitations page, in a separate tab."""
        if not first_name:
            return False
        tab = await self.context.new_page()
        try:
            await tab.goto(INVITATIONS_URL, wait_until="domcontentloaded")
            button = tab.locator(f'button[aria-label^="Accept {first_name}"]').first
            await button.click(timeout=8000)
            await asyncio.sleep(1.5)
            return True
        except PlaywrightError as error:
            log.warning("Accept button failed: %s", type(error).__name__)
            return False
        finally:
            await tab.close()


async def login() -> None:
    """Open the dedicated profile and wait for the user to sign in by hand."""
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
