"""The semantic spec store: the source of truth the next sentence edits.

A drawing revision is output and evidence; the :class:`DiagramSpec` that produced it is
the product state the next natural-language sentence modifies. The store is deliberately
narrow -- one append-only row per (document, revision), written only as the atomic
companion of the governed revision write, so a drawing and its semantic source can never
disagree about having happened.

Two refusals are part of the design, both frozen by the design gate:

* A revision that predates the store has no recoverable source. The spec is **not**
  reconstructed from ``DrawingElements`` (lossy) and **not** guessed back from an audit
  digest (a digest names a spec; it is not one): the answer is
  ``semantic_source_unavailable``.
* A stored spec whose recorded chain versions no longer match what this code would use
  to rebuild its drawing is not silently recomputed under new rules: the answer is
  ``semantic_source_version_requires_migration``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .m7_diagram_spec import DiagramSpec, load_diagram_spec

SPEC_STORE_SCHEMA_VERSION = "m7-semantic-spec-store/1"

#: The provenance version fields that must match for a stored spec's drawing to be
#: rebuilt and reconciled exactly. Recorded with every row so the rebuild answers to the
#: rules the spec was drawn under, not whatever the code says today.
CHAIN_VERSION_FIELDS: tuple[str, ...] = (
    "diagram_spec_schema_version",
    "adapter_topology_digest_version",
    "symbol_geometry_catalog_digest_version",
    "layout_digest_version",
    "layout_projection_version",
    "materializer_version",
    "materialization_digest_version",
)


class SemanticSourceError(ValueError):
    """The semantic source of a drawing could not be read back safely."""


class SemanticSourceUnavailableError(SemanticSourceError):
    """No spec row exists for this revision: the source was never stored, and v1 refuses
    to reconstruct it from the drawing or from audit digests."""

    code = "semantic_source_unavailable"


class SemanticSourceVersionError(SemanticSourceError):
    """The stored spec records chain versions this code no longer runs: the drawing must
    be migrated, not silently recomputed under new rules."""

    code = "semantic_source_version_requires_migration"


@dataclass(frozen=True)
class SemanticSpecRecord:
    """One append-only source row, as committed beside one document revision."""

    document_id: str
    revision: int
    spec_schema_version: str
    spec: DiagramSpec
    spec_digest: str
    chain_versions: dict[str, str]

    def model(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "revision": self.revision,
            "spec_schema_version": self.spec_schema_version,
            "spec_digest": self.spec_digest,
            "chain_versions": dict(self.chain_versions),
        }


def spec_payload(spec: DiagramSpec) -> str:
    """The canonical JSON the digest and the row both read from: one spelling per spec."""

    return json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def spec_digest(spec: DiagramSpec) -> str:
    return hashlib.sha256(spec_payload(spec).encode("utf-8")).hexdigest()


def build_spec_record(
    spec: DiagramSpec,
    *,
    document_id: str,
    revision: int,
    chain_versions: dict[str, str],
) -> SemanticSpecRecord:
    """The row a governed write commits beside the revision.

    ``chain_versions`` is asserted, not trusted: every field the rebuild needs must be
    present and none may be empty, because a silent default here is exactly the version
    drift the record exists to catch.
    """

    missing = [name for name in CHAIN_VERSION_FIELDS if not chain_versions.get(name)]
    if missing:
        raise SemanticSourceError(
            f"chain versions are incomplete for a spec record: {missing}"
        )
    return SemanticSpecRecord(
        document_id=document_id,
        revision=revision,
        spec_schema_version=SPEC_STORE_SCHEMA_VERSION,
        spec=spec,
        spec_digest=spec_digest(spec),
        chain_versions={name: chain_versions[name] for name in CHAIN_VERSION_FIELDS},
    )


def record_from_row(row: tuple[Any, ...] | None) -> SemanticSpecRecord | None:
    if row is None:
        return None
    document_id, revision, spec_schema_version, spec_json, digest, chain_json = row
    return SemanticSpecRecord(
        document_id=str(document_id),
        revision=int(revision),
        spec_schema_version=str(spec_schema_version),
        spec=load_diagram_spec(json.loads(spec_json)),
        spec_digest=str(digest),
        chain_versions={str(k): str(v) for k, v in json.loads(chain_json).items()},
    )


__all__ = [
    "CHAIN_VERSION_FIELDS",
    "SPEC_STORE_SCHEMA_VERSION",
    "SemanticSourceError",
    "SemanticSourceUnavailableError",
    "SemanticSourceVersionError",
    "SemanticSpecRecord",
    "build_spec_record",
    "record_from_row",
    "spec_digest",
    "spec_payload",
]
