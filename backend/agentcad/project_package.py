"""M12-D5: deterministic Project Delivery Package (Gate CODE GO).

Frozen contract (reports/m12-d1-design.md Q6 + F77-1/F77-2 + D5 CODE GO):
- input {project_id, evaluation_as_of}; member pins are SERVER-RESOLVED to
  the CURRENT revisions — callers can never pin arbitrary revisions;
- hard preconditions, each a stable error and NO package: project exists;
  every member pin == current revision; every active link pin == the package
  member pins. Current exact revision only; no historical rebuild;
- ZIP members (frozen order): MANIFEST.json (never lists itself),
  project.json, links/engineering_links.json (active links only),
  readiness/project_readiness.json, domains/pid/<id>-r<rev>.json (canonical
  P&ID envelope), domains/cable/<id>-r<rev>.zip (D3 cable export bytes);
- deterministic: ZIP_STORED + fixed 1980 ZipInfo + canonical JSON; same
  pins + same evaluation_as_of + same effective server profile →
  byte-for-byte equal across fresh processes;
- verify is a pure function: member hashes, MANIFEST self-exclusion,
  missing/extra/duplicate members and ZIP metadata tamper all rejected.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime
from typing import Any

from .cable_export import export_cable_document
from .cable_service import CableService
from .project_io import document_envelope
from .project_readiness import ProjectReadinessService
from .service import DocumentService
from .store import SQLiteDocumentStore

PACKAGE_SCHEMA = "pid-agent.project-package/1"
PACKAGE_SCHEMA_VERSION = 1
_FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_MANIFEST_PATH = "MANIFEST.json"


class ProjectPackageError(RuntimeError):
    """Stable, machine-readable package refusal — never half a package."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _zipinfo(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=_FIXED_ZIP_DATE)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 0
    info.external_attr = 0
    info.extra = b""
    return info


def _member_revision(store: SQLiteDocumentStore, document_id: str, domain: str) -> int | None:
    if domain == "cable":
        envelope = store.get_cable_envelope(document_id)
        return None if envelope is None else int(envelope[0])
    stored = store.get(document_id)
    return None if stored is None else stored.document.revision


def _member_content_hash(
    *,
    store: SQLiteDocumentStore,
    pid_service: DocumentService,
    document_id: str,
    domain: str,
    evaluation_as_of: datetime,
) -> str:
    if domain == "cable":
        from .cable_validation import assess_cable_document

        return assess_cable_document(CableService(store), document_id).content_hash
    from .release_validator import assess_document_release_readiness
    from .validation_profile import load_profile

    readiness = assess_document_release_readiness(
        pid_service, document_id, load_profile(), now=evaluation_as_of
    )
    return readiness.content_hash


