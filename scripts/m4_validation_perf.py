#!/usr/bin/env python
"""Measure canonical validation on a representative large drawing (M4, remote baseline §8).

The point of this script is that the number in the M4 report is reproducible by someone
else, on their machine, with the same method — not that it produces a specific number.

Method (the baseline's, verbatim):

1. import/decode the drawing **once**; converter time is reported separately and is not
   part of canonical validation time;
2. record the inputs: HEAD, OS, Python, CPU, converter key/version, profile id/version,
   rule-bundle fingerprint, symbol-registry fingerprint, final element count;
3. run one warm-up validation (discarded);
4. run five measured validations through the public service function
   (``validate_document``), each executing diagram-quality + engineering-graph +
   engineering-report;
5. report the five wall-clock values, median, min, max, the first cold value and peak RSS.

M4 deliberately sets **no** cross-machine timing threshold: the first real baseline has to
exist before a regression budget can be derived from it.

Usage::

    pid-agent-perf <drawing.dwg|drawing.dxf> [--runs 5] [--json report.json]

or, without installing the console script::

    .venv/bin/python scripts/m4_validation_perf.py <drawing> [--runs 5]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from agentcad.cad_import import CadImporter  # noqa: E402
from agentcad.cad_models import CadImportOptions  # noqa: E402
from agentcad.service import DocumentService  # noqa: E402
from agentcad.store import SQLiteDocumentStore  # noqa: E402
from agentcad.symbols import SymbolRegistry  # noqa: E402
from agentcad.validation_engine import validate_document  # noqa: E402
from agentcad.validation_profile import load_profile  # noqa: E402


def _cpu_model() -> str:
    if platform.system() == "Darwin":
        try:
            return subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip() or platform.processor()
        except OSError:  # pragma: no cover - platform dependent
            return platform.processor()
    return platform.processor() or platform.machine()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def measure(source: Path, runs: int) -> dict:
    symbols = SymbolRegistry()
    profile = load_profile()
    with tempfile.TemporaryDirectory() as tmp:
        service = DocumentService(SQLiteDocumentStore(Path(tmp) / "perf.db"), symbols)
        started = time.perf_counter()
        imported = CadImporter(service).import_path(
            source, options=CadImportOptions(name=source.stem)
        )
        import_seconds = time.perf_counter() - started
        document = service.get_document(imported.document_id)

        cold_started = time.perf_counter()
        cold = validate_document(service, document.id, profile)
        cold_seconds = time.perf_counter() - cold_started

        validate_document(service, document.id, profile)  # warm-up, discarded

        timings: list[float] = []
        for _ in range(runs):
            started = time.perf_counter()
            validate_document(service, document.id, profile)
            timings.append(time.perf_counter() - started)

    peak_rss_mb = None
    try:  # POSIX only; the number is optional evidence, wall clock is mandatory.
        import resource

        peak_rss_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
        if platform.system() == "Darwin":  # macOS reports bytes, Linux kilobytes
            peak_rss_mb = round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024), 1
            )
    except ImportError:  # pragma: no cover - non-POSIX
        pass

    return {
        "head": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "working_tree_dirty_files": len(_git("status", "--porcelain").splitlines()),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "cpu": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "source_file": source.name,
        "source_bytes": source.stat().st_size,
        "source_format": imported.report.source.format,
        "source_format_detail": imported.report.source.format_detail,
        "source_sha256": imported.report.source.sha256,
        "converter": imported.report.source.converter,
        "converter_version": imported.report.source.converter_version,
        "import_seconds": round(import_seconds, 3),
        "import_operations": imported.report.operations,
        "logical_mutations": imported.report.logical_mutations,
        "final_elements": len(document.elements),
        "profile_id": profile.profile_id,
        "profile_version": profile.profile_version,
        "rule_bundle_fingerprint": profile.fingerprint,
        "engine_version": cold.engine_version,
        "symbol_registry_fingerprint": cold.symbol_registry_fingerprint,
        "counts": cold.counts.model_dump(mode="json"),
        "validators_run": cold.validators_run,
        "validators_skipped": [skip.model_dump(mode="json") for skip in cold.validators_skipped],
        "cold_seconds": round(cold_seconds, 3),
        "measured_seconds": [round(value, 3) for value in timings],
        "median_seconds": round(statistics.median(timings), 3),
        "min_seconds": round(min(timings), 3),
        "max_seconds": round(max(timings), 3),
        "peak_rss_mb": peak_rss_mb,
        "result_hash": cold.result_hash,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("drawing", type=Path, help="DWG or DXF file to import and validate")
    parser.add_argument("--runs", type=int, default=5, help="measured validations (default 5)")
    parser.add_argument("--json", type=Path, default=None, help="optional report path")
    args = parser.parse_args(argv)

    if not args.drawing.exists():
        parser.error(f"drawing not found: {args.drawing}")
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    payload = measure(args.drawing, args.runs)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
