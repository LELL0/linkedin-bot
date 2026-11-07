"""LinkedIn operations.

There is deliberately no way here to start a conversation or send a connection request:
send_reply needs an existing conversation URN, and invitations can only be accepted.
Endpoint facts come from studying public projects such as mautrix/linkedin.
"""

import json
import logging
import secrets
import time
import uuid
from urllib.parse import quote

from .models import Invitation, Message, Thread, parse_message, parse_thread, profile_id
from .session import ApiResponse, Session

log = logging.getLogger(__name__)

GRAPHQL = "application/graphql"
PLAIN = "text/plain;charset=UTF-8"
MESSAGING_GRAPHQL = "/voyager/api/voyagerMessagingGraphQL/graphql"
MESSAGES = "/voyager/api/voyagerMessagingDashMessengerMessages"
CONVERSATIONS = "/voyager/api/voyagerMessagingDashMessengerConversations"

QUERY_CONVERSATIONS = "messengerConversations.8656fb361a8ad0c178e8d3ff1a84ce26"
QUERY_MESSAGES = "messengerMessages.4088d03bc70c91c3fa68965cb42336de"

INBOX_CATEGORIES = ("PRIMARY_INBOX", "SECONDARY_INBOX")
# invitationType only says PENDING, so the invitee type is what marks a person-to-person invite.
PERSON_INVITEE = "com.linkedin.voyager.relationships.invitation.ProfileInvitee"


class AuthError(RuntimeError):
    """The session was rejected, so a person has to sign in again."""


def _check(response: ApiResponse, what: str) -> ApiResponse:
    if response.status in (401, 403) or 300 <= response.status < 400 or response.status == 0:
        raise AuthError(f"{what}: HTTP {response.status}")
    if not response.ok:
        raise RuntimeError(f"{what}: HTTP {response.status}")
    return response

MESSAGE_PAGE = 30


