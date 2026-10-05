"""Tests for the post engagement owner.

The JS itself never runs under a fake, so what these cover is the part Python
owns: which pages are opened, that the reactor list is reached by activating
its control rather than by a URL, that scrolling stops, and that both surfaces
arrive in one merged result.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from linkedin_mcp_server.linkedin.engagement import (
    ACTIVITY_POST_LINKS_JS,
    COMMENT_ROWS_JS,
    REACTOR_ROWS_JS,
    SCROLL_REACTIONS_JS,
    PostEngagementReader,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

POST = "https://www.linkedin.com/feed/update/urn:li:activity:111/"
PROFILE = "https://www.linkedin.com/in/williamhgates/"


class _FakeSession:
    """Answer each engagement script, and let the dialog grow per scroll."""

    def __init__(
        self,
        *,
        comments: list[dict[str, Any]],
        reactor_batches: list[list[dict[str, str]]],
        activity: list[str] | None = None,
        control: bool = True,
    ):
        self._comments = comments
        self._batches = reactor_batches
        self._activity = activity or []
        self._round = 0
        self.scrolls = 0
        self.delays: list[float] = []

        present = MagicMock()
        present.count = AsyncMock(return_value=1 if control else 0)
        present.wait_for = AsyncMock()
        present.scroll_into_view_if_needed = AsyncMock()
        present.click = AsyncMock()
        present.first = present
        self.control = present
        self.page = MagicMock()
        self.page.locator = MagicMock(return_value=present)
        self.page.wait_for_function = AsyncMock()
        self.page.wait_for_selector = AsyncMock()

    async def check_rate_limit(self) -> None:
        return None

    async def delay(self, seconds: float) -> None:
        self.delays.append(seconds)

    async def run_on_linkedin(self, script: str, arg: Any = None) -> Any:
        if script is ACTIVITY_POST_LINKS_JS:
            return self._activity
        if script is COMMENT_ROWS_JS:
            return self._comments
        if script is SCROLL_REACTIONS_JS:
            self.scrolls += 1
            self._round = min(self._round + 1, len(self._batches) - 1)
            return len(self._batches[self._round])
        if script is REACTOR_ROWS_JS:
            return {"rows": self._batches[self._round], "anonymous": 3}
        raise AssertionError(f"unexpected script: {script[:60]!r}")


def _reader(session: _FakeSession) -> tuple[PostEngagementReader, list[str]]:
    visited: list[str] = []
    navigator = MagicMock()
    navigator._navigate_to_page = AsyncMock(side_effect=visited.append)
    reader = PostEngagementReader(
        cast(PageSession, session), cast(PageNavigator, navigator)
    )
    return reader, visited


async def test_a_post_url_opens_the_post_and_not_a_reactions_url():
    """`/feed/update/<urn>/reactions/` is a 404; the list is a dialog."""
    session = _FakeSession(
        comments=[],
        reactor_batches=[[{"name": "Ada Lovelace", "profile_path": "/in/ada"}]],
    )
    reader, visited = _reader(session)

    await reader.get_post_engagement(POST)

    assert visited == [POST]
    session.control.click.assert_awaited_once()


async def test_a_profile_url_reads_the_activity_page_then_the_post():
    session = _FakeSession(
        comments=[],
        reactor_batches=[[{"name": "Ada Lovelace", "profile_path": "/in/ada"}]],
        activity=["urn:li:activity:111", "urn:li:activity:222"],
    )
    reader, visited = _reader(session)

    result = await reader.get_post_engagement(PROFILE)

    assert visited == [
        "https://www.linkedin.com/in/williamhgates/recent-activity/all/",
        POST,
    ]
    assert result["url"].endswith("/recent-activity/all/")


async def test_max_posts_bounds_the_navigations():
    """Each post is a page view, so the cap is the caller's budget."""
    session = _FakeSession(
        comments=[],
        reactor_batches=[[{"name": "Ada Lovelace", "profile_path": "/in/ada"}]],
        activity=[f"urn:li:activity:{n}" for n in range(9)],
    )
    reader, visited = _reader(session)

    await reader.get_post_engagement(PROFILE, max_posts=3)

    assert len([url for url in visited if "/feed/update/" in url]) == 3


