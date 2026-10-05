"""A REST route over ``send_message``, for callers that are not MCP clients.

An application that already owns campaign logic does not want to speak JSON-RPC
to send one message. This exposes the same tool as a plain POST, and nothing
more: the route is a translation layer, not a second implementation.

Two things it does *not* do, both deliberate:

* **No pacing, no retry.** The rate rules in ``docs/operations-playbook.md`` are
  the caller's. A retry here would turn one ambiguous send into two messages,
  which is the one failure this endpoint is shaped to let the caller avoid --
  see ``_RETRY_SAFE`` below.
* **No authentication.** Registered only on the HTTP transport, which the config
  layer already expects to be loopback-bound (``config/schema.py``). Same-host
  is what makes that acceptable; the MCP endpoint alongside it is equally open.

Every call goes through ``FastMCP.call_tool(..., run_middleware=True)`` rather
than reaching for the extractor directly. That is the whole design: the
serializing middleware is what keeps two concurrent requests from driving one
Chromium, and a handler that bypassed it would be a correctness bug that only
shows up under the concurrency a campaign runner produces.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    RateLimitError,
)
from linkedin_mcp_server.exceptions import (
    BrowserBusyError,
    OwnerCannotAuthenticateError,
    SessionExpiredError,
)

logger = logging.getLogger(__name__)

SEND_MESSAGE_PATH = "/v1/send-message"

#: Accepted request keys. Unknown keys are refused rather than ignored: the one
#: that matters is ``profile_urn``, and a caller that sends ``profileUrn`` would
#: otherwise get a silent fallback to the slower Message-button lookup.
_ALLOWED_KEYS = frozenset(
    {"linkedin_username", "message", "confirm_send", "profile_urn"}
)

#: Tool ``status`` -> HTTP status. The tool reports failure in its result body
#: rather than by raising, so this is where a REST caller's error handling comes
#: from.
_STATUS_CODES: dict[str, int] = {
    "sent": 200,
    # Nothing was attempted: the caller did not confirm.
    "confirmation_required": 400,
    # The target is wrong or unreachable -- caller's input, not a server fault.
    "message_unavailable": 422,
    "recipient_resolution_failed": 422,
    # LinkedIn did not give us what the flow needs. Upstream, not caller.
    "composer_unavailable": 502,
    "compose_interact_failed": 502,
    "send_unavailable": 502,
}

#: Whether the caller may safely retry. ``send_unavailable`` is the one status
#: reached *after* the send button is clicked, so LinkedIn may have delivered
#: the message without confirming it. Retrying that is how a campaign
#: double-sends; every other failure stops before anything is transmitted.
_RETRY_SAFE: dict[str, bool] = {
    "confirmation_required": True,
    "message_unavailable": True,
    "recipient_resolution_failed": True,
    "composer_unavailable": True,
    "compose_interact_failed": True,
    "send_unavailable": False,
}

_UNKNOWN_STATUS_CODE = 502


def _error(
    code: str, message: str, *, http_status: int, retry_safe: bool
) -> JSONResponse:
    return JSONResponse(
        {"error": code, "message": message, "retry_safe": retry_safe},
        status_code=http_status,
    )


def _causes(exception: BaseException) -> list[BaseException]:
    """The exception and everything it was raised from.

    ``raise_tool_error`` wraps a domain exception in a ``ToolError`` but keeps
    the chain, so the type that explains a failure is usually the cause rather
    than what was caught.
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exception
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def _response_for_exception(exception: Exception) -> JSONResponse:
    """Map a raised tool failure onto an HTTP response.

    Every branch here is retry-safe: a raise means the send never reached
    LinkedIn's compose flow, or failed before the send button.
    """
    chain = _causes(exception)

    def raised(*types: type[BaseException]) -> bool:
        return any(isinstance(item, types) for item in chain)

    if raised(BrowserBusyError):
        return _error(
            "browser_busy",
            "Another client holds the shared browser. Nothing was sent.",
            http_status=409,
            retry_safe=True,
        )
    if raised(RateLimitError):
        return _error(
            "rate_limited",
            "LinkedIn rate-limited this session. Nothing was sent.",
            http_status=429,
            retry_safe=True,
        )
    if raised(
        AuthenticationError,
        SessionExpiredError,
        OwnerCannotAuthenticateError,
    ):
        return _error(
            "authentication_required",
            "The LinkedIn session is not usable. Run --login on this host.",
            http_status=503,
            retry_safe=True,
        )

    logger.exception("Unclassified failure in %s", SEND_MESSAGE_PATH)
    return _error(
        "send_failed",
        str(exception) or "The send failed.",
        http_status=500,
        retry_safe=True,
    )


