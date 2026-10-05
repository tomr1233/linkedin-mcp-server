"""Tests for the REST route over ``send_message``.

The route's whole job is translation, so these assert the two things a campaign
runner depends on: that a tool result becomes the right HTTP status, and that
the call still goes through the middleware chain that serializes browser
access. A handler that reached for the extractor directly would pass every
status assertion here and still be wrong, which is why the middleware probe is
a test and not a comment.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

from linkedin_mcp_server.core.exceptions import AuthenticationError, RateLimitError
from linkedin_mcp_server.exceptions import BrowserBusyError, SessionExpiredError
from linkedin_mcp_server.rest_api import SEND_MESSAGE_PATH, register_rest_routes
from linkedin_mcp_server.tools.messaging import register_messaging_tools


class _ProbeMiddleware(Middleware):
    """Records tool calls that pass through the middleware chain."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        self.calls.append(context.message.name)
        return await call_next(context)


def _sent_result(status: str, **overrides: Any) -> dict[str, Any]:
    result = {
        "url": "https://www.linkedin.com/messaging/compose/",
        "status": status,
        "message": "…",
        "recipient_selected": True,
        "sent": status == "sent",
    }
    result.update(overrides)
    return result


def _build_server() -> tuple[FastMCP, _ProbeMiddleware]:
    mcp = FastMCP("test-rest")
    probe = _ProbeMiddleware()
    mcp.add_middleware(probe)
    register_messaging_tools(mcp)
    register_rest_routes(mcp)
    return mcp, probe


async def _post(
    mcp: FastMCP,
    body: Any,
    *,
    headers: dict[str, str] | None = None,
    host_origin_protection: bool = False,
) -> httpx2.Response:
    app = mcp.http_app(host_origin_protection=host_origin_protection or None)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as client:
            return await client.post(SEND_MESSAGE_PATH, json=body, headers=headers)


def _valid_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "linkedin_username": "someone",
        "message": "Hello there",
        "confirm_send": True,
    }
    body.update(overrides)
    return body


def _patched_extractor(result: Any = None, side_effect: Any = None) -> Any:
    extractor = AsyncMock()
    extractor.send_message = AsyncMock(return_value=result, side_effect=side_effect)
    return patch(
        "linkedin_mcp_server.tools.messaging.get_ready_extractor",
        AsyncMock(return_value=extractor),
    )


async def test_successful_send_returns_200_and_runs_middleware() -> None:
    mcp, probe = _build_server()
    with _patched_extractor(_sent_result("sent")):
        response = await _post(mcp, _valid_body())

    assert response.status_code == 200
    assert response.json()["sent"] is True
    # The load-bearing assertion: the route dispatched through the middleware
    # chain rather than around it.
    assert probe.calls == ["send_message"]


