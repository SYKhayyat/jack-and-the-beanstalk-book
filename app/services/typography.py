# ruff: noqa: RUF001 -- typographic quotes and dashes are this module's subject.
"""The book's punctuation, set as a printer would set it.

The story catalogue is typed on a keyboard: straight quotes, ``--`` and
``...``. A picture book prints curly quotes, true dashes and an ellipsis.
Both the PDF and the web reader set every word of the book through
`book_text`, so the two never disagree about a single apostrophe; prompts are
not touched, because the model needs no typography.

Quotes are decided by what stands before them, which is how a typesetter
reads them too: after a space, an opening bracket or a dash a quote opens;
anywhere else it closes (or, for ``'``, is an apostrophe: "Jack's", "'Tis"
excepted as rare enough not to be worth a dictionary).
"""

from __future__ import annotations

import re

__all__ = ["book_text"]

_OPENERS = frozenset(" \t\n([{—–-/“‘")

_LEFT_DOUBLE, _RIGHT_DOUBLE = "“", "”"
_LEFT_SINGLE, _RIGHT_SINGLE = "‘", "’"
_DASH = re.compile(r"\s*--\s*")
_ELLIPSIS = re.compile(r"\.\.\.")
# Hebrew writes its abbreviations with a straight quote inside the word
# (gershayim, geresh), so a line with Hebrew in it keeps its quotes as typed.
_HEBREW = re.compile(r"[\u0590-\u05ff]")


def book_text(text: str) -> str:
    """`text` with typographic quotes, em dashes and ellipses."""
    if not text:
        return text
    text = _DASH.sub("—", text)
    text = _ELLIPSIS.sub("…", text)
    if _HEBREW.search(text):
        return text
    out: list[str] = []
    for position, char in enumerate(text):
        if char not in "\"'":
            out.append(char)
            continue
        before = text[position - 1] if position else " "
        opening = before in _OPENERS
        if char == '"':
            out.append(_LEFT_DOUBLE if opening else _RIGHT_DOUBLE)
        else:
            out.append(_LEFT_SINGLE if opening else _RIGHT_SINGLE)
    return "".join(out)