async def test_scrolling_stops_at_max_people():
    batches = [
        [{"name": f"P{i}", "profile_path": f"/in/p{i}"} for i in range(n)]
        for n in (2, 4, 6, 8)
    ]
    session = _FakeSession(comments=[], reactor_batches=batches)
    reader, _ = _reader(session)

    result = await reader.get_post_engagement(POST, max_people=4)

    assert len(result["references"]["post_engagement"]) == 4
    assert "more exist" in result["sections"]["post_engagement"]
    # The fourth batch is never asked for: the cap ends the walk.
    assert session.scrolls == 1


async def test_scrolling_stops_after_two_rounds_that_add_nobody():
    batch = [{"name": "Ada", "profile_path": "/in/ada"}]
    session = _FakeSession(comments=[], reactor_batches=[batch] * 6)
    reader, _ = _reader(session)

    await reader.get_post_engagement(POST, max_people=100)

    assert session.scrolls == 2


async def test_one_round_that_lands_late_does_not_end_the_walk():
    """The next page can arrive a round after the scroll that asked for it."""
    ada = {"name": "Ada", "profile_path": "/in/ada"}
    grace = {"name": "Grace", "profile_path": "/in/grace"}
    session = _FakeSession(
        comments=[], reactor_batches=[[ada], [ada], [ada, grace], [ada, grace]]
    )
    reader, _ = _reader(session)

    result = await reader.get_post_engagement(POST, max_people=100)

    assert len(result["references"]["post_engagement"]) == 2


async def test_rows_a_lazy_list_unmounted_are_still_counted():
    """Each round shows only what is mounted; everyone seen is kept."""
    session = _FakeSession(
        comments=[],
        reactor_batches=[
            [{"name": "Ada", "profile_path": "/in/ada"}],
            [{"name": "Grace", "profile_path": "/in/grace"}],
            [{"name": "Grace", "profile_path": "/in/grace"}],
        ],
    )
    reader, _ = _reader(session)

    result = await reader.get_post_engagement(POST)

    urls = [row["url"] for row in result["references"]["post_engagement"]]
    assert urls == ["/in/ada", "/in/grace"]


async def test_a_post_without_a_reaction_control_still_returns_comments():
    """No control means nothing to open, not a failed read."""
    session = _FakeSession(
        comments=[
            {
                "comment_id": "urn:li:comment:(activity:111,1)",
                "name": "Grace Hopper",
                "profile_path": "/in/grace",
                "body": "Nice one.",
                "depth": 0,
            }
        ],
        reactor_batches=[[]],
        control=False,
    )
    reader, _ = _reader(session)

    result = await reader.get_post_engagement(POST)

    rows = result["references"]["post_engagement"]
    assert len(rows) == 1
    assert rows[0]["has_commented"] is True
    assert rows[0]["has_liked"] is False
    assert "none readable" in result["sections"]["post_engagement"]
    session.control.click.assert_not_awaited()


async def test_one_person_who_reacted_and_commented_is_one_row():
    """The surfaces name them differently; the result must not."""
    session = _FakeSession(
        comments=[
            {
                "comment_id": "urn:li:comment:(activity:111,1)",
                "name": "Ada Lovelace",
                "profile_path": "/in/ada",
                "body": "Nice one.",
                "depth": 0,
            }
        ],
        reactor_batches=[[{"name": "Ada Lovelace", "profile_path": "/in/ACoAAabc"}]],
    )
    reader, _ = _reader(session)

    result = await reader.get_post_engagement(POST)

    rows = result["references"]["post_engagement"]
    assert len(rows) == 1
    assert rows[0]["has_liked"] and rows[0]["has_commented"]
    assert rows[0]["url"] == "/in/ada"
