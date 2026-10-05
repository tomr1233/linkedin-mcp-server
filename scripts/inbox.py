"""Print the LinkedIn messaging inbox.

Starts the server over stdio and calls ``get_inbox`` like any MCP client would,
so the profile lock, session checks and re-login prompts all behave exactly as
they do for a real client. Thread IDs are never requested: capturing them clicks
every row, and LinkedIn marks a clicked conversation as read.

Run: uv run python scripts/inbox.py [--limit N] [--raw]
"""

import argparse
import asyncio
import json
import re
import shutil
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

REPO_ROOT = Path(__file__).resolve().parent.parent

# The inbox text carries no URLs or attributes, only screen-reader copy, so
# parsing it is tied to the en-US strings the server's forced browser locale
# produces. Every row ends in an options line and repeats its date on two
# consecutive lines; both are the anchors here. Anything after the last
# options line is the thread LinkedIn opened on landing, not a row.
_ROW_END = "Open the options list in your conversation with"
_ROW_CHROME = re.compile(r"^\. (Active conversation|Press return to go to )")
_LABELS = ("Sponsored", "InMail")
_DATE_WIDTH = 7
_NAME_WIDTH = 24


@dataclass
class Conversation:
    name: str
    date: str
    snippet: str
    label: str = ""


def parse_inbox(text: str) -> list[Conversation]:
    lines = text.splitlines()
    entries: list[Conversation] = []
    block: list[str] = []
    for line in lines:
        if line.startswith(_ROW_END):
            entry = _parse_row(block)
            if entry is not None:
                entries.append(entry)
            block = []
        else:
            block.append(line)
    return entries


def _parse_row(block: list[str]) -> Conversation | None:
    for i in range(1, len(block) - 1):
        date = block[i].strip()
        if date and block[i + 1].strip() == date and block[i - 1].strip():
            name = block[i - 1].strip()
            body = [
                ln.strip()
                for ln in block[i + 2 :]
                if ln.strip() and not _ROW_CHROME.match(ln.strip())
            ]
            snippet = " ".join(" ".join(body).split())
            label = ""
            for candidate in _LABELS:
                if snippet.startswith(candidate + " "):
                    label, snippet = candidate, snippet[len(candidate) + 1 :]
                    break
            return Conversation(name=name, date=date, snippet=snippet, label=label)
    return None


def render(entries: list[Conversation], width: int, full: bool = False) -> str:
    noun = "conversation" if len(entries) == 1 else "conversations"
    out = [f"LinkedIn inbox · {len(entries)} {noun}", ""]
    indent = " " * (_DATE_WIDTH + 2)
    for entry in entries:
        head = f"{entry.date:<{_DATE_WIDTH}}  {entry.name:<{_NAME_WIDTH}}"
        if entry.label:
            head += f" [{entry.label}]"
        out.append(head.rstrip()[:width])
        wrapped = textwrap.wrap(
            entry.snippet,
            width=max(20, width - len(indent)),
            max_lines=None if full else 2,
            placeholder=" …",
        )
        out.extend(indent + ln for ln in wrapped)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


async def fetch_inbox(limit: int, log_path: Path) -> dict:
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "linkedin_mcp_server"],
        cwd=str(REPO_ROOT),
        keep_alive=False,
        log_file=log_path,
    )
    async with Client(transport) as client:
        result = await client.call_tool(
            "get_inbox",
            {"limit": limit, "include_thread_ids": False},
            timeout=300,
        )
        await client.call_tool("close_session", {}, raise_on_error=False)
    return result.structured_content or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Print the LinkedIn messaging inbox.")
    parser.add_argument("--limit", type=int, default=20, help="1-50 (default 20)")
    parser.add_argument(
        "--full", action="store_true", help="show whole previews, not two lines"
    )
    parser.add_argument(
        "--raw", action="store_true", help="print the tool's JSON unformatted"
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 50:
        parser.error("--limit must be between 1 and 50")

    log_path = Path(tempfile.gettempdir()) / "linkedin-inbox-server.log"
    try:
        payload = asyncio.run(fetch_inbox(args.limit, log_path))
    except Exception as exc:
        print(f"Could not fetch the inbox: {exc}", file=sys.stderr)
        print(f"Server log: {log_path}", file=sys.stderr)
        return 1

    if args.raw:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    text = payload.get("sections", {}).get("inbox", "")
    entries = parse_inbox(text)
    if text and not entries:
        print(
            "The inbox loaded but no conversations could be read from it. "
            "Run with --raw to see what LinkedIn returned.",
            file=sys.stderr,
        )
        return 1
    width = min(shutil.get_terminal_size((100, 24)).columns, 120)
    print(render(entries, width=width, full=args.full), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
