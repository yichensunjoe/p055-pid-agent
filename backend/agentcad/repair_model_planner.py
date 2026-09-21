"""Track M: the same repair contract, planned by a real model (baseline §A4, §M5-4).

The deterministic planner is the CI gate; a real model is release evidence. What makes the two
comparable is that they are **not two implementations of the feature**: this module returns the
same :class:`RepairPlanDraft`, through the same compile → shadow → oracle → governed-apply path,
under the same budgets and the same scope. The only difference is who writes the operation list.

Four rules shape the prompt, and each exists because breaking it would turn the qualification
number into something else:

* **One finding, named by identity.** The model is told which canonical finding to repair, with
  its validator, code and locators, not a paragraph describing the problem.
* **A frozen scope.** The scope's element ids are given as the *only* ids that may be written.
  A model that wants to reach further has to say so, and the oracle will refuse it — which is
  the intended behaviour, not a prompt-engineering problem.
* **No answer key.** The context contains the drawing as it *is* and the structured assessment of
  the model's own previous attempts. It never contains the mutation that caused the defect, the
  expected operation list, or any oracle verdict.
* **Structured replan.** A rejected attempt comes back as its failure code, compile issues and
  notes, so the next attempt is a response to a stated problem rather than a re-roll.

The prompt and schema fingerprints are recorded in the run identity, so "the model changed" and
"the question changed" are separable after the fact. Secrets are never recorded: only the
provider *class*, the model id, the sampling parameters and the fingerprints.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import TypeAdapter, ValidationError

from .agent_semantic_models import SemanticOperation
from .llm import (
    LLMPlanValidationError,
    LLMResponseError,
    OpenAICompatiblePlanner,
    PlannerError,
    ProviderNotConfiguredError,
)
from .models import ProviderConfig
from .provider_compat import (
    completion_budget_fields,
    completion_temperature,
    extract_chat_content,
    thinking_request_fields,
)
from .provider_security import (
    ProviderNetworkPolicy,
    ProviderURLPolicyError,
    provider_http_transport,
    request_with_response_limit,
)
from .repair_models import RepairPlannerIdentity
from .repair_planner import RepairContext, RepairPlanDraft

#: Bumped when the prompt or the parsing contract changes, so an old qualification stays
#: attributable to the question it actually answered.
MODEL_PLANNER_VERSION = "1"

_SYSTEM_PROMPT = """You are the repair planner of a P&ID engineering editor.

You receive ONE canonical validation finding and a frozen repair scope, and you return ONE
semantic transaction that removes that finding without damaging anything else.

Rules, all mandatory:
1. Repair exactly the finding you were given. Do not repair other findings, even ones you can see.
2. You may only modify element ids listed in scope.allowed_element_ids. A new element is allowed
   only when scope.permits_creation is true, and only at most scope.max_created_ids of them.
3. Never move, rename or re-route anything outside that scope.
4. Use real ids, real ports and real symbol keys from the context. Never invent an id, a port, a
   symbol key or an operation type.
5. Prefer the smallest change that removes the finding. A repair that rebuilds a neighbourhood is
   wrong even if it also happens to remove the finding.
6. Keep connector ids stable: re-bind an endpoint with reconnect_connector rather than deleting and
   recreating a line.
7. If no safe repair exists inside the scope, return {"decline": "human_required" or
   "not_repairable", "reason": "..."} instead of guessing. Declining is a correct answer.
8. Never add a waiver, never change validation settings, never claim a finding is acceptable.

Answer with JSON only, in this shape:
{"operations": [ <semantic operations> ], "rationale": "one sentence"}

