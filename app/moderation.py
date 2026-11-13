"""What may be printed on a child's book.

The About Me page is free text typed by a visitor, and the child's name is
free text too. Both are printed and shipped, so they are checked before a PDF
is built and checked *again* before a print order is placed: a book made before
this rule existed still cannot print something inappropriate.

This is a deterministic local check on purpose — no model call, no key, no
network — so it costs nothing, cannot fail because a service is down, and a
test can pin exactly what it does. It is a blocklist plus a name-shape rule,
not a name dictionary: it can reject an insult or an epithet and it can reject
text that is unmistakably explicit, but it cannot prove that "Blorp" is not a
real name. That limitation is stated in the print page's help copy and in the
write-up rather than hidden.

The two entry points:

* :func:`name_issue` — is this a plausible, non-abusive name?
* :func:`about_issues` — every problem with an About Me page, as field/message
  pairs a route can return to the person who typed them.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "ContentIssue",
    "about_issues",
    "first_issue_message",
    "name_issue",
    "text_issue",
]


#: A problem with one printed field, addressed to the person who typed it.
@dataclass(frozen=True, slots=True)
class ContentIssue:
    field: str
    message: str


# --- what must never be printed ---------------------------------------------

#: Profanity, slurs, sexual words and the insults a child can be called.
#: Matched as whole words after normalisation, never as substrings: the
#: Scunthorpe problem is a real name being rejected for letters inside it.
_BLOCKED = frozenset(
    {
        # profanity
        "ass",
        "asshole",
        "bastard",
        "bitch",
        "bollocks",
        "bullshit",
        "crap",
        "cunt",
        "damn",
        "dick",
        "fuck",
        "fucked",
        "fucker",
        "fucking",
        "piss",
        "prick",
        "shit",
        "shitty",
        "slut",
        "twat",
        "whore",
        # sexual
        "boobs",
        "naked",
        "nude",
        "penis",
        "porn",
        "rape",
        "sex",
        "tits",
        "vagina",
        # slurs
        "chink",
        "cripple",
        "dyke",
        "faggot",
        "kike",
        "midget",
        "nigger",
        "retard",
        "spastic",
        "spic",
        "tranny",
        "wetback",
        # insults and epithets
        "brat",
        "butthead",
        "crybaby",
        "dumb",
        "dumbass",
        "dummy",
        "fatso",
        "fatty",
        "idiot",
        "imbecile",
        "jerk",
        "loser",
        "moron",
        "obese",
        "pig",
        "poophead",
        "scum",
        "sissy",
        "stupid",
        "stupidhead",
        "twit",
        "ugly",
        "wimp",
    }
)

#: Words that are an insult in a name but innocent in a sentence ("The Ugly
#: Duckling", "our fat cat"). A name is a label for a person, so the bar is
#: higher there.
_NAME_ONLY_BLOCKED = frozenset({"fat", "pig", "trash", "garbage", "dumb"})

#: Whole phrases that are a threat or a put-down even when every word is
#: ordinary. Kept few and specific so a story reference is not caught.
_BLOCKED_PHRASES = (
    (
        re.compile(
            r"\b(?:i|we)\s+(?:will|'ll|am going to|are going to)\s+(?:kill|hurt)\s+you\b"
        ),
        "a threat",
    ),
    (re.compile(r"\bshut\s+up\b"), "a put-down"),
    (re.compile(r"\b(?:i|we)\s+hate\s+you\b"), "a put-down"),
    (re.compile(r"\b(?:kill|hurt)\s+yourself\b"), "a threat"),
)

#: Contact details do not belong in a printed children's book: they leak a
#: family's address or phone number to whoever holds the paper copy.
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?){2,4}\d{2,4}(?!\d)")

#: A small allowlist for the one classic phrase whose adjective is otherwise
#: blocked, so "loves The Ugly Duckling" is not refused.
_ALLOWED_PHRASES = (re.compile(r"\bugly\s+duckling\b"),)

#: The characters a person's name may contain, beyond letters: a space, a
#: hyphen, an apostrophe (straight or curly) and a period for initials.
_NAME_CHARS = re.compile(r"^[\w\s.''\-]+$", re.UNICODE)
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _normalise(text: str) -> str:
    """Fold a string into the form the lists are written in.

    NFKC turns full-width and decomposed lookalikes into the same letters;
    casefold makes the match case-insensitive; zero-width characters are
    dropped so a blocked word cannot be split invisibly. The text actually
    printed is never changed by this — only the copy that is checked.
    """
    return (
        unicodedata.normalize("NFKC", text)
        .casefold()
        .translate({ord(ch): None for ch in "\u200b\u200c\u200d\u2060\ufeff"})
    )


def _tokens(text: str) -> list[str]:
    """The words of a string, with simple leetspeak folded back."""
    return [match.group(0).translate(_LEET) for match in _WORD.finditer(_normalise(text))]


def _blocked_word(text: str, *, extra: frozenset[str] = frozenset()) -> str | None:
    """The first blocked word in `text`, if any."""
    for token in _tokens(text):
        if token in _BLOCKED or token in extra:
            return token
    return None


def _without_allowed_phrases(text: str) -> str:
    """Blank the innocent classic phrases before the word scan runs."""
    folded = _normalise(text)
    for pattern in _ALLOWED_PHRASES:
        folded = pattern.sub(" ", folded)
    return folded


# --- names --------------------------------------------------------------------


def name_issue(name: str, *, field: str = "about_name") -> ContentIssue | None:
    """Why this is not a name that may be printed, or None if it is fine.

    A name has to look like one — letters, spaces, a hyphen or apostrophe, a
    period for an initial — and must not be an insult, an epithet, profanity or
    a slur. It deliberately accepts lower-case typing and uncommon spellings:
    "noa", "Ng", "Jean-Luc" and "O'Brien" are all names. It cannot check a
    national name register; the blocklist is the line.
    """
    cleaned = unicodedata.normalize("NFKC", name).strip()
    if not cleaned:
        return ContentIssue(field, "Please give the child a name.")
    if len(cleaned) > 60:
        return ContentIssue(field, "That name is too long — 60 characters at most.")
    if not _NAME_CHARS.match(cleaned):
        return ContentIssue(
            field,
            "A name can contain letters, spaces, hyphens, apostrophes and periods only.",
        )
    letters = sum(1 for ch in cleaned if ch.isalpha())
    if letters < 2:
        return ContentIssue(
            field, "That does not look like a name — please use at least two letters."
        )
    words = [word for word in re.split(r"[\s\-]+", cleaned) if word]
    if len(words) > 5:
        return ContentIssue(
            field, "That is too many words for a name — please use the child's name."
        )
    if any(not any(ch.isalpha() for ch in word) for word in words):
        return ContentIssue(field, "That does not look like a name.")
    hit = _blocked_word(_without_allowed_phrases(cleaned), extra=_NAME_ONLY_BLOCKED)
    if hit:
        return ContentIssue(
            field,
            "A name cannot be an insult or an unkind word. Please use the child's real name.",
        )
    return None


# --- About Me text ------------------------------------------------------------


def text_issue(text: str, *, field: str) -> ContentIssue | None:
    """Why this sentence may not be printed, or None if it may.

    Checks clear profanity, slurs, sexual words, threats and contact details.
    Ordinary strong feelings ("she hates broccoli") are left alone; only the
    specific phrases above are refused.
    """
    if not text.strip():
        return None
    folded = _without_allowed_phrases(text)
    hit = _blocked_word(folded)
    if hit:
        return ContentIssue(
            field,
            "That wording is not appropriate for a printed children's book. "
            "Please rewrite it without swearing or insults.",
        )
    for pattern, reason in _BLOCKED_PHRASES:
        if pattern.search(folded):
            return ContentIssue(
                field,
                f"That reads like {reason}, which should not be printed. Please rewrite it.",
            )
    if _EMAIL.search(folded) or _URL.search(folded):
        return ContentIssue(
            field,
            "Please leave email addresses and web links off the page — "
            "this is printed and handed to a child.",
        )
    if _PHONE.search(folded):
        return ContentIssue(
            field,
            "Please leave phone numbers off the page — this is printed and handed to a child.",
        )
    return None


#: The fields of AboutMe that are printed, and the wording the page uses for
#: each one. `prompt_name` is the child's name in the story and in the cover
#: title; `about_name` is the name on the About page; they may differ.
_PRINTED_FIELDS = (
    ("prompt_name", "the child's name"),
    ("about_name", "the About page name"),
    ("title", "what they want to be"),
    ("dedication", "the dedication"),
    ("details", "the About page details"),
)


def about_issues(about: Mapping[str, object] | object) -> list[ContentIssue]:
    """Every printer-safety problem with an About Me record.

    Accepts an `AboutMe` or its `model_dump()`; only strings are read, so the
    model does not need to import this module and this module does not need
    the model. The first issue per field is returned.
    """
    getter = (
        about.get
        if isinstance(about, Mapping)
        else lambda key, default=None: getattr(about, key, default)
    )
    issues: list[ContentIssue] = []
    seen: set[str] = set()
    for field, _label in _PRINTED_FIELDS:
        value = getter(field)
        if not isinstance(value, str) or not value.strip():
            continue
        issue = (
            name_issue(value, field=field)
            if field in {"prompt_name", "about_name"}
            else text_issue(value, field=field)
        )
        if issue is not None and field not in seen:
            issues.append(issue)
            seen.add(field)
    return issues


def first_issue_message(issues: list[ContentIssue]) -> str:
    """One sentence for a route that shows a single error."""
    if not issues:
        return ""
    return issues[0].message
