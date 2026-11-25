"""Shared auth helpers for the test suite (E-24).

Since Phase 5c the browser-flow auth responses (POST
/auth/register, POST /auth/login) no longer carry the session
token in their bodies — browsers receive it exclusively as the
HttpOnly `cinemora_session` Set-Cookie. Tests that need a Bearer
token therefore extract it from the Set-Cookie header, exactly
what a browser (or the documented POST /auth/token exchange)
does, instead of reading a body field that no longer exists.
"""

from __future__ import annotations

from services.auth import SESSION_COOKIE


def session_token_from_response(response) -> str:
    """The session token a response delivered via Set-Cookie."""
    token = response.cookies.get(SESSION_COOKIE)
    if token:
        return token
    # Fallback: parse the raw Set-Cookie header (some test clients
    # surface cookies only there).
    header = response.headers.get("set-cookie", "")
    for part in header.split(";"):
        part = part.strip()
        if part.startswith(f"{SESSION_COOKIE}="):
            return part.split("=", 1)[1]
    raise AssertionError(
        f"response set no {SESSION_COOKIE} cookie: {response.status_code}"
    )


def with_session_token(response) -> dict:
    """The response JSON body plus the session_token the response
    delivered via Set-Cookie — the shape the suite's register
    helpers have always returned, now sourced from the cookie."""
    body = dict(response.json())
    body["session_token"] = session_token_from_response(response)
    return body
