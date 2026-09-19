"""Project profiles and rule precedence (Charter §12, §13; remote baseline R2 §2, §6).

A drawing is validated against *rules*, and which rules are in force is a project
decision, not a global constant. This module is where that decision is expressed and,
more importantly, where it is made *auditable*: every rule resolves to exactly one
effective setting, and every effective setting remembers which layer supplied it, so each
issue can answer "why is this rule in force on this drawing?".

Precedence, weakest first (Charter §12, §13):

    built-in domain defaults
    < adopted standard profile
    < company / owner profile
    < project design rules
    < release-phase / drawing overrides, and only when the project permits them

Three properties matter more than the feature list:

* **Configuration is server-side.** Profiles are loaded from a file the deployment owns
  (``PID_AGENT_VALIDATION_PROFILE``), never from a request body. That is what stops an
  Agent from disabling the rule that is about to fail it, or from granting itself a waiver
  that would amount to self-approval of a release (Charter §44-10).
* **Invalid configuration is rejected, not ignored.** An unknown layer, a layer out of
  order, an override for a rule that does not exist, an unknown required validator, a
  duplicated waiver id or a threshold outside its domain all raise :class:`ProfileError`.
  A typo that silently creates an unregistered rule is worse than a loud error, so there
  is no "unknown exact rule" escape hatch (remote baseline R2 P0-2).
* **The fingerprint covers scope, not just identity.** A waiver's ``object_ids`` /
  ``element_ids`` are semantics: two approvals that cover different equipment are not the
  same rule bundle (remote baseline R2 P0-3).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from .validation_models import (
    LAYER_RANK,
    PROFILE_PRECEDENCE,
    ContractModel,
    ProfileLayerName,
    ReleasePolicy,
    ValidationSeverity,
    Waiver,
)
from .validation_rules import RULE_CATALOG, RULE_THRESHOLDS, RULES_BY_ID, VALIDATOR_IDS

PROFILE_PATH_ENV = "PID_AGENT_VALIDATION_PROFILE"
PROFILE_SCHEMA = "pid-agent.validation-profile"

BUILT_IN_PROFILE_ID = "built-in"
BUILT_IN_PROFILE_VERSION = "1"


class ProfileError(ValueError):
    """A profile that cannot be trusted to mean what it says."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RuleOverride(ContractModel):
    """One typed, reviewable change to one rule."""

    #: ``<validator-id>.<CODE>`` for one rule, or ``<validator-id>.*`` for a whole set.
    rule_id: str = Field(min_length=1)
    enabled: bool | None = None
    severity: ValidationSeverity | None = None
    #: Rejected if not finite: NaN and Infinity are not thresholds, they are bugs that
    #: would silently disable (NaN compares false against everything) a release gate.
    threshold: float | None = Field(default=None, allow_inf_nan=False)


class ProfileLayer(ContractModel):
    """One step of the precedence chain."""

    layer: ProfileLayerName
    #: Human identity of the source, e.g. ``ISA-5.1`` or ``company:MSR``.
    source: str = Field(min_length=1)
    rules: list[RuleOverride] = Field(default_factory=list)


class ValidationProfile(ContractModel):
    """The document a deployment points ``PID_AGENT_VALIDATION_PROFILE`` at."""

    schema_name: Literal["pid-agent.validation-profile"] = Field(
        default=PROFILE_SCHEMA, alias="schema"
    )
    version: int = 1
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    layers: list[ProfileLayer] = Field(default_factory=list)
    waivers: list[Waiver] = Field(default_factory=list)
    release_policy: ReleasePolicy = Field(default_factory=ReleasePolicy)


class EffectiveRule(ContractModel):
    """The resolved state of one rule, with the layer that decided it."""

    rule_id: str
    validator_id: str
    code: str
    enabled: bool
    severity: ValidationSeverity
    threshold: float | None = None
    rule_source: str
    registered: bool = True


