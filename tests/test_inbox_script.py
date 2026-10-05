import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "inbox.py"
_SPEC = importlib.util.spec_from_file_location("inbox_script", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
inbox = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inbox)

# Shape of `sections.inbox` as measured against live LinkedIn (en-US): chrome,
# then one block per row ending in its options line, then the text of whichever
# thread LinkedIn opened on landing. Names and messages are synthetic.
INBOX_TEXT = "\n".join(
    [
        "Messaging",
        "Search messages",
        "Focused",
        "Jobs Unread Connections InMail Starred",
        "Conversation List",
        "Attention screen reader users, messaging items continuously update.",
        "Dana Vendor",
        "Sep 7",
        "Sep 7",
        "",
        "Sponsored Evaluating Acme? Chat With An Expert.",
        "",
        ". Active conversation",
        ". Press return to go to conversation details",
        "Open the options list in your conversation with Dana Vendor",
        "Status is reachable",
        "Sam Lee",
        "Jul 12",
        "Jul 12",
        "",
        "Sam: Hey, are you around   next week?",
        "",
        ". Press return to go to conversation details",
        "Open the options list in your conversation with Sam Lee and Me",
        "Kim Recruiter",
        "Jun 7",
        "Jun 7",
        "",
        "InMail Open roles hard to fill?",
        "",
        ". Press return to go to conversation details",
        "Open the options list in your conversation with Me and Kim Recruiter",
        "Ari Park",
        "May 8",
        "May 8",
        "",
        " Ari sent an attachment",
        "",
        ". Press return to go to conversation details",
        "Open the options list in your conversation with Me and Ari Park",
        "Dana Vendor",
        "SVP of Product at Acme",
        "Open the options list in your conversation with Dana Vendor",
        "",
        "Sponsored",
        "",
        "Sep 7",
        "Dana Vendor",
        "SVP of Product at Acme",
        "MONDAY",
        "Dana Vendor sent the following messages at 11:27 AM",
    ]
)


def test_parses_one_entry_per_row():
    entries = inbox.parse_inbox(INBOX_TEXT)

    assert [e.name for e in entries] == [
        "Dana Vendor",
        "Sam Lee",
        "Kim Recruiter",
        "Ari Park",
    ]
    assert [e.date for e in entries] == ["Sep 7", "Jul 12", "Jun 7", "May 8"]


def test_labels_and_snippets():
    dana, sam, kim, ari = inbox.parse_inbox(INBOX_TEXT)

    assert (dana.label, dana.snippet) == (
        "Sponsored",
        "Evaluating Acme? Chat With An Expert.",
    )
    assert (sam.label, sam.snippet) == ("", "Sam: Hey, are you around next week?")
    assert (kim.label, kim.snippet) == ("InMail", "Open roles hard to fill?")
    assert ari.snippet == "Ari sent an attachment"


def test_ignores_the_open_thread_after_the_list():
    entries = inbox.parse_inbox(INBOX_TEXT)

    assert all("SVP" not in e.snippet for e in entries)
    assert len(entries) == 4


def test_empty_text_has_no_entries():
    assert inbox.parse_inbox("") == []


def test_render_caps_previews_at_two_lines_unless_full():
    long = inbox.Conversation(name="Sam Lee", date="Jul 12", snippet="word " * 80)

    capped = inbox.render([long], width=60).splitlines()
    full = inbox.render([long], width=60, full=True).splitlines()

    assert len(capped) == 5  # title, blank, head, two preview lines
    assert capped[-1].endswith("…")
    assert len(full) > 5


def test_render_lists_each_conversation():
    out = inbox.render(inbox.parse_inbox(INBOX_TEXT), width=60)

    assert out.splitlines()[0] == "LinkedIn inbox · 4 conversations"
    assert "Sam Lee" in out
    assert "[InMail]" in out
    assert all(len(line) <= 60 for line in out.splitlines())
