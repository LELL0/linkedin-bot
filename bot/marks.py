"""Enc Chat marks: how the bot lays out a message, and how text from outside is kept from
adding a layout of its own.

Enc Chat 1.7.14 draws these (flutter_client/lib/chat/text_format.dart in the Enc Chat repo):
inline *bold*, _italic_, ~struck~ and `code`; lines starting "# ", "## " or "### " (headings),
"- ", "* " or "• " (bullets, two spaces a level deeper), "1. " or "1) " (numbered), "- [ ] "
and "- [x] " (tasks), "> " (a quote), a line of --- (a divider), ``` lines around a code
block, and [words](https://address) (a link in its own words). Apps 1.7.13 and older show
the marks as typed, so every layout here still reads as plain text.

A table is a code box laid out by table() (each cell through cell()), at most 32 characters a
line so its columns stay in line on a phone.

Text from outside (names, messages, error strings) goes through inline(), quote(), fence() or
cell() before it joins a message. The app has no escape character, so a mark that could
take effect is swapped for a look-alike instead (see inert()).
"""

import re
import unicodedata

# A mark that could open or close is drawn with one of these instead.
LOOKALIKES = {"*": "\u2217", "_": "\uff3f", "~": "\u223c"}  # ∗ ＿ ∼
# Nothing inside a web address or an @mention is a mark (the app's own rule, kUrlRegExp in
# link_patterns.dart); the marks or punctuation at an address's end are not part of it.
ADDRESS = re.compile(
    r"(?:https?|enc)://[^\s<]+|www\.[^\s<]+"
    r"|[a-zA-Z0-9][a-zA-Z0-9-]*\.(?:com|net|org|io|dev|app|co|me|gg|xyz|info|edu|gov)(?:/[^\s<]*)?",
    re.IGNORECASE,
)
TRAILING_MARKS = re.compile(r"[*_~`.,;:!?)\]]+$")
MENTION = re.compile(r"@[A-Za-z0-9_]{1,30}")
# A line that starts like this is laid out as a block (heading, list item, quote, divider,
# code fence) rather than drawn as text.
BLOCK_START = re.compile(r" *(?:#{1,3} |[-*•] |\d{1,3}[.)] |> |```|(?:-{3,}|\*{3,}|_{3,}) *$)")
LINE_GUARD = "\u200b"  # zero width space: the line no longer starts with a mark


def _word(char: str) -> bool:
    """A letter or a digit as the app sees it. The app reads UTF-16 units, so a character
    beyond the basic plane is never a word character there."""
    return ord(char) <= 0xFFFF and unicodedata.category(char)[0] in "LN"


def _protected(text: str) -> set[int]:
    spots: set[int] = set()
    for match in ADDRESS.finditer(text):
        tail = TRAILING_MARKS.search(match.group())
        spots.update(range(match.start(), match.end() - (len(tail.group()) if tail else 0)))
    for match in MENTION.finditer(text):
        spots.update(range(match.start(), match.end()))
    return spots


def inert(text: str) -> str:
    """[text] with no mark that can take effect, its lines kept.

    A * _ or ~ between two letters or digits (snake_case, "A*B") can neither open nor close
    and stays; any other becomes a look-alike, outside web addresses and @mentions. A
    backtick becomes ' (a code span or block could swallow what follows), and "](" gets a
    space so no [words](address) link forms."""
    protected = _protected(text)
    out = []
    for i, char in enumerate(text):
        if char == "`":
            char = "'"
        elif char in LOOKALIKES and i not in protected:
            between_words = 0 < i < len(text) - 1 and _word(text[i - 1]) and _word(text[i + 1])
            if not between_words:
                char = LOOKALIKES[char]
        elif char == "(" and i and text[i - 1] == "]" and i not in protected:
            char = " ("
        out.append(char)
    return "".join(out)


def inline(value: object, limit: int = 300) -> str:
    """Outside text for the middle of a line: one line, at most [limit] characters, inert."""
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return inert(text)


def start_safe(line: str) -> str:
    """[line] such that it cannot start a heading, list item, quote, divider or code fence."""
    return LINE_GUARD + line if BLOCK_START.match(line) else line


def quote(text: object) -> str:
    """Outside text as a quote: each line behind "> ", marks inert, empty lines left out."""
    lines = [" ".join(line.split()) for line in str(text).splitlines()]
    return "\n".join("> " + inert(line) for line in lines if line) or "> …"


def fence(text: object) -> str:
    """Text as a code block, drawn exactly as written, so marks inside it (a message's own
    asterisks) stay as typed. Three backticks in a row would end the block early."""
    return "```\n" + str(text).replace("```", "'''").strip("\n") + "\n```"


# A table is a code box: the app draws it in a monospace font at 0.9 of the text size and
# wraps it rather than scrolling, so a line longer than this breaks the columns on a phone.
TABLE_WIDTH = 32
TABLE_ROWS = 12  # rows shown; the rest become a "+N more" line under the box
TABLE_GAP = "  "


def _narrow(char: str) -> bool:
    """A character that takes one cell of a monospace line: not an emoji, a symbol picture,
    a wide (CJK) character, a joiner or a variation selector, which would push the columns
    out of line."""
    if unicodedata.east_asian_width(char) in "WF":
        return False
    if unicodedata.category(char) in ("So", "Cs", "Cf", "Mn", "Me", "Cc"):
        return False
    return not 0xFE00 <= ord(char) <= 0xFE0F


def cell(value: object, limit: int = TABLE_WIDTH) -> str:
    """One table cell: one line, no backtick (it could close the box), no character that is
    wider than a column, at most [limit] characters (cut with …)."""
    text = "".join(char for char in " ".join(str(value).split()) if _narrow(char))
    text = " ".join(text.replace("`", "'").split())
    if len(text) > limit:
        text = text[: max(limit - 1, 0)].rstrip() + "…"
    return text


def table(head: list[str], rows: list[list[object]], right: tuple[int, ...] = ()) -> str:
    """[rows] under a header in capitals, as a code box that lines up in monospace.

    Columns are two spaces apart; the columns in [right] (numbers) line up on the right.
    No line is wider than TABLE_WIDTH: the first column, the text one, is cut with … to make
    room. At most TABLE_ROWS rows; a "+N more" line under the box counts the rest."""
    shown = [[cell(value) for value in row] for row in rows[:TABLE_ROWS]]
    head = [cell(name).upper() for name in head]
    widths = [max(len(row[i]) for row in [head, *shown]) for i in range(len(head))]
    over = sum(widths) + len(TABLE_GAP) * (len(widths) - 1) - TABLE_WIDTH
    if over > 0:
        widths[0] = max(widths[0] - over, 1)
        head[0] = cell(head[0], widths[0])
        shown = [[cell(row[0], widths[0]), *row[1:]] for row in shown]

    def line(row: list[str]) -> str:
        return TABLE_GAP.join(value.rjust(width) if i in right else value.ljust(width)
                              for i, (value, width) in enumerate(zip(row, widths))).rstrip()

    text = fence("\n".join(line(row) for row in [head, *shown]))
    left = len(rows) - len(shown)
    return text + (f"\n+{left} more" if left else "")
