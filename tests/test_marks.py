"""Enc Chat marks: outside text never adds a layout of its own. All data is synthetic."""

import pytest

from bot import marks

STAR, LOW, TILDE, GUARD = "\u2217", "\uff3f", "\u223c", "\u200b"


@pytest.mark.parametrize("text, expected", [
    ("Sam Lee", "Sam Lee"),
    ("*boss*", f"{STAR}boss{STAR}"),
    ("_Sam_", f"{LOW}Sam{LOW}"),
    ("~gone~", f"{TILDE}gone{TILDE}"),
    ("a * b _ c ~ d", f"a {STAR} b {LOW} c {TILDE} d"),
    # Between two letters or digits a mark can neither open nor close: left as typed.
    ("snake_case max_replies A*B 2~3", "snake_case max_replies A*B 2~3"),
    ("ERR_PROXY_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED"),
    ("x_ _y", f"x{LOW} {LOW}y"),
    # Backticks become quotes, so no code span or block can form.
    ("`code` and ```block```", "'code' and '''block'''"),
    # No [words](address) link.
    ("[Click](https://evil.example/x)", "[Click] (https://evil.example/x)"),
    # Web addresses and @mentions are the app's own: nothing in them is a mark.
    # (A mark at an address's end is not part of it, so it is made inert like any other.)
    ("see https://site.com/_next/a_b?q=*x* now", f"see https://site.com/_next/a_b?q=*x{STAR} now"),
    ("_https://site.com/x_", f"{LOW}https://site.com/x{LOW}"),
    ("ask @_first_last_", "ask @_first_last_"),
    # A letter beyond the basic plane is not a word character in the app (UTF-16 units).
    ("\U0001d400_b_", f"\U0001d400{LOW}b{LOW}"),
    ("café_x", "café_x"),
])
def test_inert(text, expected):
    assert marks.inert(text) == expected


def test_inline_is_one_line_and_cut():
    assert marks.inline("a\nb\r\n\tc  d\u2028e") == "a b c d e"
    assert marks.inline("  ") == ""
    assert marks.inline("x" * 20, limit=10) == "x" * 9 + "…"
    assert marks.inline(ValueError("'*x*' is not a number")) == f"'{STAR}x{STAR}' is not a number"


@pytest.mark.parametrize("line", [
    "# a", "## a", "### a", "- a", "* a", "• a", "- [ ] a", "- [x] a", "1. a", "12) a",
    "> a", "---", "***", "___", "```", "```py", "  - a", "  3. a",
])
def test_start_safe_guards_every_block_start(line):
    assert marks.start_safe(line) == GUARD + line


@pytest.mark.parametrize("line", [
    "Sam", "#hashtag", "#### four", "-a", "1.5 days", "1234. too long", ">a", "-- two", "",
])
def test_start_safe_leaves_ordinary_lines(line):
    assert marks.start_safe(line) == line


def test_quote_keeps_lines_and_drops_empty_ones():
    quoted = marks.quote("one\n\n  two  words \n*three*")
    assert quoted == f"> one\n> two words\n> {STAR}three{STAR}"
    assert marks.quote("") == "> …"


def test_fence_never_holds_three_backticks():
    for text in ("```", "````", "`````", "a```b``c```", "\n```\nx\n```\n"):
        inner = marks.fence(text)[4:-4]
        assert "```" not in inner
    assert marks.fence("*hi*\n") == "```\n*hi*\n```"
