"""The §E2 mechanical gate: M5 must not quietly move M4's validation semantics.

Self-repair is only as trustworthy as the judgement it repairs *towards*. If M5 were allowed to
soften a rule, drop a required validator, lower a default threshold or reshape the canonical
payload, every success rate it reported afterwards would be measuring a different system — and
the drift would be invisible, because the repair code and the rules it leans on ship together.

So the claim is made checkable instead of promised. :func:`build_manifest` projects the parts of
the canonical layer that decide findings into one document, and :func:`run_gate` compares that
document against the manifest frozen at the M4 accepted commit. Two things are compared:

* the *declarations* — rule catalog (id, validator, code, default severity, threshold), the
  configurable defaults, the built-in required validators and release policy, validator versions,
  and the canonical schema/version;
* the *conclusions* — a frozen fixture corpus' semantic output projection: for each fixture, the
  sorted set of ``(validator, rule, code, severity, locators)`` the engine actually produced.

The second half is what makes the first half more than paperwork. A rule can keep its id and
severity and still start firing on different drawings; the corpus projection notices that.

Only M4 APIs are used here, and deliberately so: this module is copied into the accepted tree to
freeze the baseline, which would be impossible if it depended on anything M5 introduced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    AddElementOperation,
    CreateDocumentRequest,
    Point,
    SymbolElement,
    TransactionRequest,
    UpdateElementOperation,
)
from .service import DocumentService
from .store import SQLiteDocumentStore
from .symbols import SymbolRegistry
from .validation_engine import VALIDATION_ENGINE_VERSION, VALIDATORS, run_validation
from .validation_profile import (
    BUILT_IN_PROFILE_ID,
    BUILT_IN_PROFILE_VERSION,
    built_in_profile,
    resolve_profile,
)
from .validation_rules import DEFAULT_QUALITY_SCORE_THRESHOLD, RULE_CATALOG, VALIDATOR_IDS

#: The M4 commit whose semantics M5 is not allowed to move. Recorded in the manifest itself, so
#: a manifest can never be mistaken for "whatever the rules happened to be last week".
M4_ACCEPTED_SHA = "ecedc00ae3063a4043334bd30367008d00665a29"

MANIFEST_VERSION = "1"
FROZEN_DIR = Path(__file__).with_name("frozen")
FROZEN_MANIFEST = FROZEN_DIR / f"m4-validation-semantics-{M4_ACCEPTED_SHA[:7]}.json"


# -- the fixture corpus ------------------------------------------------------ #


def _symbol(element_id: str, registry: SymbolRegistry, key: str, x: float, y: float, label: str) -> SymbolElement:
    definition = registry.get(key)
    return SymbolElement(
        id=element_id,
        symbol_key=key,
        position=Point(x=x, y=y),
        width=definition.width,
        height=definition.height,
        label=label,
        properties={"tag": label},
    )


def _apply_semantic(
    service: DocumentService, document_id: str, operations: list[Any], label: str
) -> None:
    """Build fixture content the way the product does: semantics in, governed write out.

    The fixture corpus is only worth freezing if it is made of drawings the real path can
    produce, so it goes through the semantic compiler rather than around it.
    """

    from .agent_semantic_models import SemanticTransaction
    from .semantic_compiler_engine import SemanticTransactionCompiler

    document = service.get_document(document_id)
    compiled = SemanticTransactionCompiler(service).compile(
        document_id,
        SemanticTransaction(
            operations=operations, expected_revision=document.revision, label=label
        ),
    )
    assert compiled.assessment.valid and compiled.transaction is not None, [
        issue.message for issue in compiled.assessment.issues
    ]
    service.apply_transaction(document_id, compiled.transaction, source="system")


def _valve_pair(service: DocumentService, registry: SymbolRegistry, document_id: str, tags: tuple[str, str]) -> None:
    from .agent_semantic_models import ConnectPortsOperation

    _apply_semantic(
        service,
        document_id,
        [
            AddElementOperation(element=_symbol("v1", registry, "ball_valve", 200, 300, tags[0])),
            AddElementOperation(element=_symbol("v2", registry, "ball_valve", 700, 300, tags[1])),
            ConnectPortsOperation(
                connector_id="p1",
                source_element_id="v1",
                source_port_id="out",
                target_element_id="v2",
                target_port_id="in",
                process_tag="L-1",
                medium="process",
                nominal_diameter="DN50",
            ),
        ],
        "invariance.valve-pair",
    )


def _two_valves(service: DocumentService, registry: SymbolRegistry, document_id: str) -> None:
    _valve_pair(service, registry, document_id, ("HV-101", "HV-102"))


def _untagged_pair(service: DocumentService, registry: SymbolRegistry, document_id: str) -> None:
    _valve_pair(service, registry, document_id, ("HV-101", "HV-101"))


def _dangling_endpoint(service: DocumentService, registry: SymbolRegistry, document_id: str) -> None:
    _two_valves(service, registry, document_id)
    document = service.get_document(document_id)
    service.apply_transaction(
        document_id,
        TransactionRequest(
            operations=[UpdateElementOperation(element_id="p1", patch={"target": None})],
            expected_revision=document.revision,
            label="invariance.dangling",
        ),
        source="system",
    )


def _colliding_pair(service: DocumentService, registry: SymbolRegistry, document_id: str) -> None:
    """Two valves on top of each other.

    Staged at the low level on purpose: the semantic compiler *refuses* to create an overlapping
    pair (it is a drafting-quality error), so a fixture that has to make the rule fire must model
    a drawing that arrived from somewhere else — an import, or a defect introduced by hand.
    """

    document = service.get_document(document_id)
    service.apply_transaction(
        document_id,
        TransactionRequest(
            operations=[
                AddElementOperation(
                    element=_symbol("v1", registry, "ball_valve", 660, 280, "HV-101")
                ),
                AddElementOperation(
                    element=_symbol("v2", registry, "ball_valve", 680, 300, "HV-102")
                ),
            ],
            expected_revision=document.revision,
            label="invariance.collision",
        ),
        source="system",
    )


def _empty(service: DocumentService, registry: SymbolRegistry, document_id: str) -> None:
    return None


FIXTURES: tuple[tuple[str, str, Any], ...] = (
    ("clean_pair", "two tagged valves joined by a line", _two_valves),
    ("duplicate_tags", "two valves sharing one tag", _untagged_pair),
    ("dangling_endpoint", "a line whose target endpoint was cleared", _dangling_endpoint),
    ("colliding_pair", "two overlapping valves", _colliding_pair),
    ("empty_drawing", "no elements at all", _empty),
)


def fixture_projection(service: DocumentService, registry: SymbolRegistry) -> list[dict[str, Any]]:
    """The semantic output of every fixture: what the rules *concluded*, in canonical form.

    Hashes, timestamps and document identity are deliberately out: they move with the machine,
    and the claim being frozen is about conclusions. Locators are kept, because "the same code on
    a different element" is a different conclusion.
    """

    profile = resolve_profile(built_in_profile())
    projection: list[dict[str, Any]] = []
    for fixture_id, title, builder in FIXTURES:
        document = service.create_document(
            CreateDocumentRequest(name=f"M4 invariance fixture: {fixture_id}")
        )
        builder(service, registry, document.id)
        result = run_validation(
            service.get_document(document.id), registry, profile, service=service
        )
        issues = sorted(
            {
                (
                    issue.validator_id,
                    issue.rule_id,
                    issue.code,
                    issue.severity,
                    tuple(sorted(issue.element_ids)),
                    tuple(sorted(issue.object_ids)),
                )
                for issue in result.issues
            }
        )
        projection.append(
            {
                "fixture_id": fixture_id,
                "title": title,
                "validators_run": sorted(result.validators_run),
                "validators_skipped": sorted(
                    {record.validator_id for record in result.validators_skipped}
                ),
                "issues": [
                    {
                        "validator_id": validator_id,
                        "rule_id": rule_id,
                        "code": code,
                        "severity": severity,
                        "element_ids": list(element_ids),
                        "object_ids": list(object_ids),
                    }
                    for validator_id, rule_id, code, severity, element_ids, object_ids in issues
                ],
            }
        )
    return projection


# -- the manifest ------------------------------------------------------------ #


def build_manifest(service: DocumentService | None = None, registry: SymbolRegistry | None = None) -> dict[str, Any]:
    """Rebuild the semantics manifest from the code as it is *right now*."""

    import tempfile

    registry = registry or SymbolRegistry()
    temporary = None
    if service is None:
        temporary = tempfile.TemporaryDirectory()
        service = DocumentService(SQLiteDocumentStore(Path(temporary.name) / "invariance.db"), registry)
    profile = resolve_profile(built_in_profile())
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "m4_accepted_sha": M4_ACCEPTED_SHA,
        "engine_version": VALIDATION_ENGINE_VERSION,
        "profile": {"id": BUILT_IN_PROFILE_ID, "version": BUILT_IN_PROFILE_VERSION},
        "canonical": {
            "validation_result_schema": "pid-agent.validation-result",
            "release_readiness_schema": "pid-agent.release-readiness",
            "rule_catalog_size": len(RULE_CATALOG),
        },
        "rule_catalog": [
            {
                "rule_id": rule.rule_id,
                "validator_id": rule.validator_id,
                "code": rule.code,
                "default_severity": rule.default_severity,
                "threshold": rule.threshold,
            }
            for rule in RULE_CATALOG
        ],
        "validator_ids": sorted(VALIDATOR_IDS),
        "enabled_rules": sorted(
            rule_id for rule_id, rule in profile.rules.items() if rule.enabled
        ),
        "disabled_rules": sorted(
            rule_id for rule_id, rule in profile.rules.items() if not rule.enabled
        ),
        "validator_versions": {
            definition.validator_id: definition.version
            for definition in sorted(VALIDATORS, key=lambda item: item.validator_id)
        },
        "thresholds": {
            "quality_score": DEFAULT_QUALITY_SCORE_THRESHOLD,
            "rule_thresholds": {
                rule_id: rule.threshold
                for rule_id, rule in sorted(profile.rules.items())
                if rule.threshold is not None
            },
        },
        "release_policy": _release_policy(profile),
        "fixture_projections": fixture_projection(service, registry),
    }
    if temporary is not None:
        temporary.cleanup()
    return manifest


def _release_policy(profile: Any) -> dict[str, Any]:
    """The built-in release policy, as a reviewer would read it off the profile."""

    from .release_validator import RELEASE_VALIDATOR_VERSION

    policy = profile.release_policy
    return {
        "release_validator_version": RELEASE_VALIDATOR_VERSION,
        "fail_on": sorted(policy.fail_on),
        "required_validators": sorted(policy.required_validators),
        "states": ["eligible", "not_eligible"],
        "human_approval_required": True,
    }


def load_frozen_manifest(path: Path | None = None) -> dict[str, Any]:
    manifest_path = path or FROZEN_MANIFEST
    return json.loads(manifest_path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class InvarianceFinding:
    path: str
    frozen: Any
    current: Any


@dataclass(frozen=True)
class InvarianceReport:
    ok: bool
    frozen_sha: str
    findings: list[InvarianceFinding]

    def codes(self) -> list[str]:
        return [finding.path for finding in self.findings]

    def payload(self) -> dict[str, Any]:
        return {
            "gate": "m4_validation_semantics_invariance",
            "ok": self.ok,
            "m4_accepted_sha": self.frozen_sha,
            "finding_count": len(self.findings),
            "findings": [
                {"path": finding.path, "frozen": finding.frozen, "current": finding.current}
                for finding in self.findings[:40]
            ],
        }


def _join(prefix: str, suffix: str) -> str:
    return f"{prefix}.{suffix}" if prefix else suffix


def _diff(prefix: str, frozen: Any, current: Any, findings: list[InvarianceFinding]) -> None:
    """Walk two manifests and record every place they disagree, path by path.

    A path is the point of the report: "rule_catalog[3].default_severity went from error to
    info" is reviewable, whereas "the manifests differ" is not.
    """

    if type(frozen) is not type(current):
        findings.append(InvarianceFinding(prefix, frozen, current))
        return
    if isinstance(frozen, dict):
        for key in sorted(set(frozen) | set(current)):
            path = _join(prefix, key)
            if key not in frozen:
                findings.append(InvarianceFinding(path, None, current[key]))
            elif key not in current:
                findings.append(InvarianceFinding(path, frozen[key], None))
            else:
                _diff(path, frozen[key], current[key], findings)
        return
    if isinstance(frozen, list):
        if len(frozen) != len(current):
            findings.append(InvarianceFinding(_join(prefix, "length"), len(frozen), len(current)))
        for index, (left, right) in enumerate(zip(frozen, current, strict=False)):
            _diff(f"{prefix}[{index}]", left, right, findings)
        return
    if frozen != current:
        findings.append(InvarianceFinding(prefix, frozen, current))


def compare_manifests(frozen: dict[str, Any], current: dict[str, Any]) -> list[InvarianceFinding]:
    findings: list[InvarianceFinding] = []
    _diff("", frozen, current, findings)
    return findings


def run_gate(path: Path | None = None) -> InvarianceReport:
    """Regenerate the manifest and compare it with the frozen one."""

    frozen = load_frozen_manifest(path)
    current = build_manifest()
    findings = compare_manifests(frozen, current)
    return InvarianceReport(
        ok=not findings,
        frozen_sha=str(frozen.get("m4_accepted_sha", "")),
        findings=findings,
    )


__all__ = [
    "FIXTURES",
    "FROZEN_MANIFEST",
    "M4_ACCEPTED_SHA",
    "MANIFEST_VERSION",
    "InvarianceFinding",
    "InvarianceReport",
    "build_manifest",
    "compare_manifests",
    "fixture_projection",
    "load_frozen_manifest",
    "run_gate",
]