class LinkedIn:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.self_urn = ""
        self.self_id = ""
        self.first_name = ""
        self.headline = ""
        self.public_id = ""

    async def load_self(self) -> None:
        response = _check(
            await self.session.call("GET", "/voyager/api/me"), "load own profile"
        )
        payload = response.json()
        profile = payload.get("miniProfile") or next(
            (i for i in payload.get("included", []) if i.get("dashEntityUrn")), {}
        )
        self.self_urn = profile.get("dashEntityUrn", "")
        self.self_id = profile_id(self.self_urn)
        self.first_name = profile.get("firstName", "")
        self.headline = profile.get("occupation", "")
        self.public_id = profile.get("publicIdentifier", "")
        if not self.self_id:
            raise RuntimeError("Could not read own profile id")

    async def list_threads(self, category: str, before: int | None = None) -> list[Thread]:
        """The 20 latest threads, or the 20 before `before` (ms), to page further back."""
        mailbox = quote(self.self_urn, safe="")
        now = before or int(time.time() * 1000)
        query = f"(predicateUnions:List((conversationCategoryPredicate:(category:{category}))))"
        variables = f"(mailboxUrn:{mailbox},lastUpdatedBefore:{now},count:20,query:{query})"
        url = f"{MESSAGING_GRAPHQL}?queryId={QUERY_CONVERSATIONS}&variables={variables}"
        response = _check(await self.session.call("GET", url, accept=GRAPHQL), "list threads")
        data = response.json().get("data") or {}
        collection = data.get("messengerConversationsByCategoryQuery") or {}
        return [parse_thread(raw) for raw in collection.get("elements") or []]

    async def list_messages(self, thread_urn: str, count: int = MESSAGE_PAGE) -> list[Message]:
        urn = quote(thread_urn, safe="")
        now = int(time.time() * 1000) + 60_000
        variables = f"(deliveredAt:{now},conversationUrn:{urn},countBefore:{count},countAfter:0)"
        url = f"{MESSAGING_GRAPHQL}?queryId={QUERY_MESSAGES}&variables={variables}"
        response = _check(await self.session.call("GET", url, accept=GRAPHQL), "list messages")
        data = response.json().get("data") or {}
        collection = data.get("messengerMessagesByAnchorTimestamp") or {}
        messages = [parse_message(raw) for raw in collection.get("elements") or []]
        return sorted(messages, key=lambda m: m.delivered_at)

    async def send_reply(self, thread_urn: str, text: str) -> None:
        """Post into an existing conversation. This is the only send path in the project."""
        if not thread_urn:
            raise ValueError("A reply needs an existing conversation")
        body = {
            "message": {
                "body": {"text": text},
                "renderContentUnions": [],
                "conversationUrn": thread_urn,
                "originToken": str(uuid.uuid4()),
            },
            "mailboxUrn": self.self_urn,
            "trackingId": secrets.token_hex(8),
            "dedupeByClientGeneratedToken": False,
        }
        _check(
            await self.session.call(
                "POST",
                f"{MESSAGES}?action=createMessage",
                content_type=PLAIN,
                body=json.dumps(body),
            ),
            "send reply",
        )

    async def show_typing(self, thread_urn: str) -> None:
        await self.session.call(
            "POST",
            f"{CONVERSATIONS}?action=typing",
            content_type=PLAIN,
            body=json.dumps({"conversationUrn": thread_urn}),
        )

    async def mark_read(self, thread_urn: str) -> None:
        body = {"entities": {thread_urn: {"patch": {"$set": {"read": True}}}}}
        await self.session.call(
            "POST",
            f"{CONVERSATIONS}?ids=List({quote(thread_urn, safe='')})",
            content_type=PLAIN,
            body=json.dumps(body),
        )

    async def list_invitations(self) -> list[Invitation]:
        url = "/voyager/api/relationships/invitationViews?start=0&count=20&q=receivedInvitation"
        response = _check(await self.session.call("GET", url), "list invitations")
        return [
            parse_invitation(view.get("invitation") or {})
            for view in response.json().get("elements") or []
        ]

    async def accept(self, invitation: Invitation) -> str:
        """Try the REST action, then Dash, then the Accept button. Returns the path that worked."""
        legacy = await self.session.call(
            "POST",
            f"/voyager/api/relationships/invitations/{invitation.invitation_id}?action=accept",
            content_type="application/json",
            body=json.dumps(
                {
                    "invitationId": invitation.invitation_id,
                    "invitationSharedSecret": invitation.shared_secret,
                    "isGenericInvitation": False,
                }
            ),
        )
        if legacy.ok:
            return "rest"
        log.warning("REST accept failed (HTTP %s), trying Dash", legacy.status)
        urn = f"urn:li:fsd_invitation:{invitation.invitation_id}"
        dash = await self.session.call(
            "POST",
            f"/voyager/api/voyagerRelationshipsDashInvitations/{urn}?action=accept",
            content_type="application/json",
            body=json.dumps({"invitationUrn": urn, "sharedSecret": invitation.shared_secret}),
        )
        if dash.ok:
            return "dash"
        log.warning("Dash accept failed (HTTP %s), trying the page", dash.status)
        return "page" if await self.session.accept_via_page(invitation.inviter_name) else ""


def parse_invitation(raw: dict) -> Invitation:
    urn = raw.get("entityUrn", "")
    inviter = raw.get("fromMember") or {}
    return Invitation(
        urn=urn,
        invitation_id=urn.rsplit(":", 1)[-1],
        shared_secret=raw.get("sharedSecret", ""),
        inviter_id=profile_id(inviter.get("dashEntityUrn") or inviter.get("entityUrn")),
        sent_at=int(raw.get("sentTime") or 0),
        kind="CONNECTION" if PERSON_INVITEE in (raw.get("invitee") or {}) else "OTHER",
        inviter_name=" ".join(filter(None, (inviter.get("firstName"), inviter.get("lastName")))),
    )
