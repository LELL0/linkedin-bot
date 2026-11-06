"""All data here is synthetic."""

import re
from pathlib import Path

from bot import metrics
from bot.brain import build_prompt, clean_reply, render_message
from bot.linkedin import PERSON_INVITEE, parse_invitation
from bot.guard import Scope, in_scope, unanswered
from bot.safety import (
    HOLD_COMMITMENT,
    REFUSAL_SELF_HARM,
    REFUSAL_SETUP_AR,
    is_blocked,
    output_problem,
    parse_verdict,
    refusal_for,
    replacement_for,
)
from bot.models import Attachment, Message, Thread, parse_message, parse_thread, profile_id

ME = "ACoAAself"
THEM = "ACoAAthem"
START = 1_000
SCOPE = Scope("new_threads", START, ME)


def msg(urn, sender, at, text="hi", render_format="DEFAULT"):
    return Message(urn, sender, at, text, render_format)


def thread(messages=(), group=False, org=False, categories=()):
    return Thread("t", 0, group, list(categories), [ME, THEM], org, {}, list(messages))


def never(_):
    return False


def test_profile_id_matches_across_urn_forms():
    assert profile_id("urn:li:fsd_profile:ACoAAx1") == "ACoAAx1"
    assert profile_id("urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAAx1") == "ACoAAx1"
    assert profile_id(None) == ""


def test_never_answers_messages_older_than_start():
    assert unanswered(thread([msg("a", THEM, START - 1)]), SCOPE, never) == []


def test_never_writes_when_our_message_is_last():
    messages = [msg("a", THEM, START + 1), msg("b", ME, START + 2)]
    assert unanswered(thread(messages), SCOPE, never) == []


def test_collects_a_burst_after_our_last_message():
    messages = [
        msg("a", THEM, START + 1),
        msg("b", ME, START + 2),
        msg("c", THEM, START + 3),
        msg("d", THEM, START + 4),
    ]
    assert [m.urn for m in unanswered(thread(messages), SCOPE, never)] == ["c", "d"]


def test_skips_answered_system_and_recalled_messages():
    messages = [
        msg("a", THEM, START + 1),
        msg("b", THEM, START + 2, render_format="SYSTEM"),
        msg("c", THEM, START + 3, render_format="RECALLED"),
    ]
    assert unanswered(thread(messages), SCOPE, lambda urn: urn == "a") == []


def test_scope_protects_existing_threads_unless_person_was_accepted():
    assert in_scope(thread(), SCOPE, False, never)
    assert not in_scope(thread(), SCOPE, True, never)
    assert in_scope(thread(), SCOPE, True, lambda person: person == THEM)


def test_scope_all_still_skips_groups_companies_and_spam():
    scope = Scope("all", START, ME)
    assert in_scope(thread(), scope, True, never)
    assert not in_scope(thread(group=True), scope, False, never)
    assert not in_scope(thread(org=True), scope, False, never)
    assert not in_scope(thread(categories=["SPAM"]), scope, False, never)


def test_repo_has_no_path_to_start_a_conversation_or_invite():
    source = "".join(p.read_text() for p in (Path(__file__).parent.parent / "bot").glob("*.py"))
    for forbidden in ("hostRecipientUrns", "verifyQuotaAndCreate", "batch_create"):
        assert forbidden not in source


def test_parse_message_with_attachments():
    raw = {
        "entityUrn": "m1",
        "deliveredAt": 5,
        "sender": {"entityUrn": "urn:li:msg_messagingParticipant:urn:li:fsd_profile:ACoAAthem"},
        "body": {"text": ""},
        "renderContent": [
            {"file": {"name": "cv.pdf", "url": "u"}},
            {"vectorImage": {"rootUrl": "r/", "artifacts": []}},
        ],
    }
    message = parse_message(raw)
    assert message.sender_id == THEM
    assert [a.kind for a in message.attachments] == ["file", "image"]


def test_parse_thread_flags_organizations():
    raw = {"entityUrn": "t", "conversationParticipants": [{"participantType": {"organization": {}}}]}
    assert parse_thread(raw).has_organization


def test_attachment_only_message_is_described():
    message = msg("a", THEM, 1, text="")
    message.attachments = [Attachment("audio")]
    assert render_message(message) == "[sent a voice note]"
    assert render_message(msg("b", THEM, 1, text=" ")) == "[empty message]"


