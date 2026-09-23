from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx
from pydantic import ValidationError

from .agent_semantic_models import (
    AgentTransactionAssessment,
    FullDiagramTransaction,
    SemanticAgentPlan,
    SemanticAgentReplanRequest,
    SemanticTransaction,
)
from .diagnostics import DiagnosticLogger
from .diagram_quality import drafting_prompt_contract
from .llm import (
    LLMPlanValidationError,
    LLMResponseError,
    OpenAICompatiblePlanner,
    ProviderConnectionError,
    ProviderNetworkPolicyError,
    ProviderResponseTooLargeError,
    ProviderTimeoutError,
)
from .models import AgentGenerateRequest, Document, ProviderConfig
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
from .service import DocumentService
from .symbols import SymbolRegistry

MAX_SCHEMA_REPAIRS = 2


class SemanticPlanValidationError(LLMPlanValidationError):
    def __init__(
        self,
        message: str,
        *,
        provider: ProviderConfig,
        validation_errors: list[dict[str, str]],
        schema_repair_attempts: int,
        normalized_field_count: int,
    ):
        super().__init__(message, provider=provider)
        self.validation_errors = validation_errors
        self.schema_repair_attempts = schema_repair_attempts
        self.normalized_field_count = normalized_field_count

    def detail(self) -> dict[str, Any]:
        payload = super().detail()
        payload.update(
            {
                "validation_errors": self.validation_errors,
                "schema_repair_attempts": self.schema_repair_attempts,
                "normalized_field_count": self.normalized_field_count,
            }
        )
        return payload