def build_project_package(
    *,
    store: SQLiteDocumentStore,
    pid_service: DocumentService,
    cable_service: CableService,
    project_id: str,
    member_pins: dict[str, int],
    evaluation_as_of: datetime,
) -> bytes:
    """Build the deterministic project package; fail closed with a stable
    code and no bytes when any frozen precondition does not hold.

    D81-1: ``member_pins`` is the caller-declared delivery state — the exact
    {document_id: revision} set being delivered. The server re-reads the
    current revisions and compares each one; historical revisions are never
    rebuilt (F77-2). D81-4: an active-link snapshot is captured once and
    re-verified before ZIP emission, so the package is one consistent
    project state, never a check-then-read composite."""
    if evaluation_as_of.tzinfo is None:
        raise ProjectPackageError("invalid_input", "evaluation_as_of must be timezone-aware")
    if store.get_project(project_id) is None:
        raise ProjectPackageError("project_not_found", f"project {project_id!r} does not exist")
    members = sorted(store.list_project_documents(project_id))
    if not members:
        raise ProjectPackageError("empty_project", "project has no member documents")

    requested = set(member_pins)
    expected = {document_id for document_id, _domain, _added_at in members}
    if requested != expected:
        raise ProjectPackageError(
            "member_pins_mismatch",
            "member pins must be exactly the project member set; "
            f"missing={sorted(expected - requested)} extra={sorted(requested - expected)}",
        )

    # Pin gate: every requested pin must equal the CURRENT revision.
    pins: dict[str, tuple[int, str]] = {}
    for document_id, domain, _added_at in members:
        revision = _member_revision(store, document_id, domain)
        if revision is None or revision != int(member_pins[document_id]):
            raise ProjectPackageError(
                "member_revision_not_current",
                f"member {document_id!r} pin {member_pins[document_id]!r} "
                f"is not the current revision {revision!r}",
            )
        pins[document_id] = (revision, domain)

    def _canonical_link(row: dict[str, Any]) -> dict[str, Any]:
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

    member_identity_snapshot = [(document_id, domain) for document_id, domain, _ in members]

    links_snapshot = [
        _canonical_link(row) for row in store.list_active_engineering_links(project_id)
    ]
    for link in links_snapshot:
        source_pin = pins.get(link["source_document_id"])
        target_pin = pins.get(link["target_document_id"])
        if (
            source_pin is None
            or target_pin is None
            or source_pin[0] != link["pinned_source_revision"]
            or target_pin[0] != link["pinned_target_revision"]
        ):
            raise ProjectPackageError(
                "link_pin_not_current",
                f"active link {link['link_id']!r} pins are not the current revisions",
            )

    readiness = ProjectReadinessService(store, pid_service, cable_service).assess(
        project_id=project_id, evaluation_as_of=evaluation_as_of
    )

    project_name = store.get_project(project_id)[1]  # type: ignore[index]
    project_payload = {
        "project_id": project_id,
        "name": project_name,
        "members": [
            {
                "domain": domain,
                "document_id": document_id,
                "pinned_revision": pins[document_id][0],
                "content_hash": _member_content_hash(
                    store=store,
                    pid_service=pid_service,
                    document_id=document_id,
                    domain=domain,
                    evaluation_as_of=evaluation_as_of,
                ),
            }
            for document_id, domain, _added_at in members
        ],
    }
    links_payload = links_snapshot  # D81-4: one captured snapshot, not a re-read
    readiness_payload = {
        "project_id": readiness.project_id,
        "evaluation_as_of": readiness.evaluation_as_of.isoformat(),
        "state": readiness.state,
        "issues": [
            [issue.code, issue.severity, issue.link_id] for issue in readiness.issues
        ],
        "members": [
            {
                "document_id": snapshot.document_id,
                "domain": snapshot.domain,
                "state": snapshot.state,
                "readiness_hash": snapshot.readiness_hash,
                "revision": snapshot.revision,
            }
            for snapshot in readiness.members
        ],
        "result_hash": readiness.result_hash,
        "profile_id": readiness.profile_id,
        "profile_version": readiness.profile_version,
        "profile_fingerprint": readiness.profile_fingerprint,
    }

    artifacts: dict[str, bytes] = {}
    for document_id, domain, _added_at in members:
        revision, _ = pins[document_id]
        # Re-check currentness at artifact time: no package may embed a
        # revision that stopped being current mid-build.
        if _member_revision(store, document_id, domain) != revision:
            raise ProjectPackageError(
                "member_revision_not_current",
                f"member {document_id!r} revision moved during the build",
            )
        if domain == "cable":
            artifact = export_cable_document(
                cable_service, document_id, expected_revision=revision
            ).zip_bytes
            artifacts[f"domains/cable/{document_id}-r{revision}.zip"] = artifact
        else:
            document = pid_service.get_document(document_id)
            if document.revision != revision:
                raise ProjectPackageError(
                    "member_revision_not_current",
                    f"member {document_id!r} revision moved during the build",
                )
            envelope_payload = document_envelope(document).model_dump(mode="json")
            # Frozen determinism (F77): the package carries zero wall-clock —
            # evaluation_as_of is the only time input — so the volatile
            # created_at/updated_at stamps are stripped from the pinned
            # revision snapshot; the engineering content is the truth.
            envelope_payload["document"].pop("created_at", None)
            envelope_payload["document"].pop("updated_at", None)
            artifact = _canonical(envelope_payload).encode("utf-8")
            artifacts[f"domains/pid/{document_id}-r{revision}.json"] = artifact

    # D81-4 final gate: the package must be ONE project state. Re-verify
    # member revisions and the active-link snapshot immediately before ZIP
    # emission; any drift is a stable refusal, never an inconsistent bag.
    final_identity = [
        (document_id, domain) for document_id, domain, _ in store.list_project_documents(project_id)
    ]
    if final_identity != member_identity_snapshot:
        raise ProjectPackageError(
            "package_state_changed", "project membership changed during the build"
        )
    for document_id, domain, _added_at in members:
        if _member_revision(store, document_id, domain) != pins[document_id][0]:
            raise ProjectPackageError(
                "package_state_changed",
                f"member {document_id!r} revision moved during the build",
            )
    final_links = [_canonical_link(row) for row in store.list_active_engineering_links(project_id)]
    if final_links != links_snapshot:
        raise ProjectPackageError(
            "package_state_changed", "active links changed during the build"
        )

    members_payload = {path: data for path, data in artifacts.items()}
    members_payload["links/engineering_links.json"] = _canonical(links_payload).encode("utf-8")
    members_payload["readiness/project_readiness.json"] = _canonical(readiness_payload).encode(
        "utf-8"
    )
    members_payload["project.json"] = _canonical(project_payload).encode("utf-8")

    # Frozen member order (Q6): manifest-excluded metadata first, then pid
    # artifacts, then cable artifacts; each domain block sorted by
    # document_id. The verifier rejects any order drift (D81-2).
    pid_paths = sorted(
        path for path in artifacts if path.startswith("domains/pid/")
    )
    cable_paths = sorted(
        path for path in artifacts if path.startswith("domains/cable/")
    )
    ordered_paths = [
        "project.json",
        "links/engineering_links.json",
        "readiness/project_readiness.json",
        *pid_paths,
        *cable_paths,
    ]
    manifest = {
        "schema": PACKAGE_SCHEMA,
        "project_id": project_id,
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "members": [
            {
                "path": path,
                "sha256": _sha256(members_payload[path]),
                "bytes": len(members_payload[path]),
            }
            for path in ordered_paths
        ],
        "links_sha256": _sha256(members_payload["links/engineering_links.json"]),
        "readiness_sha256": _sha256(members_payload["readiness/project_readiness.json"]),
    }
    manifest_bytes = (_canonical(manifest) + "\n").encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(_zipinfo(_MANIFEST_PATH), manifest_bytes)
        for path in ordered_paths:
            archive.writestr(_zipinfo(path), members_payload[path])
    return buffer.getvalue()


