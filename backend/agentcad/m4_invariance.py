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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import (
    AddElementOperation,
    CreateDocumentRequest,
    Document,
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


#: ``polished`` records whether the fixture's drawing is produced by a full-diagram
#: transaction, i.e. whether the production annotation polish ran on it. It is not decoration:
#: the tag-resolution fix may only move conclusions on drawings the polish touched, so the gate
#: asserts exact invariance for every fixture whose ``polished`` is ``False``.
FIXTURES: tuple[tuple[str, str, Any, bool], ...] = (
    ("clean_pair", "two tagged valves joined by a line", _two_valves, True),
    ("duplicate_tags", "two valves sharing one tag", _untagged_pair, True),
    ("dangling_endpoint", "a line whose target endpoint was cleared", _dangling_endpoint, True),
    # Staged with a low-level write rather than the semantic compiler, so no polish runs and
    # the legacy ``symbol.label`` field survives: this corpus must not move at all.
    ("colliding_pair", "two overlapping valves", _colliding_pair, False),
    ("empty_drawing", "no elements at all", _empty, False),
)


def issue_key(issue: Any) -> str:
    """The identity of one conclusion: what fired, on what, and where.

    Rule *identity* rather than code, because a code emitted by the wrong validator is a
    different conclusion. Message text is deliberately not part of the key — it is compared
    separately, and only where the allowlist says a message may legitimately change.
    """

    return "|".join(
        [
            issue.validator_id,
            issue.rule_id,
            issue.code,
            *sorted(issue.element_ids),
            *sorted(issue.object_ids),
        ]
    )


def tag_sources(document: Document) -> dict[str, dict[str, str]]:
    """Resolved tag (and where it came from) per symbol.

    Recorded so the gate can *justify* a tag delta instead of assuming one: a disappearing
    ``TAG_MISSING`` is only the expected correction when the symbol it names really does resolve
    a tag, and a new ``TAG_DUPLICATE`` is only expected when the symbols it names really do share
    one. Otherwise the delta is unexplained, and unexplained is what the gate is for.
    """

    from .tag_resolver import describe_symbol_tag

    sources: dict[str, dict[str, str]] = {}
    for element in document.elements:
        if element.type != "symbol":
            continue
        described = describe_symbol_tag(document, element)
        sources[element.id] = {"tag": described.tag, "source": described.source}
    return sources


def fixture_projection(service: DocumentService, registry: SymbolRegistry) -> list[dict[str, Any]]:
    """The semantic output of every fixture: what the rules *concluded*, in canonical form.

    Hashes, timestamps and document identity are deliberately out: they move with the machine,
    and the claim being frozen is about conclusions. Locators are kept, because "the same code on
    a different element" is a different conclusion.

    Messages are kept too, keyed by finding identity. They are not decoration: one of the three
    changes the tag-resolution fix is allowed to make is a *display name*, and a projection that
    dropped messages could not tell that change apart from no change at all.

    ``tag_source`` and ``polished`` are recorded for the same reason — they are what makes the
    allowlist checkable rather than asserted. ``polished`` says whether the fixture's drawing
    went through the production polish, which is the property that decides whether a conclusion
    is *expected* to move.
    """

    profile = resolve_profile(built_in_profile())
    projection: list[dict[str, Any]] = []
    for fixture_id, title, builder, polished in FIXTURES:
        document = service.create_document(
            CreateDocumentRequest(name=f"M4 invariance fixture: {fixture_id}")
        )
        builder(service, registry, document.id)
        validated = service.get_document(document.id)
        result = run_validation(validated, registry, profile, service=service)
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
        messages: dict[str, list[str]] = {}
        for issue in result.issues:
            messages.setdefault(issue_key(issue), []).append(issue.message)
        projection.append(
            {
                "fixture_id": fixture_id,
                "title": title,
                "polished": polished,
                "validators_run": sorted(result.validators_run),
                "validators_skipped": sorted(
                    {record.validator_id for record in result.validators_skipped}
                ),
                "tag_source": tag_sources(validated),
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
                "messages": {
                    key: sorted(values) for key, values in sorted(messages.items())
                },
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
class FixtureDelta:
    """One fixture's diff, sorted into the three buckets the baseline's §E2 allowlist names.

    ``expected_corrected_delta`` is not a synonym for "a difference we tolerate". Every entry in
    it carries a reason that a reviewer can check against the fixture (this symbol now resolves a
    tag; these symbols now share one; only a display name moved), and any difference without such
    a reason lands in ``unexpected_delta`` — where a non-empty list means the gate fails.
    """

    fixture_id: str
    classification: str
    polished: bool
    reasons: list[str]
    unexpected: list[str]


@dataclass(frozen=True)
class InvarianceReport:
    ok: bool
    frozen_sha: str
    findings: list[InvarianceFinding]
    fixture_deltas: list[FixtureDelta] = field(default_factory=list)

    def codes(self) -> list[str]:
        return [finding.path for finding in self.findings]

    def _bucket(self, classification: str) -> list[dict[str, Any]]:
        return [
            {
                "fixture_id": delta.fixture_id,
                "polished": delta.polished,
                "reasons": delta.reasons,
                "unexpected": delta.unexpected,
            }
            for delta in self.fixture_deltas
            if delta.classification == classification
        ]

    def payload(self) -> dict[str, Any]:
        return {
            "gate": "m4_validation_semantics_invariance",
            "ok": self.ok,
            "m4_accepted_sha": self.frozen_sha,
            "declaration_finding_count": len(self.findings),
            "declaration_findings": [
                {"path": finding.path, "frozen": finding.frozen, "current": finding.current}
                for finding in self.findings[:40]
            ],
            "expected_corrected_delta": self._bucket("expected_corrected_delta"),
            "exactly_invariant": self._bucket("exactly_invariant"),
            "unexpected_delta": self._bucket("unexpected_delta"),
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


def compare_declarations(frozen: dict[str, Any], current: dict[str, Any]) -> list[InvarianceFinding]:
    """Compare only the declarations, leaving the fixture corpus to the allowlist classifier.

    The distinction matters. A declaration is a *promise about the rules* — which rules exist,
    what severity they carry, which validators are required — and no correction to a tag
    conclusion has any business changing one. A conclusion is an *observation about a drawing*,
    and this fix's whole point is that one observation was wrong.
    """

    findings: list[InvarianceFinding] = []
    for key in sorted(set(frozen) | set(current)):
        if key == "fixture_projections":
            continue
        if key not in frozen:
            findings.append(InvarianceFinding(key, None, current[key]))
        elif key not in current:
            findings.append(InvarianceFinding(key, frozen[key], None))
        else:
            _diff(key, frozen[key], current[key], findings)
    return findings


def _entry_key(entry: dict[str, Any]) -> str:
    return "|".join(
        [
            entry["validator_id"],
            entry["rule_id"],
            entry["code"],
            *sorted(entry["element_ids"]),
            *sorted(entry["object_ids"]),
        ]
    )


def _message_display_only(frozen_messages: list[str], current_messages: list[str]) -> bool:
    """Whether two message sets differ only in the display name in front of the same prose.

    The allowed shape is narrow on purpose: the tail from the first `` 的端口 `` on must be
    identical, and the part before it must be the symbol id on the frozen side (that is what the
    pre-fix code printed, because the label had been cleared) and non-empty on the current side.
    A different port, a different sentence or a dropped message all fail.
    """

    if len(frozen_messages) != len(current_messages):
        return False
    separator = " 的端口 "
    for before, after in zip(frozen_messages, current_messages, strict=True):
        if before == after:
            continue
        if separator not in before or separator not in after:
            return False
        before_head, before_tail = before.split(separator, 1)
        after_head, after_tail = after.split(separator, 1)
        if before_tail != after_tail:
            return False
        if not after_head.strip():
            return False
        if before_head == after_head:
            continue
    return True


def classify_fixture_delta(
    frozen: dict[str, Any], current: dict[str, Any]
) -> FixtureDelta:
    """Sort one fixture's diff into expected / unexpected, with a checkable reason for each.

    The allowlist is the one the release baseline fixes for this regression fix:

    * a disappearing ``engineering-report.TAG_MISSING`` whose element now resolves a tag;
    * a new ``engineering-report.TAG_DUPLICATE`` whose elements now resolve the same tag;
    * a ``SYMBOL_REQUIRED_PORT_UNCONNECTED`` finding whose identity is unchanged and whose
      message changed only by substituting the resolved tag for the element id.

    Anything else — a different severity, a different locator, a different validator, a
    changed validator run/skip set, a delta on a corpus that never went through the polish — is
    unexpected, and a single unexpected delta fails the gate.
    """

    fixture_id = str(current.get("fixture_id", frozen.get("fixture_id", "?")))
    polished = bool(current.get("polished", frozen.get("polished", False)))
    reasons: list[str] = []
    unexpected: list[str] = []

    frozen_entries = {_entry_key(entry): entry for entry in frozen.get("issues", [])}
    current_entries = {_entry_key(entry): entry for entry in current.get("issues", [])}

    removed = [frozen_entries[key] for key in sorted(set(frozen_entries) - set(current_entries))]
    added = [current_entries[key] for key in sorted(set(current_entries) - set(frozen_entries))]

    sources: dict[str, dict[str, str]] = current.get("tag_source", {})

    for entry in removed:
        if entry["validator_id"] == "engineering-report" and entry["code"] == "TAG_MISSING":
            unresolved = [
                element_id
                for element_id in entry["element_ids"]
                if sources.get(element_id, {}).get("source", "none") == "none"
            ]
            if unresolved:
                unexpected.append(
                    f"removed TAG_MISSING on {entry['element_ids']} but {unresolved} resolve no tag"
                )
            else:
                reasons.append(
                    f"TAG_MISSING 消失：{entry['element_ids']} 现在能解析到位号"
                    f"（来源 {[sources.get(i, {}).get('source') for i in entry['element_ids']]}）"
                )
        else:
            unexpected.append(
                f"removed {entry['validator_id']}.{entry['code']} on {entry['element_ids']}"
            )

    for entry in added:
        if entry["validator_id"] == "engineering-report" and entry["code"] == "TAG_DUPLICATE":
            tags = {sources.get(element_id, {}).get("tag", "") for element_id in entry["element_ids"]}
            if len(tags) == 1 and "" not in tags:
                reasons.append(
                    f"TAG_DUPLICATE 新增：{entry['element_ids']} 解析出同一个位号 {sorted(tags)[0]!r}"
                )
            else:
                unexpected.append(
                    f"added TAG_DUPLICATE on {entry['element_ids']} but the resolved tags are {tags}"
                )
        else:
            unexpected.append(
                f"added {entry['validator_id']}.{entry['code']} on {entry['element_ids']}"
            )

    # A finding that exists on both sides must be *identical* on both sides. The key covers
    # validator/rule/code/locators, so this comparison is what catches a severity that moved or
    # any other field a future change might add — the allowlist is about which conclusions
    # appear and disappear, never about one conclusion quietly changing shape.
    for key in sorted(set(frozen_entries) & set(current_entries)):
        if frozen_entries[key] != current_entries[key]:
            unexpected.append(
                f"finding changed shape: {frozen_entries[key]} -> {current_entries[key]}"
            )

    # Messages are compared only for findings that exist on *both* sides. A finding that was
    # added or removed already had its message accounted for when the finding itself was judged,
    # and counting it twice would turn every allowed correction into an unexplained one.
    frozen_messages = frozen.get("messages", {})
    current_messages = current.get("messages", {})
    shared = set(frozen_entries) & set(current_entries)
    for key in sorted(shared):
        before = frozen_messages.get(key)
        after = current_messages.get(key)
        if before == after:
            continue
        if before is None or after is None:
            unexpected.append(f"message projection missing on one side for {key!r}")
            continue
        entry = current_entries.get(key) or {}
        if (
            entry.get("validator_id") == "engineering-report"
            and entry.get("code") == "SYMBOL_REQUIRED_PORT_UNCONNECTED"
            and _message_display_only(before, after)
        ):
            reasons.append(f"SYMBOL_REQUIRED_PORT_UNCONNECTED 展示名改用解析位号（{key}）")
        else:
            unexpected.append(f"message changed for {key!r}: {before!r} -> {after!r}")

    for field_name in ("validators_run", "validators_skipped"):
        before = frozen.get(field_name, [])
        after = current.get(field_name, [])
        if before != after:
            unexpected.append(f"{field_name} changed: {before} -> {after}")

    # A corpus that never went through the polish kept its legacy label field, so the tag
    # source never moved and nothing about it may change. Proving that is the point of keeping
    # such a corpus in the set at all.
    if not polished and (removed or added or reasons or unexpected):
        unexpected.append("a corpus that never went through the production polish changed")
        reasons = []

    if unexpected:
        classification = "unexpected_delta"
    elif reasons:
        classification = "expected_corrected_delta"
    else:
        classification = "exactly_invariant"
    return FixtureDelta(
        fixture_id=fixture_id,
        classification=classification,
        polished=polished,
        reasons=reasons,
        unexpected=unexpected,
    )


def run_gate(path: Path | None = None) -> InvarianceReport:
    """Regenerate the manifest, then apply the allowlist to what moved.

    Declarations are compared strictly. Fixtures are classified, so the report can say which
    corrections were *expected* and justified rather than only that something differs.
    """

    frozen = load_frozen_manifest(path)
    current = build_manifest()
    findings = compare_declarations(frozen, current)

    frozen_fixtures = {item["fixture_id"]: item for item in frozen.get("fixture_projections", [])}
    current_fixtures = {item["fixture_id"]: item for item in current.get("fixture_projections", [])}
    deltas: list[FixtureDelta] = []
    for fixture_id in sorted(set(frozen_fixtures) | set(current_fixtures)):
        if fixture_id not in frozen_fixtures or fixture_id not in current_fixtures:
            deltas.append(
                FixtureDelta(
                    fixture_id=fixture_id,
                    classification="unexpected_delta",
                    polished=bool(
                        current_fixtures.get(fixture_id, frozen_fixtures.get(fixture_id, {}))
                        .get("polished", False)
                    ),
                    reasons=[],
                    unexpected=["fixture appears in only one of the two manifests"],
                )
            )
            continue
        deltas.append(
            classify_fixture_delta(frozen_fixtures[fixture_id], current_fixtures[fixture_id])
        )

    ok = not findings and not any(
        delta.classification == "unexpected_delta" for delta in deltas
    )
    return InvarianceReport(
        ok=ok,
        frozen_sha=str(frozen.get("m4_accepted_sha", "")),
        findings=findings,
        fixture_deltas=deltas,
    )


__all__ = [
    "FIXTURES",
    "FROZEN_MANIFEST",
    "M4_ACCEPTED_SHA",
    "MANIFEST_VERSION",
    "FixtureDelta",
    "InvarianceFinding",
    "InvarianceReport",
    "build_manifest",
    "classify_fixture_delta",
    "compare_declarations",
    "compare_manifests",
    "fixture_projection",
    "issue_key",
    "load_frozen_manifest",
    "run_gate",
    "tag_sources",
]
