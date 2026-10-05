"""Read who reacted to and commented on a post, and merge them into people.

Two surfaces, one person. Reactors live in a dialog opened from the post's
reaction facepile; commenters live in the post page itself. Everything here is
either the browser-side JS that reads one of those surfaces, or the merge that
turns both into one row per person.

The post page is server-driven UI: class names are hashed per build, and what
stays put is ``componentkey``, ``data-testid`` and ``data-component-type``.
Four measurements against live LinkedIn (2026-10) shape this module:

- **There is no reactions URL.** ``/feed/update/<urn>/reactions/`` serves "This
  page doesn't exist". The reactor list opens from the post link that sits
  inside ``[data-testid^="ReactionFacepileCollection-"]``. The facepile's other
  links are reactors' profiles opening in a new tab, so the post link is the
  only safe thing in it to activate. A click, not a navigation.
- **The reactor list is a lazy column.** Rows are the children of the dialog's
  ``[data-component-type="LazyColumn"]``, which is also what scrolls. Rows are
  collected across scroll rounds by profile path, so a list that unmounts what
  scrolled away still yields everyone it showed.
- **Both surfaces now name people by vanity slug.** Reactor rows used to carry
  an opaque member id (``/in/ACoAA...``) while comments carried the slug, so
  the merge had to fall back on display names. On a post with 2,499 reactions
  every reactor row read held a slug, so people are merged on their profile
  path, and a name is used only to bridge an opaque id should one still turn
  up. Such a row says so in ``identified_by``.
- **A comment is ``[componentkey^="replaceableComment_"]``** and its body is
  ``[data-testid="expandable-text-box"]``. The author's name and headline sit
  in the profile link beside it, the name being the first paragraph's own text:
  the connection degree and badges are child elements, and a verified member's
  name is repeated in an ``aria-hidden`` copy next to screen-reader text that
  spells the badge out in the viewer's language.

None of this reads a text value to decide anything, so it does not turn on the
browser's locale.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.linkedin.identifiers import (
    classify_engagement_target,
    person_profile_url,
    post_update_url,
)
from linkedin_mcp_server.linkedin.link_metadata import EngagementReference
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession

logger = logging.getLogger(__name__)

# A profile path LinkedIn issued instead of a public identifier. Only the
# reactions dialog produces these, and they are useless for merging.
_OPAQUE_PATH = re.compile(r"^/in/ACoAA", re.IGNORECASE)

# Post permalinks as they appear on an activity page, in both shapes LinkedIn
# serves. Used to turn a person's profile into the posts to read.
ACTIVITY_POST_LINKS_JS = """
() => {
  const seen = [];
  const push = (v) => { if (v && !seen.includes(v)) seen.push(v); };
  // An anchor is the permalink itself; a data attribute names the activity the
  // container holds. Both appear on the same page and neither alone is
  // complete, so both are collected in document order.
  document.querySelectorAll('[data-urn], [data-id]').forEach(el => {
    const v = el.getAttribute('data-urn') || el.getAttribute('data-id');
    if (v && v.indexOf('urn:li:activity:') === 0) push(v);
  });
  document.querySelectorAll('a[href*="/feed/update/"]').forEach(a => {
    try {
      const parts = new URL(a.href, location.origin).pathname.split('/').filter(Boolean);
      const i = parts.indexOf('update');
      if (i >= 0 && parts[i + 1]) push(decodeURIComponent(parts[i + 1]));
    } catch (e) { /* an anchor without a parseable href names nothing */ }
  });
  return seen;
}
"""

# Shared by the scripts that read people out of the post page. The visible name
# is the first paragraph's own text, read from the aria-hidden copy where one
# exists because the other copy carries localized badge text.
_PERSON_HELPERS_JS = """
  const pathOf = (a) => {
    try { return new URL(a.href, location.origin).pathname.replace(/\\/$/, ''); } catch (e) { return null; }
  };
  const nameIn = (link) => {
    const p = link && link.querySelector('p');
    if (!p) return '';
    const visual = p.querySelector('span[aria-hidden="true"]') || p.querySelector('span') || p;
    const own = Array.from(visual.childNodes)
      .filter(n => n.nodeType === Node.TEXT_NODE)
      .map(n => n.textContent).join('').trim();
    return own || (p.innerText || '').split('\\n')[0].trim();
  };
  // A minimised chat bubble can be a dialog too, and holds an editor where the
  // reactions dialog never does.
  const reactorList = () => {
    const lists = Array.from(document.querySelectorAll('dialog[open] [data-component-type="LazyColumn"]'))
      .filter(list => !list.closest('dialog').querySelector('[contenteditable="true"]'));
    return lists.length ? lists[lists.length - 1] : null;
  };
