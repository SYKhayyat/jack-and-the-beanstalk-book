"""What may be printed on a child's book.

The name goes on the front cover and the About Me text goes on the back cover
of a physical book that is posted to a family. These tests pin the two rules
that keep an insult or an epithet out of print:

* a name has to look like a name and cannot be an insult, and
* the free text cannot carry profanity, slurs, threats or contact details.

They also pin the false-positive cases that matter: "The Ugly Duckling" is a
story, "Jean-Luc" and "O'Brien" are names, and lower-case typing is allowed.
"""

from __future__ import annotations

import pytest

from app.moderation import about_issues, first_issue_message, name_issue, text_issue


@pytest.mark.parametrize(
    "name", ["Sarah", "Noa", "Jean-Luc", "O'Brien", "Mary Jane", "J. Smith", "noa", "איה"]
)
def test_a_real_sounding_name_is_accepted(name: str) -> None:
    assert name_issue(name) is None


@pytest.mark.parametrize(
    "name",
    ["Stupid", "Idiot", "You are ugly", "Fat Pig", "Little brat", "Moron", "F4tty"],
)
def test_an_insult_or_epithet_is_not_a_name(name: str) -> None:
    issue = name_issue(name)
    assert issue is not None
    assert "name" in issue.message.lower()


@pytest.mark.parametrize(
    "name", ["A", "12345", "<script>", "name@example.com", "one two three four five six"]
)
def test_something_that_is_not_a_name_is_refused(name: str) -> None:
    assert name_issue(name) is not None


def test_profanity_and_slurs_are_refused_in_details() -> None:
    assert text_issue("Loves dinosaurs and tells jokes", field="details") is None
    for bad in ("You are an idiot", "what the fuck", "a retard", "she is so stupid"):
        issue = text_issue(bad, field="details")
        assert issue is not None, bad


def test_a_threat_is_refused_even_without_a_swear_word() -> None:
    assert text_issue("I will hurt you if you tell", field="details") is not None
    assert text_issue("shut up", field="details") is not None


def test_contact_details_are_not_printed() -> None:
    for text in (
        "Email us at family@example.com",
        "See https://example.com/noa",
        "Call 555-123-4567",
    ):
        assert text_issue(text, field="details") is not None, text


def test_an_innocent_classic_phrase_is_not_a_false_positive() -> None:
    assert text_issue("Loves The Ugly Duckling and our fat cat", field="details") is None


def test_about_issues_covers_the_names_and_the_free_text() -> None:
    issues = about_issues(
        {
            "prompt_name": "Idiot",
            "about_name": "Idiot",
            "title": "an astronaut",
            "dedication": "For my little shit",
            "details": "Loves dinosaurs",
        }
    )
    assert {issue.field for issue in issues} == {"prompt_name", "about_name", "dedication"}
    assert first_issue_message(issues)


def test_a_clean_about_page_has_no_issues() -> None:
    assert (
        about_issues(
            {
                "prompt_name": "Noa",
                "about_name": "Noa",
                "title": "an astronaut",
                "dedication": "For Grandma, who tells the best stories.",
                "details": "Loves dinosaurs and the sea.",
            }
        )
        == []
    )
