"""M6-2B-D2 performance baseline: intake on a large synthetic import, median-of-5.

The budget this anchors (design baseline §8.1 proposal): region derivation + deterministic
candidate extraction on a ~10k-element imported document should stay under 5 s locally. This
script builds a synthetic multi-thousand-element drawing, runs it through the unchanged
governed import, then times the full D2 intake (pin + derive + file) five times on fresh
stores, plus the pure derivation alone.

Honesty rules carried over from M13's evidence-method ruling: the numbers are local
median-of-5 wall clock with the environment recorded; they are a *baseline to compare
against*, not a CI ceiling, and must never be quoted against a different machine or a
different workflow without the noise band being stated.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agentcad.cad_import import CadImporter  # noqa: E402
from agentcad.cad_models import CadImportOptions  # noqa: E402
from agentcad.m6_source_adapter import M6SourceAdapter  # noqa: E402
from agentcad.service import DocumentService  # noqa: E402
from agentcad.store import SQLiteDocumentStore  # noqa: E402
from agentcad.symbols import SymbolRegistry  # noqa: E402

REPORT = Path(__file__).resolve().parents[1] / "reports" / "m6-2b" / "perf-baseline.json"

#: Grid sizing: 4 block kinds x 90 instances = 360 clusters, each ~5-6 elements, plus one
#: tag per cluster, piping between neighbours and free notes -> ~2.7k elements.
GRID_COLUMNS = 18
INSTANCES_PER_BLOCK = 90
BLOCK_KINDS = ("centrifugal_pump", "闸阀", "heat_exchanger", "ZZZ-UNKNOWN")
PITCH = 90.0
RUNS = 5


def _record(kind: str, items: list[tuple[int, object]]) -> list[tuple[int, object]]:
    return [(0, kind), *items]


def _big_dxf() -> bytes:
    lines: list[str] = []

    def emit(*records: tuple[int, object]) -> None:
        for code, value in records:
            lines.extend([str(code), str(value)])

    pump = [
        _record("LINE", [(8, "EQUIP"), (10, -10.0), (20, -10.0), (11, 10.0), (21, -10.0)]),
        _record("LINE", [(8, "EQUIP"), (10, 10.0), (20, -10.0), (11, 10.0), (21, 10.0)]),
        _record("LINE", [(8, "EQUIP"), (10, 10.0), (20, 10.0), (11, -10.0), (21, 10.0)]),
        _record("LINE", [(8, "EQUIP"), (10, -10.0), (20, 10.0), (11, -10.0), (21, -10.0)]),
        _record("CIRCLE", [(8, "EQUIP"), (10, 0.0), (20, 0.0), (40, 6.0)]),
    ]
    blocks = {name: pump for name in BLOCK_KINDS}

    emit((0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1032"))
    emit((9, "$DWGCODEPAGE"), (3, "UTF-8"))
    emit((0, "ENDSEC"))
    emit((0, "SECTION"), (2, "TABLES"), (0, "TABLE"), (2, "LAYER"))
    emit((0, "LAYER"), (2, "0"), (70, 0), (62, 7), (6, "CONTINUOUS"))
    emit((0, "LAYER"), (2, "EQUIP"), (70, 0), (62, 5), (6, "CONTINUOUS"))
    emit((0, "ENDTAB"), (0, "ENDSEC"))
    emit((0, "SECTION"), (2, "BLOCKS"))
    for name, entities in blocks.items():
        emit((0, "BLOCK"), (8, "0"), (2, name), (70, 0), (10, 0.0), (20, 0.0), (30, 0.0))
        for entity in entities:
            emit(*entity)
        emit((0, "ENDBLK"), (8, "0"))
    emit((0, "ENDSEC"))
    emit((0, "SECTION"), (2, "ENTITIES"))

    tag_index = 0
    for block_index, name in enumerate(BLOCK_KINDS):
        for instance in range(INSTANCES_PER_BLOCK):
            column = (block_index * INSTANCES_PER_BLOCK + instance) % GRID_COLUMNS
            row = (block_index * INSTANCES_PER_BLOCK + instance) // GRID_COLUMNS
            x, y = 50.0 + column * PITCH, 50.0 + row * PITCH
            emit(
                *_record("INSERT", [(8, "EQUIP"), (2, name), (10, x), (20, y), (30, 0.0)])
            )
            tag_index += 1
            emit(
                *_record(
                    "TEXT",
                    [
                        (8, "EQUIP"),
                        (10, x),
                        (20, y + 25.0),
                        (30, 0.0),
                        (40, 2.5),
                        (1, f"P-{1000 + tag_index}"),
                    ],
                )
            )
            # Piping stub to the next instance: blockless geometry the adapter must skip.
            emit(
                *_record(
                    "LINE",
                    [
                        (8, "EQUIP"),
                        (10, x + 10.0),
                        (20, y),
                        (11, x + PITCH - 10.0),
                        (21, y),
                    ],
                )
            )
    for note in range(60):
        emit(
            *_record(
                "TEXT",
                [
                    (8, "EQUIP"),
                    (10, 30.0 + note * 25.0),
                    (20, 30.0),
                    (30, 0.0),
                    (40, 3.0),
                    (1, f"施工说明 {note}"),
                ],
            )
        )
    emit((0, "ENDSEC"), (0, "EOF"))
    return ("\n".join(lines) + "\n").encode("utf-8")


def main() -> int:
    registry = SymbolRegistry()
    data = _big_dxf()
    ingest_ms: list[float] = []
    derive_ms: list[float] = []
    context: dict[str, object] = {}
    for _ in range(RUNS):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteDocumentStore(Path(tmp) / "perf.sqlite3")
            service = DocumentService(store, registry)
            import_started = perf_counter()
            result = CadImporter(service).import_bytes(
                data, filename="big_synthetic.dxf", options=CadImportOptions()
            )
            import_ms = (perf_counter() - import_started) * 1000
            adapter = M6SourceAdapter(store, registry)
            started = perf_counter()
            summary = adapter.ingest(result.document_id, target_document_id="doc_target")
            ingest_ms.append((perf_counter() - started) * 1000)

            document = service.get_document(result.document_id)
            artifact = adapter.pin_source(result.document_id)
            started = perf_counter()
            adapter.derive_candidates(document, artifact, target_document_id="doc_target")
            derive_ms.append((perf_counter() - started) * 1000)
            context = {
                "elements": len(document.elements),
                "clusters": summary.cluster_count,
                "candidates": len(summary.candidate_ids),
                "counts_by_type": summary.counts_by_type,
                "import_ms_last_run": round(import_ms, 2),
                "extraction_digest": summary.extraction_digest,
                "catalogue_fingerprint": summary.catalogue_fingerprint,
            }

    report = {
        "milestone": "M6-2B-D2 source-to-candidate adapter",
        "measured_path": "M6SourceAdapter.ingest (pin source + derive + file via the governed core)",
        "budget_reference": "design baseline reports/m6-phase2b-design.md §8.1 proposal: "
        "region derivation + deterministic extraction <= 5s on a ~10k-element import, "
        "local median-of-5",
        "runs": RUNS,
        "ingest_ms_samples": [round(sample, 2) for sample in ingest_ms],
        "ingest_ms_median": round(statistics.median(ingest_ms), 2),
        "derive_only_ms_samples": [round(sample, 2) for sample in derive_ms],
        "derive_only_ms_median": round(statistics.median(derive_ms), 2),
        **context,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor_count": os.cpu_count(),
            "note": (
                "local development machine, SQLite on a temp directory, synthetic DXF. "
                "Per the M13 evidence-method ruling these numbers are a baseline, not a CI "
                "ceiling: runner noise there exceeds +/-40%, so cross-workflow comparison "
                "is meaningless."
            ),
        },
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
