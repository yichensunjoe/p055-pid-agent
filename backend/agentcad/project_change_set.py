"""M13-D3: change-set contract, deterministic impact analysis, shadow preview.

Gate-frozen scope (M13-D3 CODE GO): ChangeSetIntent + canonical intent_hash
(including required tz-aware evaluation_as_of); C1 declared-pins validation
(no silent replacement — current pins are read ONLY for comparison); C2 single
CAS source (mutation payloads carry no revision truth); deterministic impact
with fail-closed stable codes and existing-active-link re-pin derivation only
(link create/delete are out of scope); zero-write shadow preview with a
project-state-unchanged hard-lock. Approval wiring, atomic commit, HTTP write
surface: D4/D5, not here.

Design frozen at reports/m13-d1-design.md (M13-D1 DESIGN PASS, R82 wording
fixes applied).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .cable_service import CableDocumentView, CableService
from .models import (
    Operation,
    StrictModel,
    UpdateElementOperation,
)
from .project_readiness import (
    MemberReadinessSnapshot,
    ProjectReadiness,
    assess_project_readiness_core,
)
from .service import DocumentService
from .store import SQLiteDocumentStore
from .validation_profile import load_profile

_OPERATION_ADAPTER = TypeAdapter(list[Operation])

MUTATION_PID_TRANSACTION = "pid_transaction"
MUTATION_CABLE_UPDATE = "cable_update"
_MUTATION_KINDS = (MUTATION_PID_TRANSACTION, MUTATION_CABLE_UPDATE)

# C2: mutation payloads must not carry revision truth anywhere.
_REVISION_TRUTH_KEYS = {"expected_revision", "revision"}


class ChangeSetError(RuntimeError):
    """Fail-closed change-set refusal with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


class ChangeSetMutation(StrictModel):
    domain: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("domain")
    @classmethod
    def _domain_known(cls, value: str) -> str:
        if value not in ("pid", "cable"):
            raise ValueError(f"unknown mutation domain: {value!r}")
        return value

    @field_validator("kind")
    @classmethod
    def _kind_known(cls, value: str) -> str:
        if value not in _MUTATION_KINDS:
            raise ValueError(f"unknown mutation kind: {value!r}")
        return value


