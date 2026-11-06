"""Reply-only rules, as pure functions so they can be tested without LinkedIn.

The bot may only speak in an existing one-to-one thread, in answer to an unanswered
message from the other person that arrived after the bot first started.
"""

from dataclasses import dataclass

from .models import Message, Thread

SKIPPED_FORMATS = {"SYSTEM", "RECALLED"}


@dataclass(frozen=True)
class Scope:
    mode: str  # new_threads or all
    watermark: int
    self_id: str


def unanswered(thread: Thread, scope: Scope, is_answered) -> list[Message]:
    """Inbound messages after our own last message, oldest first."""
    pending: list[Message] = []
    for message in thread.messages:
        if message.sender_id == scope.self_id:
            pending = []  # Anything before our latest message has been dealt with.
        elif (
            message.delivered_at > scope.watermark
            and message.render_format not in SKIPPED_FORMATS
            and not is_answered(message.urn)
        ):
            pending.append(message)
    return pending


def in_scope(thread: Thread, scope: Scope, thread_was_known: bool, is_accepted) -> bool:
    """Decide whether the bot may speak in this thread at all."""
    if thread.group_chat or thread.has_organization or "SPAM" in thread.categories:
        return False
    if scope.mode == "all":
        return True
    others = [p for p in thread.participants if p != scope.self_id]
    return not thread_was_known or any(is_accepted(person) for person in others)