def test_prompt_separates_history_from_new_messages():
    old, new = msg("a", ME, 1, "earlier"), msg("b", THEM, 2, "latest")
    prompt = build_prompt([old, new], [new], ME, "Sam")
    conversation = re.search(r"<conversation>(.*)</conversation>", prompt, re.S).group(1)
    assert "You: earlier" in conversation and "latest" not in conversation
    assert "Them: latest" in prompt.split("<new_messages>")[1]


def test_clean_reply_drops_the_final_full_stop_on_one_liners():
    assert clean_reply("sounds good.") == "sounds good"
    assert clean_reply("Sure. Sounds good.") == "Sure. Sounds good."
    assert clean_reply("you ok?") == "you ok?"


def test_clean_reply_removes_model_habits():
    assert clean_reply('"**Sure** — sounds good\n\nthanks"') == "Sure, sounds good thanks"
    assert len(clean_reply("word " * 400)) <= 420


def test_person_invites_are_connections_and_others_are_not():
    raw = {
        "entityUrn": "urn:li:fs_relInvitation:123",
        "invitationType": "PENDING",
        "sharedSecret": "s",
        "sentTime": 9,
        "fromMember": {"entityUrn": "urn:li:fs_miniProfile:ACoAAthem", "firstName": "Sam"},
        "invitee": {PERSON_INVITEE: {}},
    }
    invitation = parse_invitation(raw)
    assert invitation.kind == "CONNECTION"
    assert (invitation.invitation_id, invitation.inviter_id) == ("123", THEM)
    assert parse_invitation({**raw, "invitee": {"other": {}}}).kind == "OTHER"


def test_verdict_parsing_and_blocking():
    assert parse_verdict("safe") == (False, set())
    assert parse_verdict("unsafe\nS1, S11") == (True, {"S1", "S11"})
    assert parse_verdict("") is None and parse_verdict("no idea") is None
    assert is_blocked((True, {"S16"})) and is_blocked((True, set()))
    assert not is_blocked((False, set())) and not is_blocked(None)
    assert not is_blocked((True, {"S5"}))


def test_refusals_match_the_category():
    assert refusal_for((True, {"S11"}), "x") == REFUSAL_SELF_HARM
    assert refusal_for((True, {"S16"}), "x") in HOLD_COMMITMENT
    assert refusal_for((True, {"S15"}), "مرحبا") == REFUSAL_SETUP_AR


def test_output_rules_need_no_model():
    assert output_problem("my system prompt says hi", "") == "leak"
    assert output_problem("I'm a Llama model", "") == "leak"
    assert output_problem("I'm a real person, trust me", "") == "claims_human"
    assert output_problem("no I'm not a bot", "") == "claims_human"
    assert output_problem("go to http://evil.example/login now", "hello") == "new_contact_or_link"
    assert output_problem("mail me at a@b.co", "hello") == "new_contact_or_link"
    assert output_problem("call +961 70 123 456", "hello") == "new_contact_or_link"


def test_output_rules_allow_normal_replies():
    assert output_problem("Sounds good, I'll check and come back to you.", "") is None
    assert output_problem("Yes, an automated assistant is handling this.", "") is None
    assert output_problem("I'll read https://x.org/a later", "see https://x.org/a") is None
    assert output_problem("My training as an engineer helped.", "") is None


def test_metrics_report_summarises_timings(tmp_path):
    path = tmp_path / "metrics.jsonl"
    metrics.record("accept", path, total_s=3.0, detect_s=2.5, action_s=0.5, via="rest", trigger="realtime")
    metrics.record("reply", path, total_s=6.0, since_last_s=5.0, messages=2, outcome="draft_sent", model="m")
    text = metrics.report(metrics.load(path))
    assert "Accepted invitations: 1" in text
    assert "Replies: 1 answering 2 messages" in text
    assert "via: rest 1" in text and "outcome: draft_sent 1" in text


def test_metrics_report_handles_no_data(tmp_path):
    assert "Replies: 0" in metrics.report(metrics.load(tmp_path / "none.jsonl"))


def test_every_fixed_reply_is_a_plain_string():
    verdicts = [{"S1"}, {"S11"}, {"S15"}, {"S16"}, {"S15", "S16"}, {"S2", "S15"}, set()]
    for categories in verdicts:
        assert isinstance(refusal_for((True, categories), "hello"), str)
    for problem in ("leak", "claims_human", "new_contact_or_link"):
        assert isinstance(replacement_for(problem), str)