def verify_project_package(package_bytes: bytes) -> None:
    """Pure verification: recompute hashes and check ZIP metadata. Any
    tampering — a flipped byte, a swapped member, a missing/extra/duplicate
    entry, re-encoded ZIP metadata — raises ProjectPackageError."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(package_bytes), mode="r")
        _verify_archive(archive)
    except zipfile.BadZipFile as exc:
        # A flipped byte surfaces as a CRC failure during member reads; a
        # truncated or re-encoded archive as a structural one. Both are
        # tampering.
        raise ProjectPackageError("tamper_detected", f"zip integrity: {exc}") from exc


def _verify_archive(archive: zipfile.ZipFile) -> None:
    with archive:
        entries = archive.infolist()
        by_name: dict[str, list[zipfile.ZipInfo]] = {}
        for info in entries:
            by_name.setdefault(info.filename, []).append(info)
        duplicates = [name for name, infos in by_name.items() if len(infos) > 1]
        if duplicates:
            raise ProjectPackageError(
                "tamper_detected", f"duplicate members: {sorted(duplicates)}"
            )
        names = set(by_name)
        if _MANIFEST_PATH not in names:
            raise ProjectPackageError("tamper_detected", "MANIFEST.json is missing")
        for info in entries:
            if (
                info.compress_type != zipfile.ZIP_STORED
                or tuple(info.date_time) != _FIXED_ZIP_DATE
                or info.extra != b""
            ):
                raise ProjectPackageError(
                    "tamper_detected", f"zip metadata drift on {info.filename!r}"
                )

        try:
            manifest = json.loads(archive.read(_MANIFEST_PATH).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProjectPackageError("tamper_detected", "MANIFEST is not valid JSON") from exc
        if _MANIFEST_PATH in {member["path"] for member in manifest.get("members", [])}:
            raise ProjectPackageError(
                "tamper_detected", "MANIFEST must not list itself as a member"
            )

        listed = [member["path"] for member in manifest.get("members", [])]
        expected = set(listed) | {_MANIFEST_PATH}
        if names != expected:
            missing = sorted(expected - names)
            extra = sorted(names - expected)
            raise ProjectPackageError(
                "tamper_detected", f"member mismatch: missing={missing} extra={extra}"
            )
        # D81-2/FINAL: order is part of the frozen format, and the verifier
        # must validate it INDEPENDENTLY — not just manifest-vs-zip agreement
        # (a attacker could reorder both consistently). The frozen canonical
        # order is fixed heads, then pid artifacts, then cable artifacts,
        # each domain block sorted by path.
        pid_paths = sorted(p for p in listed if p.startswith("domains/pid/"))
        cable_paths = sorted(p for p in listed if p.startswith("domains/cable/"))
        head_paths = [
            p
            for p in listed
            if p in (
                "project.json",
                "links/engineering_links.json",
                "readiness/project_readiness.json",
            )
        ]
        canonical_order = [
            *head_paths,
            *pid_paths,
            *cable_paths,
        ]
        if len(head_paths) != 3 or len(canonical_order) != len(listed):
            raise ProjectPackageError(
                "tamper_detected", "frozen member set does not match the canonical layout"
            )
        if listed != canonical_order:
            raise ProjectPackageError(
                "tamper_detected", "MANIFEST member order violates the frozen canonical order"
            )
        zip_order = [info.filename for info in entries if info.filename != _MANIFEST_PATH]
        if zip_order != listed:
            raise ProjectPackageError(
                "tamper_detected", "member order drift between MANIFEST and archive"
            )
        for member in manifest["members"]:
            data = archive.read(member["path"])
            if len(data) != member["bytes"] or _sha256(data) != member["sha256"]:
                raise ProjectPackageError(
                    "tamper_detected", f"hash mismatch on {member['path']!r}"
                )
        if _sha256(archive.read("links/engineering_links.json")) != manifest["links_sha256"]:
            raise ProjectPackageError("tamper_detected", "links hash mismatch")
        if (
            _sha256(archive.read("readiness/project_readiness.json"))
            != manifest["readiness_sha256"]
        ):
            raise ProjectPackageError("tamper_detected", "readiness hash mismatch")