class ChangeSetIntent(StrictModel):
    project_id: str = Field(min_length=1)
    base_member_pins: dict[str, int]
    evaluation_as_of: datetime
    mutations: list[ChangeSetMutation] = Field(min_length=1)

    @field_validator("evaluation_as_of")
    @classmethod
    def _as_of_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("evaluation_as_of must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _single_mutation_per_document(self):
        seen = set()
        for mutation in self.mutations:
            key = (mutation.domain, mutation.document_id)
            if key in seen:
                raise ValueError(
                    f"at most one mutation per (domain, document): {key!r}"
                )
            seen.add(key)
        return self


def new_change_set_id() -> str:
    return f"cs_{uuid4().hex[:12]}"


def _canonical(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def canonical_change_set_intent(intent: ChangeSetIntent) -> str:
    return _canonical(intent.model_dump(mode="json"))


def change_set_intent_hash(intent: ChangeSetIntent) -> str:
    return hashlib.sha256(canonical_change_set_intent(intent).encode("utf-8")).hexdigest()


def _scan_revision_leak(payload: Any, *, path: str = "") -> str | None:
    """C2: return the offending key path, or None when clean."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _REVISION_TRUTH_KEYS:
                return f"{path}.{key}" if path else key
            found = _scan_revision_leak(value, path=f"{path}.{key}" if path else key)
            if found:
                return found
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found = _scan_revision_leak(value, path=f"{path}[{index}]")
            if found:
                return found
    return None


def validate_single_cas_source(intent: ChangeSetIntent) -> None:
    """C2. Cable payloads may carry a top-level ``revision`` only when it equals
    the declared base pin; pid operation payloads must not carry revision keys
    at any depth. Declared base_member_pins remains the sole CAS truth."""
    for mutation in intent.mutations:
        if mutation.domain == "cable":
            # Only the top-level CableDocument revision is mechanically
            # tolerated, and only when it equals the declared base pin; any
            # other depth is rejected like pid payloads.
            revision = mutation.payload.get("revision")
            if revision is not None and int(revision) != int(
                intent.base_member_pins[mutation.document_id]
            ):
                raise ChangeSetError(
                    "change_set_revision_leak",
                    f"cable payload revision {revision!r} != declared base pin "
                    f"{intent.base_member_pins[mutation.document_id]!r}",
                )
            remainder = {k: v for k, v in mutation.payload.items() if k != "revision"}
            leak = _scan_revision_leak(remainder)
            if leak is not None:
                raise ChangeSetError(
                    "change_set_revision_leak",
                    f"cable payload carries revision truth at {leak!r}",
                )
            continue
        leak = _scan_revision_leak(mutation.payload)
        if leak is not None:
            raise ChangeSetError(
                "change_set_revision_leak",
                f"pid mutation payload carries revision truth at {leak!r}",
            )


def validate_declared_pins(
    store: SQLiteDocumentStore, *, project_id: str, declared: dict[str, int]
) -> None:
    """C1: the server re-reads current revisions ONLY to compare. Declared pins
    must be exactly the project member set, each equal to the current revision;
    anything else refuses immediately — declared pins are never silently
    upgraded."""
    members = store.list_project_documents(project_id)
    expected = {document_id for document_id, _domain, _added in members}
    if set(declared) != expected:
        raise ChangeSetError(
            "change_set_base_not_current",
            f"declared pins must be exactly the member set; "
            f"missing={sorted(expected - set(declared))} "
            f"extra={sorted(set(declared) - expected)}",
        )
    for document_id, domain, _added in members:
        if domain == "cable":
            envelope = store.get_cable_envelope(document_id)
            current = None if envelope is None else int(envelope[0])
        else:
            stored = store.get(document_id)
            current = None if stored is None else stored.document.revision
        if current is None or current != int(declared[document_id]):
            raise ChangeSetError(
                "change_set_base_not_current",
                f"member {document_id!r} declared pin {declared[document_id]!r} "
                f"is not the current revision {current!r}",
            )


@dataclass(frozen=True)
class ImpactResult:
    affected_documents: tuple[str, ...]
    affected_objects: tuple[tuple[str, str], ...]  # (document_id, object_ref)
    affected_links: tuple[dict[str, Any], ...]
    derived_repin_actions: tuple[RepinAction, ...]
    validation_scope: dict[str, Any]
    issues: tuple[str, ...]
    removed_object_refs: tuple[tuple[str, str, str], ...]  # (link_id, endpoint_role, object_ref)

    def canonical(self) -> dict[str, Any]:
        """Full impacted snapshot — the D1 frozen six fields
        (affected_documents / affected_objects / affected_links /
        derived_repin_actions / validation_scope / issues), exactly what D4
        exact-approval binding and the change-set row persist.
        removed_object_refs is kept as additional internal evidence."""
        return {
            "affected_documents": list(self.affected_documents),
            "affected_objects": [list(ref) for ref in self.affected_objects],
            "affected_links": [dict(link) for link in self.affected_links],
            "derived_repin_actions": [
                {
                    "link_id": action.link_id,
                    "pinned_source_revision": action.pinned_source_revision,
                    "pinned_target_revision": action.pinned_target_revision,
                }
                for action in self.derived_repin_actions
            ],
            "validation_scope": self.validation_scope,
            "issues": list(self.issues),
            "removed_object_refs": [list(ref) for ref in self.removed_object_refs],
        }


@dataclass(frozen=True)
class RepinAction:
    link_id: str
    pinned_source_revision: int
    pinned_target_revision: int


def _canonical_link_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "link_id": str(row["link_id"]),
        "relation_type": str(row["relation_type"]),
        "source_domain": str(row["source_domain"]),
        "source_document_id": str(row["source_document_id"]),
        "source_object_ref": str(row["source_object_ref"]),
        "source_endpoint": str(row["source_endpoint"]),
        "target_domain": str(row["target_domain"]),
        "target_document_id": str(row["target_document_id"]),
        "target_object_ref": str(row["target_object_ref"]),
        "pinned_source_revision": int(row["pinned_source_revision"]),
        "pinned_target_revision": int(row["pinned_target_revision"]),
    }


def _parse_pid_operations(payload: dict[str, Any]) -> list[Any]:
    """Canonical payload ops are plain JSON; validation uses the SAME
    discriminated union the HTTP layer uses (all ten operation kinds,
    min_length=1 / max_length=1000), so one pid_transaction speaks one
    language at every entry point."""
    raw = payload.get("operations")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 1000:
        raise ChangeSetError(
            "impact_unknown_kind",
            "pid operations must be a list of 1..1000 items",
        )
    try:
        return _OPERATION_ADAPTER.validate_python(raw)
    except Exception as exc:
        raise ChangeSetError(
            "impact_unknown_kind", f"pid operation is not a valid Operation: {exc}"
        ) from exc


class ChangeSetImpactAnalyzer:
    """Deterministic, fail-closed. Existing active links only: re-pin
    derivation; create/delete never guessed (M13 v1 narrowing)."""

    def __init__(
        self,
        store: SQLiteDocumentStore,
        pid_service: DocumentService,
        cable_service: CableService,
    ) -> None:
        self._store = store
        self._pid = pid_service
        self._cable = cable_service

    def analyze(self, intent: ChangeSetIntent) -> ImpactResult:
        validate_declared_pins(
            self._store, project_id=intent.project_id, declared=intent.base_member_pins
        )
        validate_single_cas_source(intent)

        members = {
            document_id: domain
            for document_id, domain, _added in self._store.list_project_documents(
                intent.project_id
            )
        }
        mutated = {m.document_id: m for m in intent.mutations}

        removed_pid: dict[str, set[str]] = {}  # doc_id -> removed element ids
        for mutation in intent.mutations:
            domain = members.get(mutation.document_id)
            if domain is None or domain != mutation.domain:
                raise ChangeSetError(
                    "impact_unknown_object",
                    f"mutation target {mutation.document_id!r} is not a {mutation.domain} member",
                )
            if mutation.kind == MUTATION_PID_TRANSACTION:
                removed = self._removed_pid_elements(mutation)
                if removed:
                    removed_pid[mutation.document_id] = removed
            else:  # cable_update
                self._validate_cable_payload(mutation)

        links = [
            _canonical_link_row(row)
            for row in self._store.list_active_engineering_links(intent.project_id)
        ]

        # linked-object invalidation is fail-closed: never commit a bad reference.
        invalidated: list[tuple[str, str, str]] = []
        for link in links:
            if (
                link["target_document_id"] in mutated
                and link["target_object_ref"] in removed_pid.get(link["target_document_id"], set())
            ):
                invalidated.append((link["link_id"], "target", link["target_object_ref"]))
            if (
                link["source_document_id"] in mutated
                and self._cable_segment_removed(
                    mutated[link["source_document_id"]], link["source_object_ref"]
                )
            ):
                invalidated.append((link["link_id"], "source", link["source_object_ref"]))
        if invalidated:
            detail = ", ".join(f"{lid}:{role}:{ref}" for lid, role, ref in sorted(invalidated))
            raise ChangeSetError(
                "change_set_invalidates_link",
                f"change set removes/invalidates linked objects: {detail}",
            )

        # equipment predicate re-check for updated pid elements (staged, in-memory).
        self._validate_equipment_predicate(intent)

        # affected = mutated documents ∪ DIRECT link counterparts. Peers keep
        # their declared pin, never inherit a revision bump, and their own
        # links are untouched — no transitive diffusion.
        direct_peers = {
            link["source_document_id"]
            for link in links
            if link["target_document_id"] in mutated
        } | {
            link["target_document_id"]
            for link in links
            if link["source_document_id"] in mutated
        }
        affected_documents = sorted(set(mutated) | (direct_peers - set(mutated)))
        affected_links = tuple(
            link
            for link in links
            if link["source_document_id"] in mutated
            or link["target_document_id"] in mutated
        )
        actions = []
        for link in affected_links:
            source_mutated = link["source_document_id"] in mutated
            target_mutated = link["target_document_id"] in mutated
            actions.append(
                RepinAction(
                    link_id=link["link_id"],
                    pinned_source_revision=(
                        intent.base_member_pins[link["source_document_id"]] + 1
                        if source_mutated
                        else intent.base_member_pins[link["source_document_id"]]
                    ),
                    pinned_target_revision=(
                        intent.base_member_pins[link["target_document_id"]] + 1
                        if target_mutated
                        else intent.base_member_pins[link["target_document_id"]]
                    ),
                )
            )
        affected_objects = self._affected_objects(intent, removed_pid)
        validation_scope = {
            "member_documents": list(affected_documents),
            "link_rules": "m12-seven-code-subset-over-affected",
            "single_domain_readiness": "canonical-assess",
        }
        return ImpactResult(
            affected_documents=tuple(affected_documents),
            affected_objects=affected_objects,
            affected_links=affected_links,
            derived_repin_actions=tuple(actions),
            validation_scope=validation_scope,
            issues=(),
            removed_object_refs=tuple(sorted(invalidated)),
        )

    def _removed_pid_elements(self, mutation: ChangeSetMutation) -> set[str]:
        """Removed element ids from the STAGED post-state: explicit
        delete_element ops plus clear_document, which removes every element in
        the document. Typed via the real Operation union (all ten kinds)."""
        operations = _parse_pid_operations(mutation.payload)
        removed = {
            operation.element_id
            for operation in operations
            if operation.__class__.__name__ == "DeleteElementOperation"
        }
        if any(
            operation.__class__.__name__ == "ClearDocumentOperation"
            for operation in operations
        ):
            try:
                document = self._pid.get_document(mutation.document_id)
            except Exception as exc:
                raise ChangeSetError(
                    "impact_unknown_object",
                    f"pid document {mutation.document_id!r} not found",
                ) from exc
            removed = removed | {element.id for element in document.elements}
        return set(removed)

    def _affected_objects(
        self, intent: ChangeSetIntent, removed_pid: dict[str, set[str]]
    ) -> tuple[tuple[str, str], ...]:
        """Object-level impact: touched pid element ids (add/update/delete)
        and cable segment deltas, as (document_id, object_ref) pairs."""
        objects: set[tuple[str, str]] = set()
        for mutation in intent.mutations:
            if mutation.kind == MUTATION_PID_TRANSACTION:
                operations = _parse_pid_operations(mutation.payload)
                for operation in operations:
                    name = operation.__class__.__name__
                    if name == "AddElementOperation":
                        objects.add((mutation.document_id, operation.element.id))
                    elif name in ("UpdateElementOperation", "DeleteElementOperation"):
                        objects.add((mutation.document_id, operation.element_id))
                    elif name == "ClearDocumentOperation":
                        try:
                            document = self._pid.get_document(mutation.document_id)
                        except Exception:
                            continue
                        objects |= {
                            (mutation.document_id, element.id)
                            for element in document.elements
                        }
            else:
                try:
                    view = self._cable.load(mutation.document_id)
                except Exception:
                    continue
                from .cable_models import CableDocument

                staged = CableDocument.model_validate(
                    {**mutation.payload, "revision": 0}
                )
                base_ids = {segment.id for segment in view.document.segments}
                staged_ids = {segment.id for segment in staged.segments}
                objects |= {
                    (mutation.document_id, segment_id)
                    for segment_id in (base_ids ^ staged_ids)
                }
        return tuple(sorted(objects))

    def _validate_cable_payload(self, mutation: ChangeSetMutation) -> None:
        try:
            self._cable.load(mutation.document_id)
        except Exception as exc:
            raise ChangeSetError(
                "impact_unknown_object", f"cable document {mutation.document_id!r} not found"
            ) from exc
        try:
            from .cable_models import CableDocument

            CableDocument.model_validate({**mutation.payload, "revision": 0})
        except Exception as exc:
            raise ChangeSetError(
                "impact_unknown_object",
                f"cable payload for {mutation.document_id!r} is not a valid document",
            ) from exc

    def _cable_segment_removed(self, mutation: ChangeSetMutation, segment_id: str) -> bool:
        try:
            view = self._cable.load(mutation.document_id)
        except Exception:
            return False
        base_ids = {segment.id for segment in view.document.segments}
        from .cable_models import CableDocument

        staged = CableDocument.model_validate({**mutation.payload, "revision": 0})
        staged_ids = {segment.id for segment in staged.segments}
        return segment_id in base_ids and segment_id not in staged_ids

    def _validate_equipment_predicate(self, intent: ChangeSetIntent) -> None:
        instrument_keys = frozenset(
            key
            for key, symbol in self._pid.symbols._symbols.items()  # noqa: SLF001
            if symbol.category == "仪表"
        )
        linked_targets = {
            (str(row["target_document_id"]), str(row["target_object_ref"]))
            for row in self._store.list_active_engineering_links(intent.project_id)
        }
        for mutation in intent.mutations:
            if mutation.kind != MUTATION_PID_TRANSACTION:
                continue
            update_ops = [
                operation
                for operation in _parse_pid_operations(mutation.payload)
                if isinstance(operation, UpdateElementOperation)
                and (mutation.document_id, operation.element_id) in linked_targets
            ]
            if not update_ops:
                continue
            try:
                document = self._pid.get_document(mutation.document_id)
            except Exception as exc:
                raise ChangeSetError(
                    "impact_unknown_object",
                    f"pid document {mutation.document_id!r} not found",
                ) from exc
            staged = self._pid._stage_mutation(document, update_ops)  # noqa: SLF001
            for operation in update_ops:
                element_id = str(operation.element_id)
                element = next((e for e in staged.elements if e.id == element_id), None)
                if element is None:
                    continue
                symbol_key = getattr(element, "symbol_key", "")
                if symbol_key in instrument_keys:
                    raise ChangeSetError(
                        "change_set_invalidates_link",
                        f"update turns element {element_id!r} into an instrument, "
                        "breaking the equipment predicate of its links",
                    )


@dataclass(frozen=True)
class PreviewResult:
    """Canonical, deterministic snapshot; persisted on the change-set row and
    compared against post-commit reality by the frozen e2e."""

    intent_hash: str
    revision_projection: dict[str, int]
    pid_diffs: dict[str, dict[str, Any]]
    cable_diffs: dict[str, dict[str, Any]]
    derived_repin_actions: tuple[dict[str, Any], ...]
    readiness_before: ProjectReadiness
    readiness_after: ProjectReadiness
    evaluation_as_of: str

    def canonical(self) -> dict[str, Any]:
        return {
            "intent_hash": self.intent_hash,
            "revision_projection": self.revision_projection,
            "pid_diffs": self.pid_diffs,
            "cable_diffs": self.cable_diffs,
            "derived_repin_actions": [dict(action) for action in self.derived_repin_actions],
            "readiness_before": _readiness_to_canonical(self.readiness_before),
            "readiness_after": _readiness_to_canonical(self.readiness_after),
            "evaluation_as_of": self.evaluation_as_of,
        }


def _readiness_to_canonical(readiness: ProjectReadiness) -> dict[str, Any]:
    return {
        "state": readiness.state,
        "issues": [
            [issue.code, issue.severity, issue.link_id] for issue in readiness.issues
        ],
        "result_hash": readiness.result_hash,
    }


class ChangeSetPreviewer:
    """Zero-write shadow execution. Engineering state is untouched: only reads
    plus in-memory staging; the frozen hard-lock asserts revisions, pins and
    audit counts are unchanged by a preview."""

    def __init__(
        self,
        store: SQLiteDocumentStore,
        pid_service: DocumentService,
        cable_service: CableService,
    ) -> None:
        self._store = store
        self._pid = pid_service
        self._cable = cable_service

    def preview(self, intent: ChangeSetIntent, impact: ImpactResult) -> PreviewResult:
        mutated_docs = {mutation.document_id for mutation in intent.mutations}
        revision_projection = {
            document_id: (
                int(pin) + 1 if document_id in mutated_docs else int(pin)
            )
            for document_id, pin in intent.base_member_pins.items()
        }
        pid_diffs: dict[str, dict[str, Any]] = {}
        cable_diffs: dict[str, dict[str, Any]] = {}
        staged_members: dict[str, tuple[str, Any]] = {}

        for mutation in intent.mutations:
            if mutation.kind == MUTATION_PID_TRANSACTION:
                document = self._pid.get_document(mutation.document_id)
                operations = _parse_pid_operations(mutation.payload)
                staged = self._pid._stage_mutation(document, operations)  # noqa: SLF001
                staged_members[mutation.document_id] = ("pid", staged)
                pid_diffs[mutation.document_id] = self._pid_diff(document, staged)
            else:
                from .cable_models import CableDocument

                view = self._cable.load(mutation.document_id)
                staged_doc = CableDocument.model_validate(
                    {**mutation.payload, "revision": 0}
                ).model_copy(update={"revision": view.document.revision + 1})
                staged_members[mutation.document_id] = (
                    "cable",
                    CableDocumentView(document_id=mutation.document_id, document=staged_doc),
                )
                cable_diffs[mutation.document_id] = self._cable_diff(view.document, staged_doc)

        readiness_before = self._assess_current(intent)
        readiness_after = self._assess_staged(intent, staged_members, impact)
        return PreviewResult(
            intent_hash=change_set_intent_hash(intent),
            revision_projection=revision_projection,
            pid_diffs=pid_diffs,
            cable_diffs=cable_diffs,
            derived_repin_actions=tuple(
                {
                    "link_id": action.link_id,
                    "pinned_source_revision": action.pinned_source_revision,
                    "pinned_target_revision": action.pinned_target_revision,
                }
                for action in impact.derived_repin_actions
            ),
            readiness_before=readiness_before,
            readiness_after=readiness_after,
            evaluation_as_of=intent.evaluation_as_of.isoformat(),
        )

    # ---- readiness via the pure core (same engine for current and staged) ----

    def _member_snapshot_inputs(
        self, intent: ChangeSetIntent, staged_members: dict[str, tuple[str, Any]]
    ) -> list[MemberReadinessSnapshot]:
        profile = load_profile()
        snapshots = []
        for document_id, domain, _added in self._store.list_project_documents(
            intent.project_id
        ):
            if document_id in staged_members:
                staged_domain, staged = staged_members[document_id]
                if staged_domain == "pid":
                    from .release_validator import assess_release_readiness

                    readiness = assess_release_readiness(
                        staged,
                        self._pid.symbols,
                        profile,
                        project_id=intent.project_id,
                        now=intent.evaluation_as_of,
                    )
                    snapshots.append(
                        MemberReadinessSnapshot(
                            document_id=document_id,
                            domain="pid",
                            state=readiness.state,
                            readiness_hash=readiness.readiness_hash,
                            revision=staged.revision,
                        )
                    )
                else:
                    from .cable_validation import assess

                    readiness = assess(staged)
                    snapshots.append(
                        MemberReadinessSnapshot(
                            document_id=document_id,
                            domain="cable",
                            state=readiness.state,
                            readiness_hash=readiness.result_hash,
                            revision=staged.document.revision,
                        )
                    )
                continue
            if domain == "cable":
                from .cable_validation import assess

                view = self._cable.load(document_id)
                readiness = assess(view)
                snapshots.append(
                    MemberReadinessSnapshot(
                        document_id=document_id,
                        domain="cable",
                        state=readiness.state,
                        readiness_hash=readiness.result_hash,
                        revision=view.document.revision,
                    )
                )
            else:
                from .release_validator import assess_release_readiness

                document = self._pid.get_document(document_id)
                readiness = assess_release_readiness(
                    document,
                    self._pid.symbols,
                    profile,
                    project_id=intent.project_id,
                    now=intent.evaluation_as_of,
                )
                snapshots.append(
                    MemberReadinessSnapshot(
                        document_id=document_id,
                        domain="pid",
                        state=readiness.state,
                        readiness_hash=readiness.readiness_hash,
                        revision=document.revision,
                    )
                )
        return snapshots


    def _assess_current(self, intent: ChangeSetIntent) -> ProjectReadiness:
        from .project_readiness import ProjectReadinessService

        return ProjectReadinessService(
            self._store, self._pid, self._cable
        ).assess(project_id=intent.project_id, evaluation_as_of=intent.evaluation_as_of)

    def _assess_staged(
        self,
        intent: ChangeSetIntent,
        staged_members: dict[str, tuple[str, Any]],
        impact: ImpactResult,
    ) -> ProjectReadiness:
        snapshots = self._member_snapshot_inputs(intent, staged_members)
        projected = {
            action.link_id: action for action in impact.derived_repin_actions
        }
        rows = []
        for row in self._store.list_active_engineering_links(intent.project_id):
            enriched = dict(row)
            action = projected.get(str(row["link_id"]))
            if action is not None:
                enriched["pinned_source_revision"] = action.pinned_source_revision
                enriched["pinned_target_revision"] = action.pinned_target_revision
            rows.append(
                self._fill_row_extras(enriched, staged_members)
            )
        return assess_project_readiness_core(
            project_id=intent.project_id,
            evaluation_as_of=intent.evaluation_as_of,
            member_snapshots=snapshots,
            link_rows=rows,
        )

    def _fill_row_extras(
        self, row: dict[str, Any], staged_members: dict[str, tuple[str, Any]]
    ) -> dict[str, Any]:
        """Reader-resolved extras for the pure core, from staged state when the
        document is mutated and from committed state otherwise. Read-only."""
        source_id = str(row["source_document_id"])
        target_id = str(row["target_document_id"])
        row["_registry_source"] = self._store.document_domain(source_id)
        row["_registry_target"] = self._store.document_domain(target_id)

        staged_source = staged_members.get(source_id)
        if staged_source is not None and staged_source[0] == "cable":
            row["_source_revision"] = staged_source[1].document.revision
            row["_source_reference_exists"] = (
                str(row["source_endpoint"]) in ("from", "to")
                and any(
                    segment.id == str(row["source_object_ref"])
                    for segment in staged_source[1].document.segments
                )
            )
        else:
            envelope = self._store.get_cable_envelope(source_id)
            row["_source_revision"] = None if envelope is None else int(envelope[0])
            if envelope is not None:
                try:
                    view = self._cable.load(source_id)
                    row["_source_reference_exists"] = (
                        str(row["source_endpoint"]) in ("from", "to")
                        and any(
                            segment.id == str(row["source_object_ref"])
                            for segment in view.document.segments
                        )
                    )
                except Exception:
                    row["_source_reference_exists"] = False
            else:
                row["_source_reference_exists"] = False

        staged_target = staged_members.get(target_id)
        if staged_target is not None and staged_target[0] == "pid":
            row["_target_revision"] = staged_target[1].revision
            row["_target_object_exists"] = any(
                element.id == str(row["target_object_ref"])
                for element in staged_target[1].elements
            )
        else:
            stored = self._store.get(target_id)
            row["_target_revision"] = (
                None if stored is None else stored.document.revision
            )
            if stored is not None:
                row["_target_object_exists"] = any(
                    element.id == str(row["target_object_ref"])
                    for element in stored.document.elements
                )
            else:
                row["_target_object_exists"] = False
        return row

    # ---- deterministic per-domain diffs ----

    @staticmethod
    def _pid_diff(before, after) -> dict[str, Any]:
        before_ids = {element.id for element in before.elements}
        after_ids = {element.id for element in after.elements}
        changed = sorted(
            element.id
            for element in after.elements
            if element.id in before_ids
            and element not in [e for e in before.elements if e.id == element.id]
        )
        payload = _canonical(
            [element.model_dump(mode="json") for element in after.elements]
        )
        return {
            "added": sorted(after_ids - before_ids),
            "updated": changed,
            "deleted": sorted(before_ids - after_ids),
            "content_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def _cable_diff(before, after) -> dict[str, Any]:
        before_ids = {segment.id for segment in before.segments}
        after_ids = {segment.id for segment in after.segments}
        payload = _canonical(after.model_dump(mode="json"))
        return {
            "added": sorted(after_ids - before_ids),
            "removed": sorted(before_ids - after_ids),
            "content_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        }