"""

# Rows of the reactions dialog. Anonymous rows are the ones LinkedIn will not
# name to this session; they are counted here and dropped from the result,
# because a row with no profile cannot be merged, visited or acted on. What an
# anonymous row looks like in this layout was not measured: a picture with no
# link at all is the reading, and separators, which have neither, are not
# counted.
REACTOR_ROWS_JS = (
    "() => {"
    + _PERSON_HELPERS_JS
    + """
  const list = reactorList();
  if (!list) return {rows: [], anonymous: 0, present: false};
  const rows = [];
  let anonymous = 0;
  Array.from(list.children).forEach(item => {
    const a = item.querySelector('a[href*="/in/"]');
    if (!a) {
      if (item.querySelector('figure') && !item.querySelector('a[href]')) anonymous += 1;
      return;
    }
    const path = pathOf(a);
    if (path) rows.push({name: nameIn(a), profile_path: path});
  });
  return {rows, anonymous, present: true};
}
"""
)

# The reactor list is its own scroll container, and loads its next page when
# the empty sentinel at its end comes into view. Measured: setting scrollTop
# alone moved the list without loading anything on the first round, while
# bringing the last child into view did, so both are done. Returns the row
# count so the caller can see a scroll that reached nothing.
SCROLL_REACTIONS_JS = (
    "() => {"
    + _PERSON_HELPERS_JS
    + """
  const scroller = reactorList();
  if (!scroller) return 0;
  scroller.scrollTop = scroller.scrollHeight;
  if (scroller.lastElementChild) scroller.lastElementChild.scrollIntoView({block: 'end'});
  return scroller.querySelectorAll('a[href*="/in/"]').length;
}
"""
)

# Comments, including any replies the page rendered. The comment urn is in the
# componentkey, and nesting inside another comment is what makes a reply a
# reply — no text is read to decide that.
COMMENT_ROWS_JS = (
    "() => {"
    + _PERSON_HELPERS_JS
    + """
  const COMMENT = '[componentkey^="replaceableComment_"]';
  return Array.from(document.querySelectorAll(COMMENT)).map(node => {
    const clone = node.cloneNode(true);
    // A reply's text belongs to the reply, not to the comment holding it.
    clone.querySelectorAll(COMMENT).forEach(n => n.remove());
    // Mentions inside the body are profile links too; the author's come first
    // and sit outside it. The one holding a paragraph carries the name.
    const links = Array.from(clone.querySelectorAll('a[href*="/in/"], a[href*="/company/"]'))
      .filter(a => !a.closest('[data-testid="expandable-text-box"]'));
    const named = links.find(a => a.querySelector('p')) || links[0] || null;
    const body = clone.querySelector('[data-testid="expandable-text-box"]');
    let depth = 0;
    let parent = node.parentElement;
    while (parent) {
      if (parent.matches && parent.matches(COMMENT)) depth += 1;
      parent = parent.parentElement;
    }
    return {
      comment_id: (node.getAttribute('componentkey') || '').replace('replaceableComment_', ''),
      name: nameIn(named),
      profile_path: named ? pathOf(named) : null,
      body: body ? (body.innerText || '').trim() : '',
      depth: depth,
    };
  });
}
"""
)


@dataclass
class Person:
    """One person as a single surface named them."""

    name: str
    profile_path: str


@dataclass
class Comment:
    """One comment or reply, with the person who wrote it."""

    author: Person
    body: str


@dataclass
class PostEngagement:
    """Everything read from one post."""

    post_url: str
    reactors: list[Person] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    anonymous_reactors: int = 0
    reactions_truncated: bool = False
    dialog_missing: bool = False


def fold_name(name: str) -> str:
    """A display name reduced to what two spellings of one person share.

    Accents and case are dropped because the same person is written both ways
    across LinkedIn's surfaces. Nothing else is: two different people with one
    name fold together, and that is the known cost of merging without a usable
    identifier.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