class EffectiveProfile(ContractModel):
    """Everything the engine needs, and everything a reviewer needs to reproduce it."""

    profile_id: str
    profile_version: str
    source: str
    rules: dict[str, EffectiveRule]
    waivers: list[Waiver] = Field(default_factory=list)
    release_policy: ReleasePolicy = Field(default_factory=ReleasePolicy)
    fingerprint: str = ""

    def taints(self) -> dict[str, Any]:
        """The canonical form the fingerprint is computed over.

        Sorted everywhere, including inside each waiver's scope, so that reordering a
        profile file — or listing the same elements in another order — cannot change the
        fingerprint while leaving the effective rules identical.
        """

        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "rules": [
                {
                    "rule_id": rule.rule_id,
                    "enabled": rule.enabled,
                    "severity": rule.severity,
                    "threshold": rule.threshold,
                    "rule_source": rule.rule_source,
                }
                for rule in sorted(self.rules.values(), key=lambda item: item.rule_id)
            ],
            "waivers": [
                {
                    "waiver_id": waiver.waiver_id,
                    "rule_id": waiver.rule_id,
                    "code": waiver.code,
                    "actor": waiver.actor,
                    "reason": waiver.reason,
                    "granted_at": waiver.granted_at.isoformat(),
                    "expires_at": waiver.expires_at.isoformat() if waiver.expires_at else None,
                    "max_revision": waiver.max_revision,
                    # Scope is semantics: two waivers with the same words but different
                    # covered equipment are different approvals.
                    "object_ids": sorted(waiver.object_ids),
                    "element_ids": sorted(waiver.element_ids),
                }
                for waiver in sorted(self.waivers, key=lambda item: item.waiver_id)
            ],
            "release_policy": {
                "required_validators": sorted(self.release_policy.required_validators),
                "fail_on": sorted(self.release_policy.fail_on),
                "allow_release_phase_overrides": (
                    self.release_policy.allow_release_phase_overrides
                ),
            },
        }


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def built_in_rules() -> dict[str, EffectiveRule]:
    rules: dict[str, EffectiveRule] = {}
    for rule in RULE_CATALOG:
        rules[rule.rule_id] = EffectiveRule(
            rule_id=rule.rule_id,
            validator_id=rule.validator_id,
            code=rule.code,
            enabled=True,
            severity=rule.default_severity,
            threshold=RULE_THRESHOLDS.get(rule.rule_id),
            rule_source="built-in",
        )
    return rules


def _expand_targets(rule_id: str) -> list[str]:
    """Resolve an override target against the catalog, refusing anything unknown.

    There is deliberately no fallback for an unknown exact rule id. A profile that names
    ``diagram-quality.DUPLICATE_LABELS`` (a typo) must fail loudly: registering it as a
    guessed rule would produce a profile that looks like it enforces something and does
    not.
    """

    if rule_id.endswith(".*"):
        validator_id = rule_id[: -len(".*")]
        targets = [
            rule.rule_id for rule in RULE_CATALOG if rule.validator_id == validator_id
        ]
        if not targets:
            raise ProfileError(
                "profile_unknown_validator",
                f"the profile targets validator {validator_id!r}, which declares no rules",
            )
        return targets
    if rule_id not in RULES_BY_ID:
        raise ProfileError(
            "profile_unknown_rule",
            f"the profile overrides rule {rule_id!r}, which is not in the rule catalog",
        )
    return [rule_id]


def _check_threshold_domain(rule_id: str, threshold: float) -> None:
    """Reject thresholds that cannot mean anything for this rule."""

    if not math.isfinite(threshold):
        raise ProfileError(
            "profile_threshold_not_finite",
            f"the threshold for {rule_id} must be a finite number, got {threshold!r}",
        )
    if rule_id == "diagram-quality.QUALITY_SCORE_BELOW_TARGET" and not 0.0 <= threshold <= 100.0:
        raise ProfileError(
            "profile_threshold_out_of_range",
            f"the drafting-quality score is a percentage, so the threshold for {rule_id} "
            f"must be between 0 and 100, got {threshold!r}",
        )