Semantic operation schema:
__SCHEMA__
"""

_OPERATIONS = TypeAdapter(SemanticOperation)


def semantic_schema() -> dict[str, Any]:
    """The operation schema exactly as the model is shown it, and as the run records it."""

    return _OPERATIONS.json_schema()


def schema_fingerprint() -> str:
    return hashlib.sha256(
        json.dumps(semantic_schema(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def prompt_fingerprint() -> str:
    """Fingerprint of the question itself: template, version and schema."""

    payload = {
        "version": MODEL_PLANNER_VERSION,
        "system": _SYSTEM_PROMPT,
        "schema": schema_fingerprint(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_model_prompt(context: RepairContext) -> str:
    """The user turn: the localized payload, and nothing a planner could read the answer from.

    The payload is the same dictionary the context budget was measured against, so "what the
    model was shown" and "what the budget allowed" cannot drift apart.
    """

    payload = json.dumps(context.payload, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        f"Validation finding to repair and its frozen scope:\n{payload}\n\n"
        f"Attempt number: {context.attempt} of {context.request.budgets.max_attempts}.\n"
        "Return the semantic operations that remove the target finding."
    )


class ModelRepairPlanner:
    """A :class:`~agentcad.repair_planner.RepairPlanner` backed by an OpenAI-compatible model."""

    planner_id = "model-repair-planner"

    def __init__(
        self,
        *,
        provider: ProviderConfig | None = None,
        policy: ProviderNetworkPolicy | None = None,
        max_timeout_seconds: float | None = None,
        max_response_bytes: int = 1024 * 1024,
        temperature: float | None = None,
        client_factory: Any = None,
    ) -> None:
        self.policy = policy or ProviderNetworkPolicy()
        self.max_response_bytes = max_response_bytes
        self.temperature = temperature
        self._client_factory = client_factory or httpx.Client
        # Resolution is eager so a missing credential fails at construction, where it names
        # itself, rather than once per benchmark case.
        self.provider = self._resolve_provider(provider, max_timeout_seconds)
        self.token_usage_estimated = False
        self.last_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    # -- planning --------------------------------------------------------- #

    def identity(self) -> RepairPlannerIdentity:
        return RepairPlannerIdentity(
            planner_id=self.planner_id,
            planner_version=MODEL_PLANNER_VERSION,
            provider_class="openai-compatible",
            base_url_class=_endpoint_class(self.provider.base_url or ""),
            model=self.provider.model or "",
            temperature=self.temperature,
            max_output_tokens=_max_output_tokens(self.provider),
            timeout_seconds=self.provider.timeout_seconds,
            prompt_fingerprint=prompt_fingerprint(),
            schema_fingerprint=schema_fingerprint(),
            token_usage_estimated=self.token_usage_estimated,
        )

    def plan(self, context: RepairContext) -> RepairPlanDraft:
        """One attempt: ask the model, parse the answer, hand it to the same runner.

        Transport failures raise :class:`PlannerError` so the orchestrator records them as a
        case failure instead of crashing the benchmark; a model that answers with something the
        schema rejects raises :class:`LLMPlanValidationError`, which the orchestrator treats as
        a *replannable* problem and shows back to the model on the next attempt.
        """

        payload = json.dumps(context.payload, ensure_ascii=False, sort_keys=True)
        started = time.perf_counter()
        raw = self._complete(build_model_prompt(context))
        elapsed = int((time.perf_counter() - started) * 1000)
        operations, rationale, declined = self._parse(raw)
        if declined is not None:
            from .repair_planner import RepairDeclined

            raise RepairDeclined(
                declined.get("kind", "not_repairable"),
                declined.get("reason", "the model declined to plan a repair"),
                code="model_declined",
            )
        return RepairPlanDraft(
            operations=operations,
            rationale=rationale or f"model plan (context {len(payload)} bytes, {elapsed} ms)",
            source="model",
            input_tokens=self.last_usage["input_tokens"],
            output_tokens=self.last_usage["output_tokens"],
            token_usage_estimated=self.token_usage_estimated,
        )

    # -- transport -------------------------------------------------------- #

    def _complete(self, user_prompt: str) -> str:
        provider = self.provider
        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": [
                # ``replace`` rather than ``format``: the template contains literal JSON braces,
                # and a brace-escaping bug would corrupt the schema the model is shown.
                {
                    "role": "system",
                    "content": _SYSTEM_PROMPT.replace(
                        "__SCHEMA__", json.dumps(semantic_schema(), ensure_ascii=False)
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature
            if self.temperature is not None
            else completion_temperature(provider, 0),
            "response_format": {"type": "json_object"},
        }
        payload.update(completion_budget_fields(provider))
        payload.update(thinking_request_fields(provider))
        endpoint = (provider.base_url or "").rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"

        try:
            with self._client_factory(
                timeout=provider.timeout_seconds,
                follow_redirects=False,
                transport=provider_http_transport(self.policy),
            ) as client:
                response = request_with_response_limit(
                    client, "POST", endpoint, self.max_response_bytes, json=payload, headers=headers
                )
                if response.status_code in {400, 404, 422} and (
                    "response_format" in payload or "thinking" in payload
                ):
                    fallback = {
                        key: value
                        for key, value in payload.items()
                        if key not in {"response_format", "thinking", "reasoning_effort"}
                    }
                    response = request_with_response_limit(
                        client,
                        "POST",
                        endpoint,
                        self.max_response_bytes,
                        json=fallback,
                        headers=headers,
                    )
        except ProviderURLPolicyError as exc:
            raise PlannerError(f"provider request rejected by network policy: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise PlannerError(
                f"model did not finish within {provider.timeout_seconds or 0:g} seconds"
            ) from exc
        except httpx.RequestError as exc:
            raise PlannerError("could not reach the model provider") from exc

        _raise_for_status(response)
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMResponseError("model response was not valid JSON") from exc
        self._record_usage(body, user_prompt)
        try:
            return extract_chat_content(body)
        except ValueError as exc:
            raise LLMResponseError(str(exc)) from exc

    def _record_usage(self, body: Any, user_prompt: str) -> None:
        """Provider usage when it exists, the project's estimator when it does not (§F)."""

        usage = body.get("usage") if isinstance(body, dict) else None
        prompt_tokens = _int_or_none(
            (usage or {}).get("prompt_tokens") or (usage or {}).get("input_tokens")
        )
        completion_tokens = _int_or_none(
            (usage or {}).get("completion_tokens") or (usage or {}).get("output_tokens")
        )
        if prompt_tokens is None or completion_tokens is None:
            # Four characters per token is the estimator the repository already uses for prompt
            # sizing; the flag that travels with the number says it is an estimate.
            prompt_tokens = max(1, len(user_prompt) // 4)
            completion_tokens = max(1, len(json.dumps(body, ensure_ascii=False)) // 4)
            self.token_usage_estimated = True
        self.last_usage = {
            "input_tokens": int(prompt_tokens),
            "output_tokens": int(completion_tokens),
        }

    def _parse(self, raw: str) -> tuple[list[Any], str, dict[str, str] | None]:
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.split("\n", 1)[-1] if "\n" in text else text
        try:
            decoded = json.loads(text)
        except ValueError as exc:
            raise LLMResponseError(f"model answer was not JSON: {text[:200]}") from exc
        if not isinstance(decoded, dict):
            raise LLMPlanValidationError("model answer must be a JSON object")
        if "decline" in decoded:
            return [], "", {
                "kind": str(decoded.get("decline") or "not_repairable"),
                "reason": str(decoded.get("reason") or "the model declined"),
            }
        operations = decoded.get("operations")
        if operations is None and isinstance(decoded.get("transaction"), dict):
            operations = decoded["transaction"].get("operations")
        if not isinstance(operations, list) or not operations:
            raise LLMPlanValidationError("model answer contained no operations")
        parsed: list[Any] = []
        for index, item in enumerate(operations):
            try:
                parsed.append(_OPERATIONS.validate_python(item))
            except ValidationError as exc:
                raise LLMPlanValidationError(
                    f"operation {index} does not match the semantic schema: {exc.error_count()} error(s)"
                ) from exc
        return parsed, str(decoded.get("rationale") or ""), None

    # -- configuration ---------------------------------------------------- #

    def _resolve_provider(
        self, override: ProviderConfig | None, max_timeout_seconds: float | None
    ) -> ProviderConfig:
        try:
            return OpenAICompatiblePlanner._resolve_provider(
                override, self.policy, max_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - re-raised with the credential named
            if isinstance(exc, ProviderNotConfiguredError):
                raise
            raise ProviderNotConfiguredError(str(exc)) from exc


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise PlannerError("the model provider rejected the credential (401)")
    if response.status_code == 429:
        raise PlannerError("the model provider rate-limited the request (429)")
    if response.status_code >= 400:
        raise PlannerError(f"the model provider returned HTTP {response.status_code}")


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _max_output_tokens(provider: ProviderConfig) -> int | None:
    for key in ("max_completion_tokens", "max_tokens"):
        value = getattr(provider, key, None)
        parsed = _int_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _endpoint_class(base_url: str) -> str:
    """A class, never the URL: evidence may not leak an internal hostname (§M5-5).

    ``loopback`` and ``private`` are enough for a reviewer to tell a local qualification run
    from a hosted one, which is the only question this field answers.
    """

    host = (urlsplit(base_url).hostname or "").lower()
    if not host:
        return "unset"
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return "loopback"
    if host.endswith(".local") or host.startswith("10.") or host.startswith("192.168."):
        return "private"
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
        except (IndexError, ValueError):
            return "private"
        return "private" if 16 <= second <= 31 else "public"
    return "public"


__all__ = [
    "MODEL_PLANNER_VERSION",
    "ModelRepairPlanner",
    "build_model_prompt",
    "prompt_fingerprint",
    "schema_fingerprint",
    "semantic_schema",
]
