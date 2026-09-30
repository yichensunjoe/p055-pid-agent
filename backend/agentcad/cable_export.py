"""M11-D3: deterministic Cable export artifact (two-member ZIP).

Frozen contract: ZIP_STORED, exactly two members (cable-document.json +
MANIFEST.sha256 with exactly one entry hashing cable-document.json), fully
frozen ZIP metadata for byte-for-byte cross-process reproducibility, all
hashes server-side, expected_revision stale CAS, tamper-evidence verifier.
Internal typed errors only — no HTTP semantics at this layer.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Any

from .cable_service import CableDocumentNotFoundError, CableService
from .cable_validation import CableReadiness, assess

MEMBER_DOCUMENT = "cable-document.json"
MEMBER_MANIFEST = "MANIFEST.sha256"
_MEMBERS = (MEMBER_DOCUMENT, MEMBER_MANIFEST)


class CableExportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CableExportArtifact:
    zip_bytes: bytes
    artifact_sha256: str
    readiness: CableReadiness


def _zipinfo(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 0
    info.external_attr = 0
    info.extra = b""
    return info


def export_cable_document(
    service: CableService,
    document_id: str,
    *,
    expected_revision: int,
) -> CableExportArtifact:
    """Deterministic export of the current exact revision. Read-only; stale
    expected_revision fails closed; caller-provided hashes have no entry here."""
    try:
        view = service.load(document_id)
    except CableDocumentNotFoundError:
        raise CableExportError(
            "cable_document_not_found", f"cable document {document_id!r} not found"
        ) from None
    if view.document.revision != expected_revision:
        raise CableExportError(
            "stale_revision",
            f"cable document is at revision {view.document.revision}, "
            f"caller expected {expected_revision}",
        )

    readiness = assess(view)
    document = view.document
    member_payload = _canonical(
        {
            "schema": document.schema,
            "name": document.name,
            "segments": [segment.model_dump(mode="json") for segment in document.segments],
            "revision": readiness.revision,
            "content_hash": readiness.content_hash,
            "validation": {
                "profile_id": readiness.profile_id,
                "profile_version": readiness.profile_version,
                "profile_fingerprint": readiness.profile_fingerprint,
                "eligible": readiness.eligible,
                "reasons": list(readiness.reasons),
                "rule_results": [
                    {
                        "rule_id": r.rule_id,
                        "severity": r.severity,
                        "passed": r.passed,
                        "detail": r.detail,
                    }
                    for r in readiness.rule_results
                ],
                "result_hash": readiness.result_hash,
            },
        }
    ).encode("utf-8")
    manifest = f"{_sha256(member_payload)}  {MEMBER_DOCUMENT}\n".encode()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.comment = b""
        archive.writestr(_zipinfo(MEMBER_DOCUMENT), member_payload)
        archive.writestr(_zipinfo(MEMBER_MANIFEST), manifest)
    zip_bytes = buffer.getvalue()
    return CableExportArtifact(
        zip_bytes=zip_bytes,
        artifact_sha256=_sha256(zip_bytes),
        readiness=readiness,
    )


_MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9._-]+)$")


def verify_cable_artifact(zip_bytes: bytes) -> None:
    """Tamper evidence: member set, per-member hashes, manifest format."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = archive.namelist()
            if tuple(sorted(names)) != tuple(sorted(_MEMBERS)) or len(names) != len(set(names)):
                raise CableExportError("artifact_corrupt", "member set drifted")
            if archive.comment not in (b"", None):
                raise CableExportError("artifact_corrupt", "archive comment not empty")
            document = archive.read(MEMBER_DOCUMENT)
            manifest_text = archive.read(MEMBER_MANIFEST).decode("utf-8")
            lines = manifest_text.split("\n")
            if lines[-1] != "" or len(lines) - 1 != 1:
                raise CableExportError("artifact_corrupt", "manifest must be exactly one line")
            match = _MANIFEST_LINE.fullmatch(lines[0])
            if match is None or match.group(2) != MEMBER_DOCUMENT:
                raise CableExportError("artifact_corrupt", "manifest entry malformed")
            if match.group(1) != _sha256(document):
                raise CableExportError("artifact_corrupt", "document hash mismatch")
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise CableExportError("artifact_corrupt", "not a valid ZIP") from exc