def _prefer_path(paths: list[str]) -> str:
    """The most useful of the profile paths seen for one person.

    A vanity slug beats an opaque member id: both reach the profile, but only
    one survives being read by a person or passed to another tool here.
    """
    for path in paths:
        if not _OPAQUE_PATH.match(path):
            return path
    return paths[0] if paths else ""


def merge_engagement(posts: list[PostEngagement]) -> list[EngagementReference]:
    """One row per person across every post read.

    Keyed on the profile path, which both surfaces now give as a vanity slug.
    A display name joins two entries only across an opaque member id, never
    two different slugs, because two slugs are two people whatever they are
    called. A row joined that way reports ``identified_by: display_name`` and
    is a guess the caller can see; every other row reports ``profile_url``.
    """
    rows: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}

    def slot(person: Person) -> dict[str, Any] | None:
        path_key = person.profile_path.casefold()
        name_key = fold_name(person.name)
        if not path_key and not name_key:
            return None
        row: dict[str, Any] | None = by_path.get(path_key) if path_key else None
        if row is None and name_key:
            opaque = not path_key or bool(_OPAQUE_PATH.match(person.profile_path))
            for candidate in by_name.get(name_key, []):
                if opaque or all(_OPAQUE_PATH.match(p) for p in candidate["paths"]):
                    row = candidate
                    row["by_name"] = True
                    break
        if row is None:
            row = {
                "name": person.name,
                "paths": [],
                "by_name": False,
                "has_liked": False,
                "has_commented": False,
                "posts": [],
                "comments": [],
            }
            rows.append(row)
            if name_key:
                by_name.setdefault(name_key, []).append(row)
        if path_key and path_key not in by_path:
            by_path[path_key] = row
            row["paths"].append(person.profile_path)
        return row

    for post in posts:
        for reactor in post.reactors:
            row = slot(reactor)
            if row is None:
                continue
            row["has_liked"] = True
            if post.post_url not in row["posts"]:
                row["posts"].append(post.post_url)
        for comment in post.comments:
            row = slot(comment.author)
            if row is None:
                continue
            row["has_commented"] = True
            if post.post_url not in row["posts"]:
                row["posts"].append(post.post_url)
            if comment.body and comment.body not in row["comments"]:
                row["comments"].append(comment.body)

    merged: list[EngagementReference] = []
    for row in rows:
        reference: EngagementReference = {
            "kind": "post_engagement",
            "url": _prefer_path(row["paths"]),
            "text": row["name"],
            "context": "post engagement",
            "has_liked": row["has_liked"],
            "has_commented": row["has_commented"],
            "source_post_urls": row["posts"],
            "comments": row["comments"],
            "identified_by": "display_name" if row["by_name"] else "profile_url",
        }
        merged.append(reference)
    return merged


