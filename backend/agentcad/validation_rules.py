"""The rule catalog: which rules exist, where they come from, and their defaults.

A rule id is ``<validator_id>.<code>`` (for example ``diagram-quality.NODE_OVERLAP``), and
the wildcard ``<validator_id>.*`` addresses every rule of one validator. The ids are the
stable handle a project profile uses to enable/disable a rule, restate its severity or
move a numeric threshold — so a profile can be written today and still mean the same thing
after a release.

The catalog is *data*, not logic: the checks live where they always lived (diagram
quality, engineering IR, engineering reports) and are reached through adapters in
``validation_engine``. That split is deliberate — M4 standardises the output contract
first and does not rewrite rule algorithms (Charter §41/§43, remote baseline §7).

Every ``default_severity`` below was taken from the legacy check that emits the code, not
chosen afresh. That is what makes adapter parity a test rather than an aspiration: with
the built-in profile, the canonical result must say exactly what the pre-M4 validators
said. A profile may restate a severity; the default may not drift on its own.

Unknown codes are never dropped: an adapter that meets a code the catalog does not know
still reports it (with the adapter's own severity) and marks it unregistered, because
silently discarding a finding is the one failure mode a validation contract must not have.
"""

from __future__ import annotations

from dataclasses import dataclass

from .validation_models import ValidationSeverity


@dataclass(frozen=True)
class RuleSpec:
    """One rule, as the profile layer sees it."""

    rule_id: str
    validator_id: str
    code: str
    default_severity: ValidationSeverity
    #: Numeric threshold a profile may move (``None`` when the rule has no number).
    threshold: float | None = None
    threshold_help: str = ""
    description: str = ""


#: Diagram-quality score gate. The drafting engine uses the same number, so a profile
#: that moves it is stating a project decision, not tweaking a display.
DEFAULT_QUALITY_SCORE_THRESHOLD = 95.0


def _rules(validator_id: str, entries: list[tuple[str, ValidationSeverity]]) -> list[RuleSpec]:
    return [
        RuleSpec(
            rule_id=f"{validator_id}.{code}",
            validator_id=validator_id,
            code=code,
            default_severity=severity,
        )
        for code, severity in entries
    ]


ENGINEERING_GRAPH_RULES: tuple[RuleSpec, ...] = tuple(
    _rules(
        "engineering-graph",
        [
            ("IR_IDENTITY_COLLISION", "error"),
            ("IR_DUPLICATE_IDENTITY", "error"),
            ("IR_ENDPOINT_ELEMENT_MISSING", "error"),
            ("IR_SYMBOL_DEFINITION_MISSING", "error"),
            ("IR_OPC_CONNECTION_ID_DUPLICATE", "error"),
            ("IR_ORPHAN_LINE", "warning"),
            ("IR_OPC_TARGET_MISSING", "warning"),
            ("IR_SIGNAL_WITHOUT_INSTRUMENT", "warning"),
            ("IR_ISOLATED_OBJECT", "info"),
            ("IR_SIGNAL_UNNAMED", "info"),
        ],
    )
)

ENGINEERING_REPORT_RULES: tuple[RuleSpec, ...] = tuple(
    _rules(
        "engineering-report",
        [
            ("TAG_MISSING", "warning"),  # engineering_reports._rule_findings
            ("TAG_DUPLICATE", "error"),
            ("SYMBOL_DEFINITION_MISSING", "error"),
            ("SYMBOL_REQUIRED_PORT_UNCONNECTED", "warning"),
            ("LINE_TAG_MISSING", "warning"),
            ("LINE_MEDIUM_MISSING", "warning"),
            ("LINE_DIAMETER_MISSING", "warning"),
            ("CONNECTOR_ENDPOINT_DANGLING", "error"),
            ("CONNECTOR_ENDPOINT_ELEMENT_MISSING", "error"),
            ("CONNECTOR_ENDPOINT_PORT_MISSING", "error"),
            ("CONNECTOR_ENDPOINT_INVALID_ELEMENT_TYPE", "error"),
            ("CONNECTOR_ENDPOINT_POINT_MISMATCH", "error"),
        ],
    )
)

DIAGRAM_QUALITY_RULES: tuple[RuleSpec, ...] = tuple(
    _rules(
        "diagram-quality",
        [
            ("NON_ORTHOGONAL_SEGMENT", "error"),
            ("MICRO_SEGMENT", "error"),
            ("UNNECESSARY_BEND", "error"),
            ("EXCESSIVE_BENDS", "warning"),
            ("NODE_OVERLAP", "error"),
            ("PIPE_THROUGH_EQUIPMENT", "error"),
            ("UNBRIDGED_CROSSING", "error"),
            ("PORT_DIRECTION_MISMATCH", "error"),
            ("PORT_EXIT_MISMATCH", "error"),
            ("PORT_FACING_MISMATCH", "error"),
            ("SYMBOL_OUT_OF_BOUNDS", "warning"),
            ("CONNECTOR_OUT_OF_BOUNDS", "error"),
            ("DUPLICATE_LABEL", "error"),
            ("ANNOTATION_OVERLAP", "error"),
            # The one threshold rule: a profile can raise or lower the required score.
            ("QUALITY_SCORE_BELOW_TARGET", "error"),
        ],
    )
)

RULE_CATALOG: tuple[RuleSpec, ...] = (
    *DIAGRAM_QUALITY_RULES,
    *ENGINEERING_GRAPH_RULES,
    *ENGINEERING_REPORT_RULES,
)

RULES_BY_ID: dict[str, RuleSpec] = {rule.rule_id: rule for rule in RULE_CATALOG}
VALIDATOR_IDS: tuple[str, ...] = tuple(
    dict.fromkeys(rule.validator_id for rule in RULE_CATALOG)
)

#: Thresholds a profile can move, with their defaults. Kept next to the catalog so
#: "which numbers are configurable" is answerable from one place.
RULE_THRESHOLDS: dict[str, float] = {
    "diagram-quality.QUALITY_SCORE_BELOW_TARGET": DEFAULT_QUALITY_SCORE_THRESHOLD,
}


def rule_id_for(validator_id: str, code: str) -> str:
    """The rule id an issue with this code belongs to (registered or not)."""

    candidate = f"{validator_id}.{code}"
    return candidate
