"""TypeSafe System One as the agent's semantic judge: judgments instead of generated text.

The agent's model path asks an LLM to *write* a transaction and then validates what it wrote. This
module adds the other shape: code owns the workflow and the candidate set, and TypeSafe answers
narrow judgments about them -- which catalogue symbol a phrase names, which of two placed devices a
connection clause means, whether a clause asks for flow direction. A judgment is a number and a
label, so it can be thresholded, logged and refused, which is what makes it usable inside a drawing
pipeline where an invented element is worse than a rejected request.

Credentials follow the project's existing provider rule: a request may carry them, the environment
is the fallback (``TYPESAFE_API_KEY`` / ``TYPESAFE_BASE_URL`` / ``TYPESAFE_MODEL``), and nothing
here ever writes a key into a log, an error or a response body. :meth:`TypesafeConfig.describe`
exists so call sites have a safe thing to publish.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx

TYPESAFE_DEFAULT_BASE_URL = "https://api.typesafe.ai"
TYPESAFE_DEFAULT_MODEL = "jev-latest"
TYPESAFE_SYSTEM_ONE_PATH = "/v1/systemone"
TYPESAFE_ENV_API_KEY = "TYPESAFE_API_KEY"
TYPESAFE_ENV_BASE_URL = "TYPESAFE_BASE_URL"
TYPESAFE_ENV_MODEL = "TYPESAFE_MODEL"

#: A judgment below this is reported as uncertain rather than acted on. System One is calibrated,
#: so the number is information: refusing to draw is the honest outcome when nothing is close.
DEFAULT_CONFIDENCE_FLOOR = 0.34


class TypesafeError(RuntimeError):
    """A TypeSafe call that produced no usable judgment. Never carries the key."""

    def __init__(self, code: str, message: str, *, status_code: int = 502) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class TypesafeConfig:
    """Where to call TypeSafe and with what. Immutable so no call site can mutate the key away."""

    api_key: str
    base_url: str = TYPESAFE_DEFAULT_BASE_URL
    model: str = TYPESAFE_DEFAULT_MODEL
    timeout_seconds: float = 60.0
    source: str = "request"

    def describe(self) -> dict[str, Any]:
        """The shape a log line or an API response may publish: presence, never the value."""

        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key_present": bool(self.api_key),
            "api_key_source": self.source,
            "timeout_seconds": self.timeout_seconds,
        }


def resolve_typesafe_config(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout_seconds: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> TypesafeConfig:
    """The request's values first, the environment second, and an explicit error when neither has one.

    Silent fallback to a default key would be worse than failing: the caller would get judgments
    from an account it did not choose.
    """

    env = os.environ if environ is None else environ
    key = (api_key or "").strip() or env.get(TYPESAFE_ENV_API_KEY, "").strip()
    if not key:
        raise TypesafeError(
            "typesafe_key_missing",
            (
                "TypeSafe needs an API key: configure it in the agent panel, or set "
                f"{TYPESAFE_ENV_API_KEY} for the server"
            ),
            status_code=400,
        )
    source = "request" if (api_key or "").strip() else "environment"
    return TypesafeConfig(
        api_key=key,
        base_url=(base_url or "").strip() or env.get(TYPESAFE_ENV_BASE_URL, "").strip()
        or TYPESAFE_DEFAULT_BASE_URL,
        model=(model or "").strip() or env.get(TYPESAFE_ENV_MODEL, "").strip()
        or TYPESAFE_DEFAULT_MODEL,
        timeout_seconds=timeout_seconds or 60.0,
        source=source,
    )


def typesafe_status(
    *, api_key: str | None = None, base_url: str | None = None, model: str | None = None
) -> dict[str, Any]:
    """Whether a call *would* be possible, without making one and without touching the key."""

    try:
        config = resolve_typesafe_config(api_key=api_key, base_url=base_url, model=model)
    except TypesafeError as error:
        return {
            "configured": False,
            "api_key_present": False,
            "error_code": error.code,
            "base_url": (base_url or "").strip() or TYPESAFE_DEFAULT_BASE_URL,
            "model": (model or "").strip() or TYPESAFE_DEFAULT_MODEL,
        }
    return {"configured": True, "error_code": None, **config.describe()}


#: The transport seam. A call site may pass its own so tests never open a socket.
Transport = Callable[[TypesafeConfig, dict[str, Any]], dict[str, Any]]


def _http_transport(config: TypesafeConfig, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{config.base_url.rstrip('/')}{TYPESAFE_SYSTEM_ONE_PATH}"
    try:
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=config.timeout_seconds,
        )
    except httpx.TimeoutException as error:
        raise TypesafeError(
            "typesafe_timeout", f"TypeSafe did not answer within {config.timeout_seconds:g}s"
        ) from error
    except httpx.HTTPError as error:
        raise TypesafeError("typesafe_unreachable", f"could not reach {url}: {error}") from error
    if response.status_code != 200:
        # The provider's body can quote the request, so only the status is repeated.
        raise TypesafeError(
            "typesafe_rejected",
            f"TypeSafe answered HTTP {response.status_code} for {url}",
            status_code=502 if response.status_code >= 500 else 400,
        )
    try:
        return response.json()
    except ValueError as error:  # pragma: no cover - a provider answering non-JSON
        raise TypesafeError("typesafe_response_invalid", "TypeSafe answered non-JSON") from error


class TypesafeClient:
    """One System One call per judgment batch. Small on purpose: state in, answers out."""

    def __init__(
        self,
        config: TypesafeConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or _http_transport

    def judge(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> dict[str, Any]:
        """Ask every question at once and return ``{question_id: answer}`` -- never partial.

        Independent questions belong in one request: they run in parallel, they cost one round trip,
        and their answers cannot influence each other, which is what keeps a selection honest.
        """

        if not questions:
            raise TypesafeError("typesafe_no_questions", "a judgment batch needs questions", status_code=400)
        payload = {"state": dict(state), "model": self.config.model, "questions": dict(questions)}
        started = perf_counter()
        raw = self.transport(self.config, payload)
        elapsed_ms = round((perf_counter() - started) * 1000, 2)
        answers = raw.get("answers") if isinstance(raw, Mapping) else None
        if not isinstance(answers, Mapping):
            raise TypesafeError("typesafe_response_invalid", "TypeSafe answered without 'answers'")
        missing = sorted(set(questions) - set(answers))
        if missing:
            raise TypesafeError(
                "typesafe_judgment_missing",
                "TypeSafe answered without judging: " + ", ".join(missing),
            )
        return {
            "answers": {str(key): dict(value) for key, value in answers.items()},
            "model": raw.get("model", self.config.model),
            "usage": dict(raw.get("usage") or {}),
            "latency_ms": elapsed_ms,
            "config": self.config.describe(),
        }

    def verify(self) -> dict[str, Any]:
        """Prove the key works by asking one judgment with an answer nobody has to interpret.

        A status endpoint that only reports "a key is configured" cannot tell a typo from a valid
        key, and a drawing pipeline that discovers that mid-request fails in the worst place.
        """

        result = self.judge(
            state={"probe": {"label": "PT-101", "kind": "pressure transmitter"}},
            questions={
                "reachable": {
                    "type": "noul",
                    "instructions": {
                        "question": "Does `probe` describe a measuring instrument?",
                        "target": "probe",
                    },
                }
            },
        )
        answer = result["answers"]["reachable"]
        return {
            "ok": True,
            "model": result["model"],
            "latency_ms": result["latency_ms"],
            "usage": result["usage"],
            "answer": answer,
        }


def choice_probabilities(answer: Mapping[str, Any]) -> dict[str, float]:
    """The distribution behind a Choice answer, as plain floats (empty when the model gave none)."""

    raw = answer.get("probabilities")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): float(value) for key, value in raw.items() if isinstance(value, (int, float))}


__all__ = [
    "DEFAULT_CONFIDENCE_FLOOR",
    "TYPESAFE_DEFAULT_BASE_URL",
    "TYPESAFE_DEFAULT_MODEL",
    "TYPESAFE_ENV_API_KEY",
    "TYPESAFE_ENV_BASE_URL",
    "TYPESAFE_ENV_MODEL",
    "Transport",
    "TypesafeClient",
    "TypesafeConfig",
    "TypesafeError",
    "choice_probabilities",
    "resolve_typesafe_config",
    "typesafe_status",
]