def render_engagement_text(posts: list[PostEngagement]) -> str:
    """The readable section text: what was read, per post, and what was not.

    Truncation is stated rather than implied. A list that stopped at the cap
    and a list that ended look identical in the rows alone, and a caller acting
    on "everyone who engaged" needs to know which one it got.
    """
    blocks: list[str] = []
    for post in posts:
        lines = [f"Post: {post.post_url}"]
        if post.dialog_missing:
            lines.append(
                "Reactions: none readable — the post exposes no reaction details "
                "(it may have no reactions)."
            )
        else:
            counted = f"Reactions: {len(post.reactors)} read"
            if post.reactions_truncated:
                counted += " (stopped at the requested maximum; more exist)"
            if post.anonymous_reactors:
                counted += (
                    f" · {post.anonymous_reactors} not visible to you "
                    "(out of your network)"
                )
            lines.append(counted)
            lines.extend(
                f"  {person.name} — {person.profile_path}" for person in post.reactors
            )
        lines.append(f"Comments: {len(post.comments)} read")
        for comment in post.comments:
            lines.append(f"  {comment.author.name} — {comment.author.profile_path}")
            if comment.body:
                lines.append(f"    {comment.body}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# The post link inside the facepile opens the reactor list; the facepile's other
# links are reactors' profiles in a new tab.
_REACTION_DETAILS_SELECTOR = (
    '[data-testid^="ReactionFacepileCollection-"] a[href*="/feed/update/"]'
)
# The reactor list once it is open. A minimised chat bubble can be a dialog too,
# and is the only one carrying an editor.
_REACTOR_LIST_SELECTOR = (
    'dialog[open]:not(:has([contenteditable="true"])) '
    '[data-component-type="LazyColumn"]'
)
# Either surface having rendered means the post's engagement is on the page.
_ENGAGEMENT_READY_SELECTOR = (
    '[data-testid^="ReactionFacepileCollection-"], '
    '[componentkey^="replaceableComment_"]'
)


class PostEngagementReader:
    """Own the workflow that turns posts into the people engaging with them."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def _wait_for_main_text(
        self,
        *,
        minimum_length: int = 100,
        timeout: int = 10000,
        log_context: str,
    ) -> None:
        """Wait for main content to populate enough text to read."""
        try:
            await self._session.page.wait_for_function(
                """({ minimumLength }) => {
                    const main = document.querySelector('main');
                    if (!main) return false;
                    return main.innerText.length > minimumLength;
                }""",
                arg={"minimumLength": minimum_length},
                timeout=timeout,
            )
        except PlaywrightTimeoutError:
            logger.debug("%s content did not appear", log_context)

    async def _open_page(self, url: str, log_context: str) -> None:
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._wait_for_main_text(log_context=log_context)

    async def _reactor_list_is_open(self, *, timeout: int = 1000) -> bool:
        """Return whether the reactions dialog has rendered its list."""
        locator = self._session.page.locator(_REACTOR_LIST_SELECTOR)
        try:
            await locator.first.wait_for(state="visible", timeout=timeout)
            return True
        except Exception:
            return False

    async def _click_first(self, selector: str, *, timeout: int = 5000) -> None:
        """Click the first visible locator that matches a selector."""
        target = self._session.page.locator(selector).first
        try:
            await target.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            logger.debug("Could not scroll %s into view", selector, exc_info=True)
        await target.click(timeout=timeout)

    async def get_post_engagement(
        self,
        url: str,
        *,
        max_posts: int = 1,
        max_people: int = 100,
    ) -> dict[str, Any]:
        """Read who reacted to and commented on a post, merged into people.

        Accepts a post permalink or a person's profile URL. A profile costs one
        extra navigation to their activity page, where the newest ``max_posts``
        permalinks are read; the reactions themselves cost nothing extra,
        because the reactor list is a dialog on the post rather than a page of
        its own.

        Comments are the ones the post page renders on load, replies included.
        Nothing here clicks a "load more comments" control: no handle for it was
        found that does not read the button's text, and the repo does not
        classify by text. So a long thread reports the comments it showed, and
        ``max_people`` bounds the reactors.

        Returns:
            {url, sections: {post_engagement: text},
             references: {post_engagement: [engagement rows]}}
        """
        kind, target = classify_engagement_target(url)

        if kind == "post":
            post_urls = [target]
            landing_url = target
        else:
            landing_url = person_profile_url(target, "/recent-activity/all/")
            await self._open_page(landing_url, "Activity page")
            urns: list[str] = (
                await self._session.run_on_linkedin(ACTIVITY_POST_LINKS_JS) or []
            )
            post_urls = [post_update_url(urn, "/") for urn in urns[:max_posts]]
            if not post_urls:
                logger.info("No posts found on %s", landing_url)
                return {"url": landing_url, "sections": {}}

        posts: list[PostEngagement] = []
        for index, post_url in enumerate(post_urls):
            if index or kind == "person":
                await self._session.delay(NAV_DELAY)
            posts.append(
                await self._read_post_engagement(post_url, max_people=max_people)
            )

        text = render_engagement_text(posts)
        references = merge_engagement(posts)
        result: dict[str, Any] = {"url": landing_url, "sections": {}}
        if text:
            result["sections"]["post_engagement"] = text
            if references:
                result["references"] = {"post_engagement": list(references)}
        return result

    async def _read_post_engagement(
        self,
        post_url: str,
        *,
        max_people: int,
    ) -> PostEngagement:
        """Read one post's reactors and commenters."""
        await self._open_page(post_url, "Post")
        # Main fills before the social section does, so main text alone says
        # nothing about whether comments and the facepile are there yet.
        try:
            await self._session.page.wait_for_selector(
                _ENGAGEMENT_READY_SELECTOR, timeout=5000
            )
        except PlaywrightTimeoutError:
            logger.debug("No engagement rendered on %s", post_url)

        post = PostEngagement(post_url=post_url)

        raw_comments: list[dict[str, Any]] = (
            await self._session.run_on_linkedin(COMMENT_ROWS_JS) or []
        )
        for row in raw_comments[:max_people]:
            path = row.get("profile_path") or ""
            name = row.get("name") or ""
            if not path and not name:
                continue
            post.comments.append(
                Comment(
                    author=Person(name=name, profile_path=path),
                    body=row.get("body") or "",
                )
            )

        # The reactor list only exists behind the facepile; there is no URL
        # for it. Its absence is a post with nothing to show, not a failure.
        if not await self._session.page.locator(_REACTION_DETAILS_SELECTOR).count():
            post.dialog_missing = True
            return post

        await self._click_first(_REACTION_DETAILS_SELECTOR)
        if not await self._reactor_list_is_open(timeout=5000):
            post.dialog_missing = True
            return post

        collected: dict[str, Person] = {}

        async def read_rows() -> int:
            """Add what the list shows now; return how many were new."""
            snapshot: dict[str, Any] = (
                await self._session.run_on_linkedin(REACTOR_ROWS_JS) or {}
            )
            post.anonymous_reactors = max(
                post.anonymous_reactors, snapshot.get("anonymous") or 0
            )
            added = 0
            for row in snapshot.get("rows") or []:
                path = row.get("profile_path") or ""
                if path and path not in collected:
                    collected[path] = Person(
                        name=row.get("name") or "", profile_path=path
                    )
                    added += 1
            return added

        # Each scroll round appended about ten rows when measured, so the
        # rounds are sized from the cap with headroom. Rows are kept across
        # rounds rather than re-read at the end, so a list that unmounts what
        # scrolled away loses nobody. A page can land a round late, so it takes
        # two rounds in a row that add nobody to end the walk.
        idle = 0
        for round_index in range(max_people // 10 + 4):
            added = await read_rows()
            if len(collected) >= max_people:
                post.reactions_truncated = True
                break
            idle = 0 if added or not round_index else idle + 1
            if idle >= 2:
                break
            await self._session.run_on_linkedin(SCROLL_REACTIONS_JS)
            await self._session.delay(1.5)
        else:
            await read_rows()
            if len(collected) >= max_people:
                post.reactions_truncated = True

        post.reactors = list(collected.values())[:max_people]
        return post