def _validation_error(payload: Any) -> JSONResponse | None:
    """Refuse a malformed body before it reaches the browser."""
    if not isinstance(payload, dict):
        return _error(
            "invalid_request",
            "Body must be a JSON object.",
            http_status=400,
            retry_safe=True,
        )

    unknown = sorted(set(payload) - _ALLOWED_KEYS)
    if unknown:
        return _error(
            "invalid_request",
            f"Unknown field(s): {', '.join(unknown)}. "
            f"Accepted: {', '.join(sorted(_ALLOWED_KEYS))}.",
            http_status=400,
            retry_safe=True,
        )

    for key in ("linkedin_username", "message"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            return _error(
                "invalid_request",
                f"'{key}' is required and must be a non-empty string.",
                http_status=400,
                retry_safe=True,
            )

    # Required rather than defaulted, in either direction. The playbook's rule
    # is that a wrapper must not default this to True; defaulting it to False
    # would be quieter but just as wrong, because a caller that meant to send
    # would get a silent dry run reported as a failure.
    if not isinstance(payload.get("confirm_send"), bool):
        return _error(
            "invalid_request",
            "'confirm_send' is required and must be true or false.",
            http_status=400,
            retry_safe=True,
        )

    profile_urn = payload.get("profile_urn")
    if profile_urn is not None and (
        not isinstance(profile_urn, str) or not profile_urn.strip()
    ):
        return _error(
            "invalid_request",
            "'profile_urn', when given, must be a non-empty string.",
            http_status=400,
            retry_safe=True,
        )

    return None


def register_rest_routes(mcp: FastMCP) -> None:
    """Attach the REST routes to *mcp*. HTTP transport only.

    Called from the transport branch rather than from ``create_mcp_server`` so
    that a stdio server -- where there is no port and no REST caller -- does not
    carry a route it cannot serve.
    """

    @mcp.custom_route(SEND_MESSAGE_PATH, methods=["POST"], name="send_message_rest")
    async def send_message_route(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return _error(
                "invalid_request",
                "Body must be valid JSON.",
                http_status=400,
                retry_safe=True,
            )

        invalid = _validation_error(payload)
        if invalid is not None:
            return invalid

        arguments = {
            "linkedin_username": payload["linkedin_username"],
            "message": payload["message"],
            "confirm_send": payload["confirm_send"],
            "profile_urn": payload.get("profile_urn"),
        }

        logger.info(
            "REST send_message for %s (confirm_send=%s)",
            arguments["linkedin_username"],
            arguments["confirm_send"],
        )

        try:
            # run_middleware=True is load-bearing: it is what puts this call
            # behind the same lock and profile lease as an MCP tool call.
            result = await mcp.call_tool("send_message", arguments, run_middleware=True)
        except ToolError as exc:
            return _response_for_exception(exc)
        except Exception as exc:  # noqa: BLE001 - mapped to a 5xx below
            return _response_for_exception(exc)

        body = result.structured_content
        if not isinstance(body, dict):
            logger.error("send_message returned no structured content")
            return _error(
                "send_failed",
                "The send produced no structured result.",
                http_status=_UNKNOWN_STATUS_CODE,
                retry_safe=False,
            )

        status = body.get("status")
        http_status = _STATUS_CODES.get(str(status), _UNKNOWN_STATUS_CODE)
        response: dict[str, Any] = dict(body)
        if http_status != 200:
            # An unrecognised status is treated as unsafe to retry. A new
            # failure mode added to the tool is more likely to sit late in the
            # flow than early, and guessing "safe" is the guess that duplicates
            # a message.
            response["retry_safe"] = _RETRY_SAFE.get(str(status), False)
        return JSONResponse(response, status_code=http_status)
