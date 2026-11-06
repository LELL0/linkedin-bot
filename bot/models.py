"""Plain data types, and parsers that turn LinkedIn payloads into them."""

import re
from dataclasses import dataclass, field
from typing import Any

_PROFILE_ID = re.compile(r"(ACoA[A-Za-z0-9_-]+)")


def profile_id(urn: str | None) -> str:
    """Reduce any URN form (fsd_profile, fs_miniProfile, msg_messagingParticipant) to the bare id."""
    match = _PROFILE_ID.search(urn or "")
    return match.group(1) if match else ""


@dataclass
class Attachment:
    kind: str  # image, file, audio, video, gif, forwarded, reply
    name: str = ""
    text: str = ""


@dataclass
class Message:
    urn: str
    sender_id: str
    delivered_at: int
    text: str
    render_format: str = "DEFAULT"
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class Thread:
    urn: str
    last_activity_at: int
    group_chat: bool
    categories: list[str]
    participants: list[str]
    has_organization: bool
    names: dict[str, str] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)


@dataclass
class Invitation:
    urn: str
    invitation_id: str
    shared_secret: str
    inviter_id: str
    sent_at: int
    kind: str
    inviter_name: str = ""


def _text(value: Any) -> str:
    return (value or {}).get("text", "") if isinstance(value, dict) else (value or "")


def parse_attachments(render_content: list[dict] | None) -> list[Attachment]:
    found = []
    for item in render_content or []:
        if item.get("vectorImage"):
            found.append(Attachment("image"))
        elif item.get("file"):
            file = item["file"]
            found.append(Attachment("file", name=file.get("name", "")))
        elif item.get("audio"):
            found.append(Attachment("audio"))
        elif item.get("video"):
            found.append(Attachment("video"))
        elif item.get("externalMedia"):
            found.append(Attachment("gif", name=item["externalMedia"].get("title", "")))
        elif item.get("forwardedMessageContent"):
            body = _text(item["forwardedMessageContent"].get("forwardedBody"))
            found.append(Attachment("forwarded", text=body))
        elif item.get("repliedMessageContent"):
            original = item["repliedMessageContent"].get("originalMessage") or {}
            found.append(Attachment("reply", text=_text(original.get("body"))))
    return found


def parse_message(raw: dict) -> Message:
    return Message(
        urn=raw.get("entityUrn", ""),
        sender_id=profile_id((raw.get("sender") or {}).get("entityUrn")),
        delivered_at=int(raw.get("deliveredAt") or 0),
        text=_text(raw.get("body")),
        render_format=raw.get("messageBodyRenderFormat") or "DEFAULT",
        attachments=parse_attachments(raw.get("renderContent")),
    )


def parse_thread(raw: dict) -> Thread:
    participants, names, has_organization = [], {}, False
    for person in raw.get("conversationParticipants") or []:
        kind = person.get("participantType") or {}
        if kind.get("organization") is not None:
            has_organization = True
        pid = profile_id(person.get("entityUrn"))
        if pid:
            participants.append(pid)
            names[pid] = _text((kind.get("member") or {}).get("firstName"))
    messages = [parse_message(m) for m in (raw.get("messages") or {}).get("elements") or []]
    return Thread(
        urn=raw.get("entityUrn", ""),
        last_activity_at=int(raw.get("lastActivityAt") or 0),
        group_chat=bool(raw.get("groupChat")),
        categories=raw.get("categories") or [],
        participants=participants,
        has_organization=has_organization,
        names=names,
        messages=sorted(messages, key=lambda m: m.delivered_at),
    )
