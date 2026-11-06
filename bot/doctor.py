"""Read-only endpoint check. It sends nothing, and prints only counts, status codes and field names."""

import asyncio

from .linkedin import INBOX_CATEGORIES, LinkedIn
from .session import Session


async def doctor() -> None:
    session = Session()
    await session.start()
    api = LinkedIn(session)
    await asyncio.sleep(6)
    print("signed_in:", session.signed_in, "auth_cookie:", await session.has_auth_cookie())
    await api.load_self()
    print("account:", api.public_id, "headline found:", bool(api.headline))

    for category in INBOX_CATEGORIES:
        try:
            threads = await api.list_threads(category)
            with_messages = sum(1 for t in threads if t.messages)
            print(f"{category}: {len(threads)} threads, {with_messages} include messages")
        except Exception as error:
            print(f"{category}: FAILED {error}")

    try:
        threads = await api.list_threads(INBOX_CATEGORIES[0])
        if threads:
            history = await api.list_messages(threads[0].urn, count=5)
            print("messages: fetched", len(history), "newest has sender:", bool(history and history[-1].sender_id))
    except Exception as error:
        print("messages: FAILED", error)

    invitations = await api.list_invitations()
    print("invitations:", len(invitations), "kinds:", sorted({i.kind for i in invitations}))
    print("all parsed:", all(i.invitation_id and i.shared_secret and i.inviter_id and i.sent_at for i in invitations))

    await session.page.goto("https://www.linkedin.com/mynetwork/invitation-manager/received/")
    await asyncio.sleep(5)
    buttons = await session.page.locator('button[aria-label^="Accept"]').count()
    print("accept buttons on page:", buttons)

    await session.page.goto("https://www.linkedin.com/messaging/")
    await asyncio.sleep(10)
    print("realtime events so far:", session.realtime_events)
    await session.stop()
