from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .config import Settings

_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
}


def redact_query_string(query: str) -> str:
    if not query:
        return ""
    safe: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        lowered = key.lower().replace("-", "_")
        safe.append((key, "<redacted>" if lowered in _SENSITIVE_QUERY_KEYS else value))
    return urlencode(safe)


def query_contains_credentials(request: Request) -> bool:
    return any(
        key.lower().replace("-", "_") in _SENSITIVE_QUERY_KEYS
        for key in request.query_params.keys()
    )


def _is_agent_planning_path(path: str) -> bool:
    return path.startswith("/api/v2/documents/") and path.endswith(
        ("/agent/plan-v2", "/agent/plan-v2-stream", "/agent/replan")
    )


def _error(status_code: int, code: str, message: str, *, authenticate: bool = False) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if authenticate else None
    response = JSONResponse(
        status_code=status_code,
        content={"detail": {"error": code, "message": message, "retryable": False}},
        headers=headers,
    )
    apply_security_headers(response)
    return response


class RequestBoundary:
    def __init__(self, settings_or_app: Any, settings: Settings | None = None):
        if isinstance(settings_or_app, Settings):
            self.settings = settings_or_app
            self.app = None
        else:
            self.app = settings_or_app
            self.settings = settings or Settings()
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent_requests)

    async def __call__(
        self,
        request_or_scope: Request | dict[str, Any],
        call_next_or_receive: Any = None,
        send: Any = None,
    ) -> Any:
        if isinstance(request_or_scope, Request):
            return await self._handle_request(request_or_scope, call_next_or_receive)
        return await self._handle_asgi(request_or_scope, call_next_or_receive, send)

    async def _handle_request(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        protected = path.startswith("/api/")

        if protected and query_contains_credentials(request):
            return _error(
                400,
                "credentials_in_query",
                "Credentials must be sent in the Authorization header or JSON body, never in the URL.",
            )

        if protected and request.method != "OPTIONS" and self.settings.api_token:
            authorization = request.headers.get("Authorization", "")
            if not authorization:
                return _error(
                    401,
                    "authentication_required",
                    "This deployment requires an Authorization: Bearer token header.",
                    authenticate=True,
                )
            scheme, _, supplied = authorization.partition(" ")
            if scheme.lower() != "bearer" or not supplied:
                return _error(
                    401,
                    "invalid_authorization_header",
                    "Use the Authorization: Bearer <token> header.",
                    authenticate=True,
                )
            if not secrets.compare_digest(supplied, self.settings.api_token):
                return _error(403, "invalid_access_token", "The supplied service access token is invalid.")

        body_limit: int | None = None
        if protected and request.method in {"POST", "PUT", "PATCH"}:
            if path.startswith("/api/v2/imports/"):
                limit = self.settings.max_import_body_bytes
            elif _is_agent_planning_path(path):
                limit = self.settings.max_agent_body_bytes
            else:
                limit = self.settings.max_json_body_bytes
            body_limit = limit
            content_length = request.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > limit:
                        return _error(413, "request_body_too_large", f"Request body exceeds {limit} bytes.")
                except ValueError:
                    return _error(400, "invalid_content_length", "Content-Length must be an integer.")
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=0.01)
        except TimeoutError:
            return _error(
                429,
                "request_concurrency_exceeded",
                "The server is handling the configured maximum number of concurrent requests.",
            )
        try:
            if body_limit is not None:
                chunks: list[bytes] = []
                total = 0
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > body_limit:
                        return _error(
                            413,
                            "request_body_too_large",
                            f"Request body exceeds {body_limit} bytes.",
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
                request._body = body  # type: ignore[attr-defined]

                delivered = False

                async def receive() -> dict[str, object]:
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    await asyncio.Event().wait()
                    return {"type": "http.disconnect"}

                request._receive = receive  # type: ignore[attr-defined]
            response = await call_next(request)
        finally:
            self._semaphore.release()
        apply_security_headers(response)
        return response

    async def _handle_asgi(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        protected = path.startswith("/api/")

        query_string = scope.get("query_string", b"").decode("utf-8", errors="replace")
        if protected:
            for key, _ in parse_qsl(query_string, keep_blank_values=True):
                if key.lower().replace("-", "_") in _SENSITIVE_QUERY_KEYS:
                    res = _error(
                        400,
                        "credentials_in_query",
                        "Credentials must be sent in the Authorization header or JSON body, never in the URL.",
                    )
                    return await res(scope, receive, send)

            if self.settings.api_token and scope.get("method") != "OPTIONS":
                raw_headers = scope.get("headers", [])
                auth_val = ""
                for h_name, h_val in raw_headers:
                    if h_name.lower() == b"authorization":
                        auth_val = h_val.decode("utf-8", errors="replace")
                        break
                if not auth_val:
                    res = _error(
                        401,
                        "authentication_required",
                        "This deployment requires an Authorization: Bearer token header.",
                        authenticate=True,
                    )
                    return await res(scope, receive, send)
                scheme, _, supplied = auth_val.partition(" ")
                if scheme.lower() != "bearer" or not supplied:
                    res = _error(
                        401,
                        "invalid_authorization_header",
                        "Use the Authorization: Bearer <token> header.",
                        authenticate=True,
                    )
                    return await res(scope, receive, send)
                if not secrets.compare_digest(supplied, self.settings.api_token):
                    res = _error(
                        403,
                        "invalid_access_token",
                        "The supplied service access token is invalid.",
                    )
                    return await res(scope, receive, send)

        body_limit: int | None = None
        if protected and scope.get("method") in {"POST", "PUT", "PATCH"}:
            if path.startswith("/api/v2/imports/"):
                limit = self.settings.max_import_body_bytes
            elif _is_agent_planning_path(path):
                limit = self.settings.max_agent_body_bytes
            else:
                limit = self.settings.max_json_body_bytes
            body_limit = limit

            raw_headers = scope.get("headers", [])
            for h_name, h_val in raw_headers:
                if h_name.lower() == b"content-length":
                    try:
                        if int(h_val.decode("ascii")) > limit:
                            res = _error(413, "request_body_too_large", f"Request body exceeds {limit} bytes.")
                            return await res(scope, receive, send)
                    except ValueError:
                        res = _error(400, "invalid_content_length", "Content-Length must be an integer.")
                        return await res(scope, receive, send)

        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=0.01)
        except TimeoutError:
            res = _error(
                429,
                "request_concurrency_exceeded",
                "The server is handling the configured maximum number of concurrent requests.",
            )
            return await res(scope, receive, send)

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"referrer-policy", b"no-referrer"))
                headers.append((b"x-frame-options", b"DENY"))
                headers.append((
                    b"content-security-policy",
                    b"default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
                    b"script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                    b"font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:",
                ))
                message["headers"] = headers
            await send(message)

        try:
            replay_receive = receive
            if body_limit is not None:
                buffered_messages: list[dict[str, Any]] = []
                total = 0
                while True:
                    message = await receive()
                    buffered_messages.append(message)
                    if message.get("type") == "http.request":
                        chunk = message.get("body", b"")
                        total += len(chunk)
                        if total > body_limit:
                            res = _error(
                                413,
                                "request_body_too_large",
                                f"Request body exceeds {body_limit} bytes.",
                            )
                            return await res(scope, receive, send)
                        if not message.get("more_body", False):
                            break
                    elif message.get("type") == "http.disconnect":
                        break

                replay_index = 0

                async def replay_receive() -> dict[str, Any]:
                    nonlocal replay_index
                    if replay_index < len(buffered_messages):
                        message = buffered_messages[replay_index]
                        replay_index += 1
                        return message
                    return await receive()

            await self.app(scope, replay_receive, send_with_security_headers)
        finally:
            self._semaphore.release()


def apply_security_headers(response: Response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:",
    )
