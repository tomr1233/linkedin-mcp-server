"""
LinkedIn post/content search tool.

Performs LinkedIn's global content search (the "Posts" results tab) using
innerText extraction, so informal "we're hiring" / "Buscamos ..." posts can
be found before a formal job listing is published. Mirrors search_people:
build a /search/results/content/ URL, scroll to load results, and return the
raw innerText for the LLM to parse, plus post-permalink references.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.linkedin.contracts import FilterValidationError

logger = logging.getLogger(__name__)


def register_post_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register post/content-search tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "search"},
    )
    async def search_posts(
        keywords: str,
        ctx: Context,
        date_posted: str | None = None,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
    ) -> dict[str, Any]:
        """
        Search LinkedIn posts/content globally by keyword (the "Posts" tab).

        Use this to catch informal hiring posts ("we're hiring", "Buscamos
        ...", "estamos contratando", "join our team") that often appear before
        a formal job listing exists. This is global content search, distinct
        from get_feed (your own home feed) and get_company_posts (one
        company's page).

        Args:
            keywords: Search keywords (e.g., "Buscamos Unity", "AI automation hiring")
            ctx: FastMCP context for progress reporting
            date_posted: Optional recency filter. One of "past-24h",
                "past-week", "past-month"; the "past_24_hours" / "past_week" /
                "past_month" spellings used by search_jobs are accepted too.
                Omit for any time.
            max_pages: Scroll depth as result "pages" of ~5 scrolls each
                (1-10, default 3). Content search is an infinite scroll, so
                this caps how far the page is scrolled rather than fetching
                discrete pages.

        Returns:
            Dict with url, sections (search_results -> raw text), and optional
            references (post authors, companies, linked jobs, and kind
            "feed_post" permalinks read from the page's payload responses —
            /feed/update/<urn>/ or /posts/<slug>, both valid permalinks) and
            section_errors. The DOM carries no per-post permalink anchors;
            captured permalinks are not aligned to result order. The LLM
            should parse the raw text to extract each post's author,
            headline/role, company, body, posted date, and reaction/comment
            counts.
        """
        try:
            extractor = await get_ready_extractor(ctx, tool_name="search_posts")
            logger.info(
                "Searching posts: keywords='%s', date_posted='%s', max_pages=%d",
                keywords,
                date_posted,
                max_pages,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting post search"
            )

            try:
                result = await extractor.search_posts(
                    keywords,
                    date_posted=date_posted,
                    max_pages=max_pages,
                )
            except FilterValidationError as e:
                # Validation messages carry actionable detail; surface them as
                # ToolError so mask_error_details doesn't reduce them to a
                # generic "Error calling tool 'search_posts'".
                raise ToolError(str(e)) from e

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except ToolError:
            # Already a properly formatted client-facing error; do not log it
            # as "Unexpected error" via raise_tool_error.
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_posts")
        except Exception as e:
            raise_tool_error(e, "search_posts")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Post Engagement",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "engagement", "scraping"},
    )
    async def get_post_engagement(
        url: str,
        ctx: Context,
        max_posts: Annotated[int, Field(ge=1, le=10)] = 1,
        max_people: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        """
        List the people who reacted to and commented on a post.

        Accepts a post permalink (/feed/update/<urn>/ or /posts/<slug>) or a
        person's profile URL, in which case their newest `max_posts` posts are
        read. A company page is not accepted.

        Args:
            url: Post permalink, activity urn, or a person's /in/ profile URL
            ctx: FastMCP context for progress reporting
            max_posts: How many of a person's newest posts to read, when `url`
                names a person (1-10, default 1). Ignored for a post URL. Each
                post costs its own page view, so raise it deliberately.
            max_people: Most reactors to read per post (1-500, default 100).
                The result says when a list stopped at this cap.

        Returns:
            Dict with url, sections (post_engagement -> readable text) and
            references (post_engagement -> one entry per person, carrying
            has_liked, has_commented, source_post_urls, comments and
            identified_by).

            One person appears once, even across several posts. Reactors and
            commenters are merged on their display name, because LinkedIn
            names reactors by opaque member id and commenters by vanity slug
            with nothing on the page bridging them; `identified_by` is
            "display_name" on a row that was merged that way and "profile_url"
            on one that was not. Two people sharing a name would merge, so
            treat a "display_name" row as a claim rather than a fact.
        """
        try:
            extractor = await get_ready_extractor(ctx, tool_name="get_post_engagement")
            logger.info(
                "Reading post engagement: url='%s', max_posts=%d, max_people=%d",
                url,
                max_posts,
                max_people,
            )

            await ctx.report_progress(progress=0, total=100, message="Opening post")

            result = await extractor.get_post_engagement(
                url,
                max_posts=max_posts,
                max_people=max_people,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_post_engagement")
        except Exception as e:
            raise_tool_error(e, "get_post_engagement")  # NoReturn
