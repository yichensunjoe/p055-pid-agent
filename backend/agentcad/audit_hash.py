"""Canonical hashing primitives for the audit chain (leaf module, no domain imports).

Kept separate from ``audit.py`` so that ``store.py`` can assign chain hashes inside
the same SQLite transaction as the document write without importing the domain
modules (mirrors / routing / semantic diff) that ``audit.py`` needs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .audit_models import (
    AUDIT_CHAIN_SCHEMA,
    AUDIT_CHAIN_VERSION,
    AUDIT_HASH_ALGORITHM,
    GENESIS_HASH,
)


def audit_chain_metadata() -> dict[str, Any]:
    """Published description of the chain rule, embedded in evidence packages."""
    return {
        "schema": AUDIT_CHAIN_SCHEMA,
        "version": AUDIT_CHAIN_VERSION,
        "hash_algorithm": AUDIT_HASH_ALGORITHM,
        "genesis_hash": GENESIS_HASH,
        "hashed_fields": list(HASHED_FIELDS),
        "rule": "sha256(canonical_json({payload: <hashed_fields>, prev_hash: <previous record_hash>}))",
    }

# The exact field list covered by the chain hash. Verification recomputes the hash
# from this list only, so both the stored row and an external evidence package can
# be checked with the same, published rule.
HASHED_FIELDS: tuple[str, ...] = (
    "record_id",
    "ordinal",
    "recorded_at",
    "event_type",
    "actor",
    "surface",
    "tool_name",
    "status",
    "document_id",
    "project_id",
    "base_revision",
    "result_revision",
    "session_id",
    "approval_id",
    "tool_call_id",
    "provider",
    "model",
    "label",
    "intent_hash",
    "diff_hash",
    "diff_preview_hash",
    "diff_binding",
    "validation_status",
    "validation_hash",
    "evidence",
    "error_code",
    "chain_schema",
    "chain_version",
)


def canonical_json(value: Any) -> str:
    """Return the canonical JSON used for every audit hash and comparison."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def payload_hash(value: Any) -> str:
    """SHA-256 over canonical JSON of an arbitrary, already-redacted payload."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hash_payload(value: Any) -> str:
    """Alias kept explicit for readability at call sites."""
    return payload_hash(value)


def normalize_hashed_value(field: str, value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if field in {"base_revision", "result_revision", "ordinal", "chain_version"} and value is not None:
        return int(value)
    if value == "":
        return ""
    return value


def record_hash_payload(record: Any) -> dict[str, Any]:
    """Build the hash payload for one audit record from the published field list."""
    payload: dict[str, Any] = {}
    for field in HASHED_FIELDS:
        value = record.get(field) if isinstance(record, dict) else getattr(record, field, None)
        payload[field] = normalize_hashed_value(field, value)
    return payload


def compute_record_hash(record: Any, prev_hash: str) -> str:
    payload = record_hash_payload(record)
    return payload_hash({"payload": payload, "prev_hash": prev_hash})


__all__ = [
    "AUDIT_CHAIN_SCHEMA",
    "AUDIT_CHAIN_VERSION",
    "GENESIS_HASH",
    "audit_chain_metadata",
    "HASHED_FIELDS",
    "canonical_json",
    "compute_record_hash",
    "hash_payload",
    "normalize_hashed_value",
    "payload_hash",
    "record_hash_payload",
]
