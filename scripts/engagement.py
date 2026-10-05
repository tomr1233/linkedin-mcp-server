"""Print the people who reacted to and commented on a post.

Starts the server over stdio and calls ``get_post_engagement`` like any MCP
client would, so the profile lock, session checks and re-login prompts all
behave exactly as they do for a real client.

Pass a post permalink to read that post, or a person's profile URL to read
their newest posts (one by default, ``--posts N`` for more). One person is one
row, however many posts they turn up on.

Run: uv run python scripts/engagement.py <url> [--posts N] [--max-people N]
                                               [--table] [--full]
"""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

REPO_ROOT = Path(__file__).resolve().parent.parent

_NAME_WIDTH = 26


def rows_from(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The schema this script prints, from the tool's references.

    The tool returns engagement rows as references so that an LLM client reads
    the same result as a readable section. This is the other half: the field
    names a person asked for, in the order they asked for them.
    """
    references = (payload.get("references") or {}).get("post_engagement") or []
    rows: list[dict[str, Any]] = []
    for reference in references:
        rows.append(
            {
                "Name": reference.get("text", ""),
                "Has Liked": bool(reference.get("has_liked")),
                "Has Commented": bool(reference.get("has_commented")),
                "LinkedIn profile": reference.get("url", ""),
                "Source Post URL": reference.get("source_post_urls", []),
                "Comments": reference.get("comments", []),
            }
        )
    return rows


def render(rows: list[dict[str, Any]], width: int, full: bool = False) -> str:
    """A readable table. The arrays collapse to a count and an indented list."""
    noun = "person" if len(rows) == 1 else "people"
    out = [f"Post engagement · {len(rows)} {noun}", ""]
    for row in rows:
        marks = "".join(
            (
                "L" if row["Has Liked"] else "·",
                "C" if row["Has Commented"] else "·",
            )
        )
        posts = len(row["Source Post URL"])
        head = f"{marks}  {row['Name']:<{_NAME_WIDTH}}  {row['LinkedIn profile']}"
        if posts > 1:
            head += f"  ({posts} posts)"
        out.append(head.rstrip()[:width])
        for comment in row["Comments"]:
            wrapped = textwrap.wrap(
                comment,
                width=max(20, width - 6),
                max_lines=None if full else 2,
                placeholder=" …",
            )
            out.extend("      " + line for line in wrapped)
        out.append("")
    out.append("L = reacted   C = commented")
    return "\n".join(out).rstrip() + "\n"


async def fetch_engagement(
    url: str, posts: int, max_people: int, log_path: Path
) -> dict[str, Any]:
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "linkedin_mcp_server"],
        cwd=str(REPO_ROOT),
        keep_alive=False,
        log_file=log_path,
    )
    async with Client(transport) as client:
        result = await client.call_tool(
            "get_post_engagement",
            {"url": url, "max_posts": posts, "max_people": max_people},
            timeout=600,
        )
        await client.call_tool("close_session", {}, raise_on_error=False)
    return result.structured_content or {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print who reacted to and commented on a LinkedIn post."
    )
    parser.add_argument("url", help="post permalink or a person's profile URL")
    parser.add_argument(
        "--posts",
        type=int,
        default=1,
        help="posts to read when the URL names a person (1-10, default 1)",
    )
    parser.add_argument(
        "--max-people",
        type=int,
        default=100,
        help="most reactors to read per post (1-500, default 100)",
    )
    parser.add_argument(
        "--table", action="store_true", help="print a readable table, not JSON"
    )
    parser.add_argument(
        "--full", action="store_true", help="show whole comments, not two lines"
    )
    args = parser.parse_args()
    if not 1 <= args.posts <= 10:
        parser.error("--posts must be between 1 and 10")
    if not 1 <= args.max_people <= 500:
        parser.error("--max-people must be between 1 and 500")

    log_path = Path(tempfile.gettempdir()) / "linkedin-engagement-server.log"
    try:
        payload = asyncio.run(
            fetch_engagement(args.url, args.posts, args.max_people, log_path)
        )
    except Exception as exc:
        print(f"Could not read engagement: {exc}", file=sys.stderr)
        print(f"Server log: {log_path}", file=sys.stderr)
        return 1

    rows = rows_from(payload)
    if not rows:
        # An empty result and a blocked one look the same in the rows alone.
        note = payload.get("sections", {}).get("post_engagement", "")
        print(
            "No engagement was read. " + (note.splitlines()[0] if note else ""),
            file=sys.stderr,
        )
        return 1

    if args.table:
        width = min(shutil.get_terminal_size((100, 24)).columns, 120)
        print(render(rows, width=width, full=args.full), end="")
    else:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
