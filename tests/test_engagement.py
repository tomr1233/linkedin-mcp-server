"""Tests for merging a post's reactors and commenters into people.

The merge is the part of get_post_engagement that has no browser in it and the
part most likely to be wrong in a way nobody notices: a person who both reacted
and commented silently becoming two rows, or two different people becoming one.
"""

from linkedin_mcp_server.linkedin.engagement import (
    Comment,
    Person,
    PostEngagement,
    fold_name,
    merge_engagement,
    render_engagement_text,
)

POST_A = "https://www.linkedin.com/feed/update/urn:li:activity:111/"
POST_B = "https://www.linkedin.com/feed/update/urn:li:activity:222/"

# The forms the two surfaces actually produce, kept verbatim from a live read.
OPAQUE = "/in/ACoAAG14sGkBu6jGr0wYwRxY8Eo4I6iVVmbV7xw"
VANITY = "/in/rene-sietse-vereijken-301068355"


class TestFoldName:
    def test_folds_case_and_accents(self):
        assert fold_name("René Sietse Vereijken") == fold_name("rene sietse vereijken")

    def test_collapses_whitespace(self):
        assert fold_name("  Ada   Lovelace ") == "ada lovelace"

    def test_keeps_different_people_apart(self):
        assert fold_name("Ada Lovelace") != fold_name("Ada Byron")


class TestMergeEngagement:
    def test_reactor_and_commenter_with_one_name_is_one_row(self):
        """The whole point: the two surfaces disagree about the URL."""
        post = PostEngagement(
            post_url=POST_A,
            reactors=[Person(name="René Sietse Vereijken", profile_path=OPAQUE)],
            comments=[
                Comment(
                    author=Person(name="Rene Sietse Vereijken", profile_path=VANITY),
                    body="Agreed.",
                )
            ],
        )
        rows = merge_engagement([post])
        assert len(rows) == 1
        assert rows[0]["has_liked"] is True
        assert rows[0]["has_commented"] is True
        assert rows[0]["comments"] == ["Agreed."]

    def test_merged_row_prefers_the_vanity_url(self):
        """An opaque id reaches the profile but tells a reader nothing."""
        post = PostEngagement(
            post_url=POST_A,
            reactors=[Person(name="Ada Lovelace", profile_path=OPAQUE)],
            comments=[
                Comment(
                    author=Person(name="Ada Lovelace", profile_path="/in/ada"),
                    body="Yes.",
                )
            ],
        )
        assert merge_engagement([post])[0]["url"] == "/in/ada"

    def test_merged_row_says_it_was_matched_on_a_name(self):
        post = PostEngagement(
            post_url=POST_A,
            reactors=[Person(name="Ada Lovelace", profile_path=OPAQUE)],
            comments=[
                Comment(
                    author=Person(name="Ada Lovelace", profile_path="/in/ada"),
                    body="Yes.",
                )
            ],
        )
        assert merge_engagement([post])[0]["identified_by"] == "display_name"

    def test_unmerged_row_is_not_claimed_as_a_name_match(self):
        post = PostEngagement(
            post_url=POST_A,
            reactors=[Person(name="Ada Lovelace", profile_path=OPAQUE)],
        )
        row = merge_engagement([post])[0]
        assert row["identified_by"] == "profile_url"
        assert row["has_commented"] is False

    def test_one_person_across_two_posts_is_one_row_with_both_posts(self):
        posts = [
            PostEngagement(
                post_url=POST_A,
                reactors=[Person(name="Ada Lovelace", profile_path=OPAQUE)],
            ),
            PostEngagement(
                post_url=POST_B,
                reactors=[Person(name="Ada Lovelace", profile_path=OPAQUE)],
            ),
        ]
        rows = merge_engagement(posts)
        assert len(rows) == 1
        assert rows[0]["source_post_urls"] == [POST_A, POST_B]

    def test_different_people_stay_apart(self):
        post = PostEngagement(
            post_url=POST_A,
            reactors=[
                Person(name="Ada Lovelace", profile_path="/in/ada"),
                Person(name="Grace Hopper", profile_path="/in/grace"),
            ],
        )
        assert len(merge_engagement([post])) == 2

    def test_two_people_with_one_name_stay_apart(self):
        """Two slugs are two people, whatever they are called."""
        post = PostEngagement(
            post_url=POST_A,
            reactors=[Person(name="Ada Lovelace", profile_path="/in/ada-1")],
            comments=[
                Comment(
                    author=Person(name="Ada Lovelace", profile_path="/in/ada-2"),
                    body="Yes.",
                )
            ],
        )
        rows = merge_engagement([post])
        assert [row["url"] for row in rows] == ["/in/ada-1", "/in/ada-2"]

    def test_one_slug_is_one_person_however_it_is_spelled(self):
        post = PostEngagement(
            post_url=POST_A,
            reactors=[Person(name="Ada Lovelace", profile_path="/in/ada")],
            comments=[
                Comment(
                    author=Person(name="Ada King", profile_path="/in/ada"),
                    body="Yes.",
                )
            ],
        )
        rows = merge_engagement([post])
        assert len(rows) == 1
        assert rows[0]["has_liked"] and rows[0]["has_commented"]
        assert rows[0]["identified_by"] == "profile_url"

    def test_replies_join_the_same_persons_comments(self):
        post = PostEngagement(
            post_url=POST_A,
            comments=[
                Comment(
                    author=Person(name="Ada Lovelace", profile_path="/in/ada"),
                    body="First thought.",
                ),
                Comment(
                    author=Person(name="Ada Lovelace", profile_path="/in/ada"),
                    body="A reply to someone.",
                ),
            ],
        )
        rows = merge_engagement([post])
        assert len(rows) == 1
        assert rows[0]["comments"] == ["First thought.", "A reply to someone."]

    def test_anonymous_reactors_never_become_rows(self):
        """They have no profile, so a row for them cannot be acted on."""
        post = PostEngagement(
            post_url=POST_A,
            reactors=[Person(name="Ada Lovelace", profile_path="/in/ada")],
            anonymous_reactors=12,
        )
        assert len(merge_engagement([post])) == 1


class TestRenderEngagementText:
    def test_states_that_a_list_was_cut_short(self):
        post = PostEngagement(
            post_url=POST_A,
            reactors=[Person(name="Ada Lovelace", profile_path="/in/ada")],
            reactions_truncated=True,
        )
        assert "more exist" in render_engagement_text([post])

    def test_a_complete_list_claims_nothing_about_more(self):
        post = PostEngagement(
            post_url=POST_A,
            reactors=[Person(name="Ada Lovelace", profile_path="/in/ada")],
        )
        assert "more exist" not in render_engagement_text([post])

    def test_counts_the_people_linkedin_would_not_name(self):
        post = PostEngagement(
            post_url=POST_A,
            reactors=[Person(name="Ada Lovelace", profile_path="/in/ada")],
            anonymous_reactors=12,
        )
        assert "12 not visible to you" in render_engagement_text([post])

    def test_reports_a_post_whose_reactions_could_not_be_opened(self):
        post = PostEngagement(post_url=POST_A, dialog_missing=True)
        assert "none readable" in render_engagement_text([post])