async def test_arguments_reach_the_tool_unchanged() -> None:
    mcp, _ = _build_server()
    extractor = AsyncMock()
    extractor.send_message = AsyncMock(return_value=_sent_result("sent"))
    with patch(
        "linkedin_mcp_server.tools.messaging.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        await _post(mcp, _valid_body(profile_urn="ACoAAB123"))

    extractor.send_message.assert_awaited_once()
    await_args = extractor.send_message.await_args
    assert await_args is not None
    args, kwargs = await_args
    assert args[0] == "someone"
    assert args[1] == "Hello there"
    assert kwargs["confirm_send"] is True
    assert kwargs["profile_urn"] == "ACoAAB123"


@pytest.mark.parametrize(
    ("status", "expected_code", "expected_retry_safe"),
    [
        ("confirmation_required", 400, True),
        ("message_unavailable", 422, True),
        ("recipient_resolution_failed", 422, True),
        ("composer_unavailable", 502, True),
        ("compose_interact_failed", 502, True),
        # The one status reached after the send button is clicked.
        ("send_unavailable", 502, False),
        # An unrecognised status must not be guessed as safe.
        ("something_new", 502, False),
    ],
)
async def test_tool_status_maps_to_http_status_and_retry_safety(
    status: str, expected_code: int, expected_retry_safe: bool
) -> None:
    mcp, _ = _build_server()
    with _patched_extractor(_sent_result(status)):
        response = await _post(mcp, _valid_body())

    assert response.status_code == expected_code
    assert response.json()["retry_safe"] is expected_retry_safe


async def test_send_unavailable_is_the_only_unsafe_failure() -> None:
    """Guards the distinction the caller's retry logic is built on."""
    mcp, _ = _build_server()
    unsafe = []
    for status in (
        "confirmation_required",
        "message_unavailable",
        "recipient_resolution_failed",
        "composer_unavailable",
        "compose_interact_failed",
        "send_unavailable",
    ):
        with _patched_extractor(_sent_result(status)):
            response = await _post(mcp, _valid_body())
        if response.json()["retry_safe"] is False:
            unsafe.append(status)

    assert unsafe == ["send_unavailable"]


@pytest.mark.parametrize(
    ("exception", "expected_code", "expected_error"),
    [
        (BrowserBusyError(), 409, "browser_busy"),
        (RateLimitError("limited"), 429, "rate_limited"),
    ],
)
async def test_raised_failures_map_to_status_codes(
    exception: Exception, expected_code: int, expected_error: str
) -> None:
    mcp, _ = _build_server()
    with _patched_extractor(side_effect=exception):
        response = await _post(mcp, _valid_body())

    assert response.status_code == expected_code
    body = response.json()
    assert body["error"] == expected_error
    # Nothing was transmitted in any of these, so all are safe to retry.
    assert body["retry_safe"] is True


async def test_expired_session_maps_to_503() -> None:
    """An auth failure runs the tool's re-login path first.

    ``send_message`` catches ``AuthenticationError`` and hands it to
    ``handle_auth_error``, which tries to recover the session. Only when that
    recovery itself fails does the caller see an error, so the exception the
    route classifies is the one recovery raised -- not the original.
    """
    mcp, _ = _build_server()
    with (
        _patched_extractor(side_effect=AuthenticationError("expired")),
        patch(
            "linkedin_mcp_server.tools.messaging.handle_auth_error",
            AsyncMock(side_effect=SessionExpiredError("session expired")),
        ),
    ):
        response = await _post(mcp, _valid_body())

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "authentication_required"
    assert body["retry_safe"] is True


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"message": "hi", "confirm_send": True}, "missing username"),
        ({"linkedin_username": "someone", "confirm_send": True}, "missing message"),
        (_valid_body(linkedin_username="   "), "blank username"),
        (
            {"linkedin_username": "someone", "message": "hi"},
            "confirm_send must be explicit",
        ),
        (_valid_body(confirm_send="true"), "confirm_send must be a bool"),
        (_valid_body(profile_urn=""), "blank profile_urn"),
        ({"profileUrn": "ACoAAB123", **_valid_body()}, "camelCase typo is refused"),
        ([1, 2, 3], "body is not an object"),
    ],
)
async def test_malformed_requests_are_refused_without_touching_the_browser(
    body: Any, reason: str
) -> None:
    mcp, probe = _build_server()
    extractor = AsyncMock()
    extractor.send_message = AsyncMock(return_value=_sent_result("sent"))
    with patch(
        "linkedin_mcp_server.tools.messaging.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        response = await _post(mcp, body)

    assert response.status_code == 400, reason
    assert response.json()["error"] == "invalid_request"
    # The point of validating early: a bad request costs no navigation.
    extractor.send_message.assert_not_awaited()
    assert probe.calls == []


async def test_confirm_send_false_is_accepted_as_a_dry_run() -> None:
    """False is a legitimate value; only absence is refused."""
    mcp, _ = _build_server()
    extractor = AsyncMock()
    extractor.send_message = AsyncMock(
        return_value=_sent_result("confirmation_required", sent=False)
    )
    with patch(
        "linkedin_mcp_server.tools.messaging.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        response = await _post(mcp, _valid_body(confirm_send=False))

    assert response.status_code == 400
    await_args = extractor.send_message.await_args
    assert await_args is not None
    assert await_args.kwargs["confirm_send"] is False


async def test_route_is_reachable_with_host_origin_protection_on() -> None:
    """The guard the HTTP transport enables must not shut out the REST caller."""
    mcp, _ = _build_server()
    with _patched_extractor(_sent_result("sent")):
        response = await _post(mcp, _valid_body(), host_origin_protection=True)

    assert response.status_code == 200


async def test_foreign_host_is_refused_by_the_guard() -> None:
    mcp, _ = _build_server()
    with _patched_extractor(_sent_result("sent")):
        response = await _post(
            mcp,
            _valid_body(),
            headers={"Host": "attacker.example"},
            host_origin_protection=True,
        )

    assert response.status_code != 200


async def test_route_is_absent_unless_registered() -> None:
    """A stdio server never calls register_rest_routes, so it has no endpoint."""
    mcp = FastMCP("test-no-rest")
    register_messaging_tools(mcp)

    with _patched_extractor(_sent_result("sent")):
        response = await _post(mcp, _valid_body())

    assert response.status_code == 404