def resolve_profile(profile: ValidationProfile, *, source: str = "") -> EffectiveProfile:
    """Fold the layer chain into one effective rule set."""

    if profile.version != 1:
        raise ProfileError(
            "profile_version_unsupported",
            f"unsupported validation profile version {profile.version}",
        )
    unknown_required = sorted(set(profile.release_policy.required_validators) - set(VALIDATOR_IDS))
    if unknown_required:
        raise ProfileError(
            "profile_unknown_validator",
            "the release policy requires unknown validator(s): "
            + ", ".join(unknown_required),
        )
    seen_waivers: set[str] = set()
    for waiver in profile.waivers:
        if waiver.waiver_id in seen_waivers:
            raise ProfileError(
                "profile_duplicate_waiver",
                f"waiver id {waiver.waiver_id!r} appears twice; waiver evidence must be "
                "unambiguous",
            )
        seen_waivers.add(waiver.waiver_id)

    rules = built_in_rules()
    seen_layers: list[ProfileLayerName] = []
    for layer in profile.layers:
        if layer.layer in seen_layers:
            raise ProfileError(
                "profile_layer_duplicated",
                f"layer {layer.layer!r} appears twice; precedence must be unambiguous",
            )
        if seen_layers and LAYER_RANK[layer.layer] <= LAYER_RANK[seen_layers[-1]]:
            expected = " < ".join(PROFILE_PRECEDENCE)
            raise ProfileError(
                "profile_layer_out_of_order",
                f"layer {layer.layer!r} must come after {seen_layers[-1]!r} ({expected})",
            )
        if (
            layer.layer == "release-phase"
            and layer.rules
            and not profile.release_policy.allow_release_phase_overrides
        ):
            raise ProfileError(
                "profile_overrides_not_permitted",
                "the profile declares release-phase overrides but does not permit them "
                "(release_policy.allow_release_phase_overrides is false)",
            )
        seen_layers.append(layer.layer)
        for override in layer.rules:
            for target in _expand_targets(override.rule_id):
                current = rules[target]
                if override.threshold is not None:
                    if current.threshold is None:
                        raise ProfileError(
                            "profile_threshold_not_configurable",
                            f"{target} has no numeric threshold to configure",
                        )
                    _check_threshold_domain(target, override.threshold)
                rules[target] = current.model_copy(
                    update={
                        "enabled": (
                            override.enabled if override.enabled is not None else current.enabled
                        ),
                        "severity": override.severity or current.severity,
                        "threshold": (
                            override.threshold
                            if override.threshold is not None
                            else current.threshold
                        ),
                        "rule_source": f"{layer.layer}:{layer.source}",
                    }
                )
    effective = EffectiveProfile(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        source=source or profile.profile_id,
        rules=rules,
        waivers=list(profile.waivers),
        release_policy=profile.release_policy,
    )
    return effective.model_copy(update={"fingerprint": _fingerprint(effective.taints())})


def built_in_profile() -> ValidationProfile:
    """The default project profile: every rule on, every validator required.

    Requiring all three validators by default is the useful default for a release gate:
    with an empty requirement list, a validator that could not build its context would
    make nothing missing and the gate would pass on absent evidence. A project that wants
    a narrower gate says which validators it requires.
    """

    return ValidationProfile(
        profile_id=BUILT_IN_PROFILE_ID,
        profile_version=BUILT_IN_PROFILE_VERSION,
        release_policy=ReleasePolicy(
            required_validators=sorted(VALIDATOR_IDS),
        ),
    )


def load_profile(path: Path | str | None = None) -> EffectiveProfile:
    """Load and resolve the deployment's profile.

    No path (and no ``PID_AGENT_VALIDATION_PROFILE``) means the built-in defaults, which
    is exactly what the repository validates with today. A malformed file is an error, not
    a fallback: falling back to defaults would silently validate against the wrong rules.
    """

    resolved = path if path is not None else os.getenv(PROFILE_PATH_ENV, "")
    if not resolved:
        return resolve_profile(built_in_profile(), source="built-in")
    profile_path = Path(resolved)
    try:
        raw = profile_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(
            "profile_unreadable",
            f"the validation profile at {profile_path} could not be read: {exc}",
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProfileError(
            "profile_not_json",
            f"the validation profile at {profile_path} is not valid JSON: {exc}",
        ) from exc
    try:
        profile = ValidationProfile.model_validate(payload)
    except ValidationError as exc:
        raise ProfileError(
            "profile_invalid",
            f"the validation profile at {profile_path} is invalid: {exc}",
        ) from exc
    return resolve_profile(profile, source=str(profile_path))


__all__ = [
    "BUILT_IN_PROFILE_ID",
    "BUILT_IN_PROFILE_VERSION",
    "PROFILE_PATH_ENV",
    "PROFILE_SCHEMA",
    "EffectiveProfile",
    "EffectiveRule",
    "ProfileError",
    "ProfileLayer",
    "RuleOverride",
    "ValidationProfile",
    "built_in_profile",
    "built_in_rules",
    "load_profile",
    "resolve_profile",
]