class SemanticAgentPlanner:
    def __init__(
        self,
        service: DocumentService,
        symbols: SymbolRegistry,
        diagnostics: DiagnosticLogger | None = None,
        *,
        provider_policy: ProviderNetworkPolicy | None = None,
        max_response_bytes: int = 4 * 1024 * 1024,
        max_timeout_seconds: float = 600,
    ):
        self.service = service
        self.symbols = symbols
        self.diagnostics = diagnostics
        self.provider_transport = OpenAICompatiblePlanner(
            service=service,
            symbols=symbols,
            provider_policy=provider_policy,
            max_response_bytes=max_response_bytes,
            max_timeout_seconds=max_timeout_seconds,
        )

    def plan(self, document_id: str, request: AgentGenerateRequest) -> SemanticAgentPlan:
        provider = self.provider_transport._resolve_provider(
            request.provider,
            self.provider_transport.provider_policy,
            self.provider_transport.max_timeout_seconds,
        )
        document = self.service.get_document(document_id)
        scene = self.service.scene_summary(document_id)
        user_prompt = (
            f"Current document JSON:\n{document.model_dump_json(indent=2)}\n\n"
            f"Scene summary:\n{json.dumps(scene, ensure_ascii=False, indent=2)}\n\n"
            f"Additional process/design context:\n{request.context or '(none)'}\n\n"
            f"User request:\n{request.prompt}"
        )
        plan = self._request_plan(
            provider,
            user_prompt,
            repair=False,
            document_id=document_id,
            document=document,
        )
        plan.transaction.expected_revision = (
            request.expected_revision
            if request.expected_revision is not None
            else document.revision
        )
        return plan

    @staticmethod
    def _completeness_block(failure: AgentTransactionAssessment) -> str:
        """Tell the model which of the two problems it is being asked to repair.

        A partial plan is not a hard failure: the retained operations are individually legal
        and the drawing is simply missing part of what was asked for. Saying so explicitly is
        the difference between "repair these rejected operations" and "start over", and the
        rejected operations are listed with the advice the compiler already computed for them.
        """

        if failure.operation_accounting != "evaluated":
            return (
                "Proposal accounting: not_evaluated -- no operation was examined, so the "
                "counts are unknown rather than zero.\n"
            )
        lines = [
            f"Proposal completeness: {failure.completeness}",
            f"({failure.accepted_operation_count} accepted, "
            f"{failure.rejected_operation_count} rejected of "
            f"{failure.proposed_operation_count} proposed)",
        ]
        if failure.completeness == "partial":
            lines.append(
                "This is a PARTIAL plan, not an invalid one. The accepted operations are "
                "legal and stay; the drawing is missing the rejected operations below. "
                "Return a replacement plan that keeps the accepted work and adds the missing "
                "part, or explains why an operation cannot be expressed with the catalogue."
            )
        elif failure.completeness == "empty" and failure.rejected_operation_count:
            lines.append(
                "Nothing survived: every submitted operation was rejected, so there is no "
                "accepted work to keep. Return a plan that draws the request using only "
                "catalogue symbols that exist and element ids that are already present."
            )
        for receipt in failure.rejected_operations:
            suggestions = "; ".join(receipt.suggestions) if receipt.suggestions else ""
            lines.append(
                f"  - [{receipt.reason_code}] operation #{receipt.original_index} "
                f"({receipt.operation_kind}) at {receipt.field_path or '(no field)'}: "
                f"{receipt.message}" + (f" | try: {suggestions}" if suggestions else "")
            )
        return "\n".join(lines) + "\n"

    def replan(
        self,
        document_id: str,
        request: SemanticAgentReplanRequest,
        failure: AgentTransactionAssessment,
    ) -> SemanticAgentPlan:
        provider = self.provider_transport._resolve_provider(
            request.provider,
            self.provider_transport.provider_policy,
            self.provider_transport.max_timeout_seconds,
        )
        document = self.service.get_document(document_id)
        scene = self.service.scene_summary(document_id)
        user_prompt = (
            f"Current document JSON:\n{document.model_dump_json(indent=2)}\n\n"
            f"Scene summary:\n{json.dumps(scene, ensure_ascii=False, indent=2)}\n\n"
            f"Original process/design context:\n{request.context or '(none)'}\n\n"
            f"Original user request:\n{request.prompt}\n\n"
            f"Failed semantic plan:\n{request.failed_plan.model_dump_json(indent=2)}\n\n"
            f"Structured failure analysis:\n{failure.model_dump_json(indent=2)}\n\n"
            f"{self._completeness_block(failure)}\n"
            f"Repair attempt: {request.attempt}. Return a complete replacement plan, not a patch to the failed JSON."
        )
        plan = self._request_plan(
            provider,
            user_prompt,
            repair=True,
            document_id=document_id,
            document=document,
        )
        plan.transaction.expected_revision = (
            request.expected_revision
            if request.expected_revision is not None
            else document.revision
        )
        return plan

    def _request_plan(
        self,
        provider: ProviderConfig,
        user_prompt: str,
        *,
        repair: bool,
        document_id: str,
        document: Document,
    ) -> SemanticAgentPlan:
        full_diagram = not document.elements
        transaction_model = FullDiagramTransaction if full_diagram else SemanticTransaction
        schema = transaction_model.model_json_schema()
        raw_plan = self._request_model_json(
            provider,
            system_prompt=self._system_prompt(
                schema, repair=repair, full_diagram=full_diagram
            ),
            user_prompt=user_prompt,
            temperature=0.05 if repair else 0.1,
        )
        total_normalized = 0
        last_errors: list[dict[str, str]] = []

        for schema_attempt in range(MAX_SCHEMA_REPAIRS + 1):
            shaped = self._coerce_plan_shape(raw_plan)
            normalized, normalized_count, normalized_paths = self._normalize_raw_plan(
                shaped,
                document,
            )
            total_normalized += normalized_count
            if normalized_count and self.diagnostics is not None:
                self.diagnostics.emit(
                    "llm.semantic_schema_repair.normalized",
                    document_id=document_id,
                    schema_attempt=schema_attempt,
                    normalized_field_count=normalized_count,
                    normalized_field_paths=normalized_paths,
                    model=provider.model,
                    base_url=provider.base_url,
                )

            try:
                plan = SemanticAgentPlan.model_validate(normalized)
                if full_diagram:
                    FullDiagramTransaction.model_validate(
                        plan.transaction.model_dump(mode="python")
                    )
            except ValidationError as exc:
                last_errors = self._compact_validation_errors(exc)
                if schema_attempt >= MAX_SCHEMA_REPAIRS:
                    if self.diagnostics is not None:
                        self.diagnostics.emit(
                            "llm.semantic_schema_repair.failed",
                            document_id=document_id,
                            schema_repair_attempts=schema_attempt,
                            normalized_field_count=total_normalized,
                            validation_error_count=len(last_errors),
                            validation_error_paths=[item["path"] for item in last_errors],
                            model=provider.model,
                            base_url=provider.base_url,
                        )
                    summary = "; ".join(
                        f"{item['path']}: {item['message']}" for item in last_errors[:5]
                    )
                    if len(last_errors) > 5:
                        summary += f"; and {len(last_errors) - 5} more"
                    raise SemanticPlanValidationError(
                        "model returned an invalid semantic transaction after "
                        f"{schema_attempt} schema repair attempt(s): {summary}",
                        provider=provider,
                        validation_errors=last_errors,
                        schema_repair_attempts=schema_attempt,
                        normalized_field_count=total_normalized,
                    ) from exc

                repair_attempt = schema_attempt + 1
                if self.diagnostics is not None:
                    self.diagnostics.emit(
                        "llm.semantic_schema_repair.started",
                        document_id=document_id,
                        schema_repair_attempt=repair_attempt,
                        normalized_field_count=total_normalized,
                        validation_error_count=len(last_errors),
                        validation_error_paths=[item["path"] for item in last_errors],
                        model=provider.model,
                        base_url=provider.base_url,
                    )
                raw_plan = self._request_model_json(
                    provider,
                    system_prompt=self._schema_repair_system_prompt(),
                    user_prompt=self._schema_repair_user_prompt(
                        normalized,
                        last_errors,
                        schema,
                        repair_attempt,
                    ),
                    temperature=0.0,
                    repair=True,
                )
                continue

            if schema_attempt and self.diagnostics is not None:
                self.diagnostics.emit(
                    "llm.semantic_schema_repair.completed",
                    document_id=document_id,
                    schema_repair_attempts=schema_attempt,
                    normalized_field_count=total_normalized,
                    model=provider.model,
                    base_url=provider.base_url,
                )
            return plan

        raise AssertionError(f"unreachable schema repair state: {last_errors}")

    def _request_model_json(
        self,
        provider: ProviderConfig,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        repair: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": completion_temperature(provider, temperature),
            "response_format": {"type": "json_object"},
        }
        payload.update(completion_budget_fields(provider))
        payload.update(thinking_request_fields(provider))
        headers = OpenAICompatiblePlanner._headers(provider)
        endpoint = provider.base_url.rstrip("/") + "/chat/completions"
        try:
            with httpx.Client(
                timeout=provider.timeout_seconds,
                follow_redirects=False,
                transport=provider_http_transport(self.provider_transport.provider_policy),
            ) as client:
                response = request_with_response_limit(
                    client,
                    "POST",
                    endpoint,
                    self.provider_transport.max_response_bytes,
                    json=payload,
                    headers=headers,
                )
                self.provider_transport._inspect_response(response, provider, endpoint)
                if response.status_code in {400, 404, 422} and any(
                    key in payload for key in (
                        "response_format",
                        "thinking",
                        "reasoning_effort",
                        "max_completion_tokens",
                    )
                ):
                    fallback_payload = dict(payload)
                    fallback_payload.pop("response_format", None)
                    fallback_payload.pop("thinking", None)
                    fallback_payload.pop("reasoning_effort", None)
                    fallback_payload.pop("max_completion_tokens", None)
                    response = request_with_response_limit(
                        client,
                        "POST",
                        endpoint,
                        self.provider_transport.max_response_bytes,
                        json=fallback_payload,
                        headers=headers,
                    )
                    self.provider_transport._inspect_response(response, provider, endpoint)
        except ProviderURLPolicyError as exc:
            if exc.category == "response size":
                raise ProviderResponseTooLargeError(str(exc), provider=provider) from exc
            raise ProviderNetworkPolicyError(
                str(exc), category=exc.category, provider=provider
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"model did not finish within {provider.timeout_seconds:g} seconds",
                provider=provider,
                timeout_seconds=provider.timeout_seconds,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderConnectionError(
                "could not connect to model provider",
                provider=provider,
            ) from exc

        OpenAICompatiblePlanner._raise_for_response(response, provider)
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError(
                "model response was not valid JSON",
                provider=provider,
            ) from exc
        try:
            content = extract_chat_content(data)
        except ValueError as exc:
            raise LLMResponseError(str(exc), provider=provider) from exc
        return OpenAICompatiblePlanner._parse_json(content, provider)

    def _stream_model_json(
        self,
        provider: ProviderConfig,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ):
        payload = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": completion_temperature(provider, temperature),
            "stream": True,
        }
        payload.update(completion_budget_fields(provider))
        payload.update(thinking_request_fields(provider))
        headers = OpenAICompatiblePlanner._headers(provider)
        endpoint = provider.base_url.rstrip("/") + "/chat/completions"

        full_content: list[str] = []
        full_thinking: list[str] = []
        inside_think = False

        def _stream_lines(client: httpx.Client, req_payload: dict[str, Any]):
            with client.stream("POST", endpoint, json=req_payload, headers=headers) as resp:
                self.provider_transport._inspect_response(resp, provider, endpoint)
                if resp.status_code in {400, 404, 422} and any(
                    k in req_payload for k in ("thinking", "reasoning_effort", "max_completion_tokens")
                ):
                    fallback = dict(req_payload)
                    fallback.pop("thinking", None)
                    fallback.pop("reasoning_effort", None)
                    fallback.pop("max_completion_tokens", None)
                    with client.stream("POST", endpoint, json=fallback, headers=headers) as fallback_resp:
                        self.provider_transport._inspect_response(fallback_resp, provider, endpoint)
                        if fallback_resp.is_error:
                            raise httpx.HTTPStatusError(
                                f"HTTP {fallback_resp.status_code}",
                                request=fallback_resp.request,
                                response=fallback_resp,
                            )
                        for line in fallback_resp.iter_lines():
                            yield line
                elif resp.is_error:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                else:
                    for line in resp.iter_lines():
                        yield line

        try:
            with httpx.Client(
                timeout=provider.timeout_seconds,
                follow_redirects=False,
                transport=provider_http_transport(self.provider_transport.provider_policy),
            ) as client:
                for line in _stream_lines(client, payload):
                    if not line:
                        continue
                    line_str = line.strip()
                    if not line_str.startswith("data:"):
                        continue
                    data_part = line_str[5:].strip()
                    if data_part == "[DONE]":
                        break
                    try:
                        chunk_json = json.loads(data_part)
                    except ValueError:
                        continue
                    choices = chunk_json.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}

                    # 1. Extract reasoning / thinking tokens (DeepSeek, Kimi, GLM, Ark, OpenRouter, Qwen, etc.)
                    reasoning_chunk = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or delta.get("thought")
                        or delta.get("thinking")
                        or choice.get("reasoning_content")
                        or choice.get("reasoning")
                        or ""
                    )
                    if reasoning_chunk:
                        full_thinking.append(reasoning_chunk)
                        yield ("thinking", reasoning_chunk)

                    # 2. Extract content tokens (also handling embedded <think> tags)
                    content_chunk = delta.get("content") or ""
                    if content_chunk:
                        if "<think>" in content_chunk or "<thought>" in content_chunk:
                            tag = "<think>" if "<think>" in content_chunk else "<thought>"
                            inside_think = True
                            parts = content_chunk.split(tag, 1)
                            if parts[0]:
                                full_content.append(parts[0])
                                yield ("content", parts[0])
                            content_chunk = parts[1]

                        if inside_think:
                            end_tag = (
                                "</think>"
                                if "</think>" in content_chunk
                                else "</thought>"
                                if "</thought>" in content_chunk
                                else None
                            )
                            if end_tag:
                                inside_think = False
                                parts = content_chunk.split(end_tag, 1)
                                if parts[0]:
                                    full_thinking.append(parts[0])
                                    yield ("thinking", parts[0])
                                if parts[1]:
                                    full_content.append(parts[1])
                                    yield ("content", parts[1])
                            else:
                                full_thinking.append(content_chunk)
                                yield ("thinking", content_chunk)
                        else:
                            full_content.append(content_chunk)
                            yield ("content", content_chunk)
        except Exception:
            raw = self._request_model_json(
                provider,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
            )
            if "explanation" in raw and raw["explanation"]:
                yield ("thinking", f"【工艺拓扑与改动分析】\n{raw['explanation']}")
            yield ("content", json.dumps(raw, ensure_ascii=False, indent=2))
            return raw

        accumulated = "".join(full_content)
        if not accumulated.strip():
            raw = self._request_model_json(
                provider,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
            )
            if "explanation" in raw and raw["explanation"]:
                yield ("thinking", f"【工艺拓扑与改动分析】\n{raw['explanation']}")
            yield ("content", json.dumps(raw, ensure_ascii=False, indent=2))
            return raw

        parsed = OpenAICompatiblePlanner._parse_json(accumulated, provider)
        if not full_thinking and "explanation" in parsed and parsed["explanation"]:
            yield ("thinking", f"【工艺拓扑与改动分析】\n{parsed['explanation']}")
        return parsed

    def stream_plan_events(
        self,
        document_id: str,
        request: AgentGenerateRequest,
    ):
        provider = self.provider_transport._resolve_provider(
            request.provider,
            self.provider_transport.provider_policy,
            self.provider_transport.max_timeout_seconds,
        )
        document = self.service.get_document(document_id)
        scene = self.service.scene_summary(document_id)
        user_prompt = (
            f"Current document JSON:\n{document.model_dump_json(indent=2)}\n\n"
            f"Scene summary:\n{json.dumps(scene, ensure_ascii=False, indent=2)}\n\n"
            f"Additional process/design context:\n{request.context or '(none)'}\n\n"
            f"User request:\n{request.prompt}"
        )
        full_diagram = not document.elements
        transaction_model = FullDiagramTransaction if full_diagram else SemanticTransaction
        schema = transaction_model.model_json_schema()
        system_prompt = self._system_prompt(schema, repair=False, full_diagram=full_diagram)

        raw_plan = yield from self._stream_model_json(
            provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.1,
        )

        shaped = self._coerce_plan_shape(raw_plan)
        normalized, _, _ = self._normalize_raw_plan(shaped, document)
        plan = SemanticAgentPlan.model_validate(normalized)
        if full_diagram:
            FullDiagramTransaction.model_validate(
                plan.transaction.model_dump(mode="python")
            )
        plan.transaction.expected_revision = (
            request.expected_revision
            if request.expected_revision is not None
            else document.revision
        )
        yield ("plan", plan)

    @staticmethod
    def _coerce_plan_shape(raw_plan: dict[str, Any]) -> dict[str, Any]:
        if "transaction" in raw_plan or "operations" not in raw_plan:
            return deepcopy(raw_plan)
        return {
            "explanation": raw_plan.get("explanation", ""),
            "transaction": {
                "operations": deepcopy(raw_plan["operations"]),
                "label": raw_plan.get("label", "Agent semantic modification"),
            },
        }

    @staticmethod
    def _normalize_raw_plan(
        raw_plan: dict[str, Any],
        document: Document,
    ) -> tuple[dict[str, Any], int, list[str]]:
        normalized = deepcopy(raw_plan)
        transaction = normalized.get("transaction")
        operations = transaction.get("operations") if isinstance(transaction, dict) else None
        if not isinstance(operations, list):
            return normalized, 0, []

        junction_ids = {
            element.id for element in document.elements if element.type == "junction"
        }
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("op") != "add_element":
                continue
            element = operation.get("element")
            if (
                isinstance(element, dict)
                and element.get("type") == "junction"
                and isinstance(element.get("id"), str)
            ):
                junction_ids.add(element["id"])

        changed_paths: list[str] = []

        def normalize_endpoint(endpoint: Any, path: str) -> None:
            if not isinstance(endpoint, dict):
                return
            element_id = endpoint.get("element_id")
            if element_id in junction_ids and not endpoint.get("port_id"):
                endpoint["port_id"] = "node"
                changed_paths.append(f"{path}.port_id")

        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                continue
            op = operation.get("op")
            if op == "add_element":
                element = operation.get("element")
                if isinstance(element, dict) and element.get("type") == "connector":
                    normalize_endpoint(
                        element.get("source"),
                        f"transaction.operations.{index}.add_element.element.connector.source",
                    )
                    normalize_endpoint(
                        element.get("target"),
                        f"transaction.operations.{index}.add_element.element.connector.target",
                    )
            elif op == "reconnect_connector":
                element_id = operation.get("element_id")
                if element_id in junction_ids and not operation.get("port_id"):
                    operation["port_id"] = "node"
                    changed_paths.append(
                        f"transaction.operations.{index}.reconnect_connector.port_id"
                    )
            elif op == "connect_ports":
                if (
                    operation.get("source_element_id") in junction_ids
                    and not operation.get("source_port_id")
                ):
                    operation["source_port_id"] = "node"
                    changed_paths.append(
                        f"transaction.operations.{index}.connect_ports.source_port_id"
                    )
                if (
                    operation.get("target_element_id") in junction_ids
                    and not operation.get("target_port_id")
                ):
                    operation["target_port_id"] = "node"
                    changed_paths.append(
                        f"transaction.operations.{index}.connect_ports.target_port_id"
                    )

        return normalized, len(changed_paths), changed_paths

    @staticmethod
    def _compact_validation_errors(exc: ValidationError) -> list[dict[str, str]]:
        compact: list[dict[str, str]] = []
        for error in exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )[:50]:
            compact.append(
                {
                    "path": ".".join(str(part) for part in error.get("loc", ())),
                    "type": str(error.get("type", "validation_error")),
                    "message": str(error.get("msg", "invalid value")),
                }
            )
        return compact

    @staticmethod
    def _schema_repair_system_prompt() -> str:
        return (
            "You repair P&ID-Agent semantic transaction JSON. Return one complete corrected JSON object only. "
            "Preserve the original engineering intent, IDs, coordinates, labels and operation order unless a "
            "validation error requires a change. Do not add unrelated elements. Every bound connector endpoint "
            "must provide both element_id and port_id. A junction endpoint always uses port_id 'node'. A free "
            "endpoint has no element_id or port_id and must provide point. When instrument_tap already creates "
            "a labeled instrument, remove any standalone add_element symbol with the same label. Preserve the "
            "drafting contract: exact valve type, correct inlet/outlet direction, aligned ports, orthogonal "
            "routing, no micro-doglegs, and jump bridges for non-connecting crossings."
        )

    @staticmethod
    def _schema_repair_user_prompt(
        raw_plan: dict[str, Any],
        errors: list[dict[str, str]],
        schema: dict[str, Any],
        attempt: int,
    ) -> str:
        return (
            f"Schema repair attempt: {attempt}\n\n"
            f"Invalid semantic plan JSON:\n{json.dumps(raw_plan, ensure_ascii=False)}\n\n"
            f"Validation errors:\n{json.dumps(errors, ensure_ascii=False)}\n\n"
            f"Semantic transaction JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}"
        )

    def _system_prompt(
        self, schema: dict, *, repair: bool, full_diagram: bool
    ) -> str:
        mode = (
            "The previous plan failed. Use the structured issue code, available values and suggestions "
            "to produce the smallest complete corrected plan. Do not repeat the same invalid IDs, ports, "
            "symbol keys or endpoint edits."
            if repair
            else "Plan the smallest atomic change that satisfies the user request."
        )
        task_mode = (
            "This is an empty-document task. Only use operations in the supplied full-diagram schema. "
            "A request that only manages layers or systems is valid and may intentionally leave the canvas "
            "without visible elements. Never create a raw connector through add_element. Use connect_ports "
            "for equipment-to-equipment pipes, with waypoints for explicit orthogonal routing. Use "
            "instrument_tap to split a main connector and create a junction, root valve, instrument and "
            "bound branch pipes. "
            if full_diagram
            else "This is a local-edit task on an existing drawing. "
        )
        return (
            "You are P&ID-Agent's deterministic semantic engineering and drafting engine. "
            f"{task_mode}"
            "Return JSON only with keys 'explanation' and 'transaction'. Preserve unrelated "
            "elements. Use only real element IDs, "
            "symbol keys and port IDs from the supplied document and catalog. "
            "When a later operation references an element or connector added earlier in the same transaction, "
            "assign an explicit unique id in the add operation and reuse that exact id. "
            "Use connect_ports to create a semantic pipe between two real ports. Prefer no waypoints; "
            "the deterministic router owns the final route. Supply waypoints only as coarse lane hints when "
            "a genuine obstacle or reserved pipe highway makes a direct orthogonal route unsuitable. "
            "Use instrument_tap for pressure, temperature or flow takeoffs from a main connector. "
            "instrument_tap already creates the instrument symbol, root valve, junction and branch "
            "connectors: never add a second standalone symbol with the same instrument label. "
            "When the user names an equipment type that exists in the catalog, use that exact symbol "
            "instead of a merely similar category. Before returning a full diagram, verify that every "
            "explicitly requested inlet, outlet and utility-side connection is represented by a real connector. "
            "A symbol position is the top-left corner of its unrotated bounding box, not the visual center and "
            "not a port coordinate. For a straight horizontal process train, calculate each symbol's position "
            "from the catalog port offset so every connected process port has exactly the same y coordinate; "
            "do not give unlike symbols the same position.y and rely on orthogonal routing to hide the mismatch. "
            "Place off-page connectors so the pipe lands on the visual centerline of their real process port. "
            "Use reconnect_connector to move one existing connector endpoint; never edit source or target "
            "through update_element. Use replace_symbol to replace equipment while preserving connector IDs; "
            "provide port_mapping whenever old connected port IDs do not exist on the replacement. "
            "Use delete_element with an explicit connection_policy. Prefer reject_if_connected unless the user "
            "clearly asked to leave detached pipes or delete the connected pipes. "
            "Never change symbol_key through update_element. Use junction ports only as 'node'. "
            "For add_element connector endpoints: element_id and port_id are an inseparable pair; a free endpoint "
            "must omit both and provide point. Include expected_revision and a concise transaction label. "
            f"{mode}\n\n{drafting_prompt_contract()}\n\n"
            f"Available symbol catalog:\n{self.symbols.as_prompt_catalog()}\n\n"
            f"Semantic transaction JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}"
        )
