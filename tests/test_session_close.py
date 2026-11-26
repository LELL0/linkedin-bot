import asyncio

from bot.session import Session


def test_unexpected_close_reports_it():
    session = Session()
    seen = []
    session.on_closed = seen.append
    session._closed("Chrome closed")
    assert seen == ["Chrome closed"]


def test_close_during_stop_is_ignored():
    session = Session()
    seen = []
    session.on_closed = seen.append
    asyncio.run(session.stop())
    session._closed("Chrome closed")
    assert seen == []


def test_no_handler_is_fine():
    Session()._closed("Chrome closed")
