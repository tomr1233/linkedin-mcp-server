"""Tests for scripts/engagement.py, the client that prints engagement rows.

The script's only real job is turning the tool's references into the field
names a person asked for, and rendering them. Both are places where a silent
change (a renamed key, a dropped array) produces plausible-looking output.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "engagement.py"

spec = importlib.util.spec_from_file_location("engagement_script", SCRIPT)
assert spec and spec.loader
engagement_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engagement_script)

PAYLOAD = {
    "url": "https://www.linkedin.com/feed/update/urn:li:activity:111/",
    "sections": {"post_engagement": "Post: ..."},
    "references": {
        "post_engagement": [
            {
                "kind": "post_engagement",
                "url": "/in/ada",
                "text": "Ada Lovelace",
                "has_liked": True,
                "has_commented": True,
                "source_post_urls": [
                    "https://www.linkedin.com/feed/update/urn:li:activity:111/"
                ],
                "comments": ["Nice one."],
                "identified_by": "display_name",
            },
            {
                "kind": "post_engagement",
                "url": "/in/ACoAAabc",
                "text": "Grace Hopper",
                "has_liked": True,
                "has_commented": False,
                "source_post_urls": [
                    "https://www.linkedin.com/feed/update/urn:li:activity:111/"
                ],
                "comments": [],
                "identified_by": "profile_url",
            },
        ]
    },
}


class TestRowsFrom:
    def test_uses_the_requested_field_names(self):
        row = engagement_script.rows_from(PAYLOAD)[0]
        assert set(row) == {
            "Name",
            "Has Liked",
            "Has Commented",
            "LinkedIn profile",
            "Source Post URL",
            "Comments",
        }

    def test_carries_the_values_through(self):
        row = engagement_script.rows_from(PAYLOAD)[0]
        assert row["Name"] == "Ada Lovelace"
        assert row["Has Liked"] is True
        assert row["Has Commented"] is True
        assert row["LinkedIn profile"] == "/in/ada"
        assert row["Comments"] == ["Nice one."]

    def test_a_person_who_only_reacted_says_so(self):
        row = engagement_script.rows_from(PAYLOAD)[1]
        assert row["Has Liked"] is True
        assert row["Has Commented"] is False
        assert row["Comments"] == []

    def test_a_result_with_no_references_is_no_rows(self):
        assert engagement_script.rows_from({"url": "x", "sections": {}}) == []


class TestRender:
    def test_marks_what_each_person_did(self):
        out = engagement_script.render(engagement_script.rows_from(PAYLOAD), width=100)
        lines = [line for line in out.splitlines() if "Ada Lovelace" in line]
        assert lines and lines[0].startswith("LC")
        grace = [line for line in out.splitlines() if "Grace Hopper" in line]
        assert grace and grace[0].startswith("L·")

    def test_shows_the_comment_text(self):
        out = engagement_script.render(engagement_script.rows_from(PAYLOAD), width=100)
        assert "Nice one." in out

    def test_counts_the_people(self):
        out = engagement_script.render(engagement_script.rows_from(PAYLOAD), width=100)
        assert "2 people" in out
