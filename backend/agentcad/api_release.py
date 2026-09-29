"""M9-WS2: release evidence-package construction (Phase A, in-memory only).

This module builds the deterministic evidence ZIP for one release. It is
deliberately split from the HTTP surface and from the store:

* everything here works on the *pinned* in-memory ``Document`` snapshot the
  caller read exactly once — no function in this module re-reads the drawing,
  so PDF/DXF/validation/readiness all describe the same revision;
* nothing here touches the database except reading the audit chain (a pure
  read) — package generation never holds a SQLite write lock (Gate amendment:
  Phase A stages in memory, Phase B is the short atomic ``commit_release``);
* hash formation order is frozen (Gate R49-1): plain members → per-member
  SHA256 → MANIFEST.sha256 → evidence_manifest_hash → ZIP → package_sha256.
  ``release.json`` is the record's evidence projection and physically cannot
  contain the hashes, because they do not exist yet when it is serialised.

Frozen member set (exactly these seven, ASCII-sorted in the MANIFEST):
drawing.pdf, drawing.dxf, validation-report.json, release-readiness.json,
audit-chain.json, approval.json, release.json, plus MANIFEST.sha256 itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from .audit import verify_audit_records
from .audit_hash import GENESIS_HASH
from .dxf_export import DxfExportOptions, render_dxf
from .exporting import resolve_export_bounds
from .models import Document
from .pdf_export import (
    PdfExportError,
    PdfExportOptions,
    build_pdf_export_plan,
    render_pdf_bytes,
)
from .review_models import ReleaseRecord
from .service import DocumentService
from .store import ReleaseEvidencePackage, SQLiteDocumentStore
from .symbols import SymbolRegistry
from .validation_models import ReleaseReadiness, ValidationResult

#: The exact, frozen member set of one evidence package (MANIFEST excluded).
PACKAGE_MEMBERS: tuple[str, ...] = (
    "approval.json",
    "audit-chain.json",
    "drawing.dxf",
    "drawing.pdf",
    "release-readiness.json",
    "release.json",
    "validation-report.json",
)


class EvidenceBuildError(RuntimeError):
    """The evidence package could not be built; the release must not proceed."""

    code = "release_evidence_build_failed"


@dataclass(frozen=True)
class BuiltEvidencePackage:
    members: dict[str, bytes]
    manifest_bytes: bytes
    manifest_sha256: str
    zip_bytes: bytes
    package_sha256: str
    verified_through_ordinal: int
    verified_global_tip_hash: str


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_audit_chain_member(
    store: SQLiteDocumentStore, document_id: str
) -> tuple[bytes, int, str]:
    """The frozen audit-chain.json (Gate R49-2): a *document subset* that was
    verified against the complete global hash chain, never a stand-alone chain.

    Gate R68-1: the records are read exactly ONCE and the verification, the
    reported ordinal, the reported tip and the document subset all come from
    that same immutable snapshot — the claimed cutoff is provably the verified
    cutoff. The cutoff is the chain tip at Phase A time, a historical anchor;
    the ``release.released`` audit of this very release is written in Phase B
    and therefore can never appear inside the package.
    """

    records = store.all_audit_records()
    verification = verify_audit_records(
        records, database_instance_id=store.database_instance_id
    )
    if not verification.ok:
        raise EvidenceBuildError(
            f"global audit chain diverged: {verification.first_divergence.reason} "
            f"at ordinal {verification.first_divergence.ordinal}"
        )
    verified_through_ordinal = len(records)
    verified_global_tip_hash = records[-1].record_hash if records else GENESIS_HASH
    member = {
        "scope": "document-subset",
        "verified_through_ordinal": verified_through_ordinal,
        "verified_global_tip_hash": verified_global_tip_hash,
        "global_chain_verification": "ok",
        "database_instance_id": store.database_instance_id,
        "document_records": [
            record.model_dump(mode="json")
            for record in records
            if record.document_id == document_id
        ],
    }
    return _canonical_json(member), verified_through_ordinal, verified_global_tip_hash


def build_release_evidence_package(
    *,
    document: Document,
    state_release: ReleaseRecord,
    approval_json: dict[str, Any],
    readiness: ReleaseReadiness,
    validation_result: ValidationResult,
    service: DocumentService,
    store: SQLiteDocumentStore,
) -> BuiltEvidencePackage:
    """Build the complete evidence package for one release from pinned inputs.

    ``state_release`` may carry placeholder hashes — only its evidence
    projection (which excludes the hashes) is serialised into the package.
    """

    symbols: SymbolRegistry = service.symbols
    try:
        bounds = resolve_export_bounds(
            document, symbols, export_range="content", padding=24
        )
        plan = build_pdf_export_plan(
            document,
            bounds,
            service.get_project_settings(),
            PdfExportOptions(
                paper_size="A3",
                orientation="landscape",
                layout="fit",
                margin_mm=10,
                frame=True,
                title_block=True,
                tile_scale=1,
                project_name=None,
                drawing_number=None,
                revision=None,
                drawing_date=None,
            ),
        )
        pdf_bytes = render_pdf_bytes(document, symbols, plan)
        dxf_result = render_dxf(
            document, symbols, bounds, DxfExportOptions(units="mm", scale=1)
        )
    except (PdfExportError, ValueError, OSError) as exc:
        raise EvidenceBuildError(f"export surface failed while building evidence: {exc}") from exc

    audit_member, verified_through_ordinal, verified_tip = _build_audit_chain_member(
        store, document.id
    )

    members: dict[str, bytes] = {
        "drawing.pdf": pdf_bytes,
        "drawing.dxf": dxf_result.payload.encode("utf-8"),
        "validation-report.json": _canonical_json(validation_result.model_dump(mode="json")),
        "release-readiness.json": _canonical_json(readiness.model_dump(mode="json")),
        "audit-chain.json": audit_member,
        "approval.json": _canonical_json(approval_json),
        "release.json": _canonical_json(state_release.evidence_projection()),
    }
    if tuple(sorted(members)) != PACKAGE_MEMBERS:
        raise EvidenceBuildError("package member set drifted from the frozen contract")

    manifest_lines = [
        f"{_sha256(members[name])}  {name}\n" for name in sorted(members)
    ]
    manifest_bytes = "".join(manifest_lines).encode("utf-8")
    manifest_sha256 = _sha256(manifest_bytes)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, members[name])
        manifest_info = zipfile.ZipInfo(
            filename="MANIFEST.sha256", date_time=(1980, 1, 1, 0, 0, 0)
        )
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(manifest_info, manifest_bytes)
    zip_bytes = buffer.getvalue()

    return BuiltEvidencePackage(
        members=members,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
        zip_bytes=zip_bytes,
        package_sha256=_sha256(zip_bytes),
        verified_through_ordinal=verified_through_ordinal,
        verified_global_tip_hash=verified_tip,
    )


class ReleaseEvidenceCorruptError(RuntimeError):
    """A stored package fails the frozen integrity re-check (HTTP 500)."""

    code = "release_evidence_corrupt"


#: Frozen MANIFEST line: 64 lowercase hex, two spaces, ASCII filename.
_MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9._-]+)$")


def verify_evidence_package(
    package: ReleaseEvidencePackage,
    *,
    expected_manifest_sha256: str,
    expected_package_sha256: str,
) -> None:
    """Re-verify a stored package before serving it (Gate amendments, frozen).

    Gate R68-2 (cross-plane binding): the BLOB row must agree with the
    governance-plane ReleaseRecord on BOTH hashes before anything else — a row
    made internally self-consistent by tampering must still fail against the
    record. Gate R68-3: every structural/encoding/JSON failure maps to
    :class:`ReleaseEvidenceCorruptError` (HTTP 500 ``release_evidence_corrupt``);
    the MANIFEST format itself is strictly locked (exactly seven lines, one per
    frozen member, ASCII-sorted, LF-terminated). In-memory only — the ZIP is
    never extracted to the file system.
    """

    if package.manifest_sha256 != expected_manifest_sha256:
        raise ReleaseEvidenceCorruptError(
            "package row manifest hash disagrees with the release record"
        )
    if package.package_sha256 != expected_package_sha256:
        raise ReleaseEvidenceCorruptError(
            "package row package hash disagrees with the release record"
        )
    if _sha256(package.package_blob) != package.package_sha256:
        raise ReleaseEvidenceCorruptError("package blob does not match package_sha256")
    try:
        with zipfile.ZipFile(BytesIO(package.package_blob)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ReleaseEvidenceCorruptError("duplicate member names in package")
            if "MANIFEST.sha256" not in names:
                raise ReleaseEvidenceCorruptError("package has no MANIFEST.sha256")
            member_names = [name for name in names if name != "MANIFEST.sha256"]
            if tuple(sorted(member_names)) != PACKAGE_MEMBERS:
                raise ReleaseEvidenceCorruptError(
                    "package member set does not match the frozen contract"
                )
            manifest_bytes = archive.read("MANIFEST.sha256")
            if _sha256(manifest_bytes) != package.manifest_sha256:
                raise ReleaseEvidenceCorruptError("MANIFEST does not match manifest_sha256")
            manifest_text = manifest_bytes.decode("utf-8")
            lines = manifest_text.split("\n")
            if lines[-1] != "" or len(lines) - 1 != len(PACKAGE_MEMBERS):
                raise ReleaseEvidenceCorruptError(
                    "MANIFEST must be exactly seven LF-terminated lines"
                )
            entries: dict[str, str] = {}
            for line in lines[:-1]:
                match = re.match(_MANIFEST_LINE, line)
                if match is None:
                    raise ReleaseEvidenceCorruptError(
                        "MANIFEST line must be 64hex + two spaces + ASCII filename"
                    )
                digest, name = match.group(1), match.group(2)
                if name in entries:
                    raise ReleaseEvidenceCorruptError(f"duplicate MANIFEST entry: {name}")
                entries[name] = digest
            if tuple(sorted(entries)) != PACKAGE_MEMBERS:
                raise ReleaseEvidenceCorruptError("MANIFEST member set drifted")
            for name in PACKAGE_MEMBERS:
                if _sha256(archive.read(name)) != entries[name]:
                    raise ReleaseEvidenceCorruptError(f"member hash mismatch: {name}")
            release_member = json.loads(archive.read("release.json").decode("utf-8"))
            for forbidden in ("evidence_manifest_hash", "package_sha256", "package_blob"):
                if forbidden in release_member:
                    raise ReleaseEvidenceCorruptError(
                        f"release.json must not contain {forbidden}"
                    )
    except UnicodeDecodeError as exc:
        raise ReleaseEvidenceCorruptError("package text member is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseEvidenceCorruptError(f"release.json is not valid JSON: {exc}") from exc
    except zipfile.BadZipFile as exc:
        raise ReleaseEvidenceCorruptError("package is not a valid ZIP") from exc
