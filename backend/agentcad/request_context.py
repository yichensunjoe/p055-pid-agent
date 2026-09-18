"""Per-request correlation context for audit attribution.

The ASGI middleware owns the request id; routers and services read it here so one
audit record can be correlated with the HTTP request that produced it without
threading an extra parameter through every call site.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    request_id: str = ""
    method: str = ""
    path: str = ""
    client: str = ""

    def as_metadata(self) -> dict[str, str]:
        payload = {"method": self.method, "path": self.path}
        if self.client:
            payload["client"] = self.client
        return {key: value for key, value in payload.items() if value}


_request_context: ContextVar[RequestContext | None] = ContextVar(
    "pid_agent_request_context", default=None
)


def bind_request_context(context: RequestContext):
    return _request_context.set(context)


def reset_request_context(token) -> None:
    _request_context.reset(token)


def current_request_context() -> RequestContext | None:
    return _request_context.get()


def current_request_id() -> str:
    context = _request_context.get()
    return context.request_id if context else ""


__all__ = [
    "RequestContext",
    "bind_request_context",
    "current_request_context",
    "current_request_id",
    "reset_request_context",
]
