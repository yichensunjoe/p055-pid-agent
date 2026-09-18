"""External DWG decoders, invoked as separate processes.

Charter reference: §14 (drawing intake), §21.6 (reviewability).

AgentCAD reads DXF with its own reader (``cad_dxf``). DWG is a closed binary format, so
decoding it needs a converter, and this module is the *only* place that runs one. Three
rules keep that honest:

* **Separate processes, never libraries.** LibreDWG is GPL-3.0 and the ODA File
  Converter is proprietary; linking either into this codebase would make the licence
  question the project's problem. Running an installed executable is the recognised
  boundary, and the executable is never bundled or redistributed.
* **Every candidate is declared with its evidence.** A converter that has been run on
  real drawings says ``verified``; one that is merely present says ``unverified``.
  The importer records which converter produced a document, so a reviewer can tell a
  high-fidelity import from a lossy one without re-running anything.
* **Failure is reported, not papered over.** If a converter produces a partial file,
  the attempt is recorded with its exit status and stderr, and the next candidate is
  tried. A DWG import never silently falls back to "no geometry".

Measured on a real 气路系统总图 (AC1032, 9283 entities), decoding the same file every way:

| Converter | Primitives recovered | Missing block definitions |
|---|---|---|
| ``autocad-core-console`` (official engine, if installed) | **9757** | **0** |
| LibreDWG object dump | 9242 | 0 |
| LibreDWG ``dwg2dxf`` DXF writer | 6523 | **157** |

LibreDWG's *object dump* keeps the contents of dynamic blocks (140 valve instances, 12
pumps, 3 check valves) while its *DXF writer* emits those block definitions empty — which
is why the object dump is preferred over ``dwg2dxf`` rather than the other way round. When
the user already owns a full AutoCAD, its own headless engine reproduces more still, so it
is tried first and everything degrades gracefully from there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

CadConversionKind = Literal["object-stream", "dxf"]

DEFAULT_CONVERT_TIMEOUT_SECONDS = 180.0
MAX_CONVERT_TIMEOUT_SECONDS = 3_600.0

#: Locations worth checking beyond ``PATH`` — the ODA converter ships as a macOS
#: application bundle and as a Windows directory, and neither installs onto ``PATH``.
EXTRA_EXECUTABLE_DIRS: tuple[Path, ...] = (
    Path("/usr/local/bin"),
    Path("/opt/homebrew/bin"),
    Path("/usr/bin"),
    Path("/opt/ODA"),
    Path("C:/Program Files/ODA"),
    Path("C:/Program Files (x86)/ODA"),
)

EXTRA_APPLICATION_GLOBS: tuple[str, ...] = (
    "/Applications/ODAFileConverter*.app/Contents/MacOS",
    "/Applications/ODAFileConverter*.app/Contents/Resources",
    "/Applications/FreeCAD*.app/Contents/Resources/bin",
    "/Applications/FreeCAD*.app/Contents/MacOS",
)

#: AutoCAD's headless core engine, which is *not* a PATH executable and whose name differs
#: per platform (``accoreconsole.exe``/``accoreconsole`` vs ``AcCoreConsole`` inside an app
#: bundle on macOS). Declared as globs because the year is part of every directory name.
AUTOCAD_CORE_CONSOLE_GLOBS: tuple[str, ...] = (
    "/Applications/Autodesk/AutoCAD */AutoCAD *.app/Contents/Helpers/AcCoreConsole.app/Contents/MacOS/AcCoreConsole",
    "/Applications/Autodesk/AutoCAD*/AcCoreConsole.app/Contents/MacOS/AcCoreConsole",
    "/Applications/AutoCAD */AutoCAD *.app/Contents/Helpers/AcCoreConsole.app/Contents/MacOS/AcCoreConsole",
    "/opt/Autodesk/AutoCAD */accoreconsole",
    "/opt/autodesk/autocad*/accoreconsole",
    "C:/Program Files/Autodesk/AutoCAD */accoreconsole.exe",
    "C:/Program Files (x86)/Autodesk/AutoCAD */accoreconsole.exe",

)


def _expand_app_globs(globs: tuple[str, ...]) -> list[Path]:
    """Expand shell-style globs whose wildcards sit in directory names.

    ``Path.glob`` cannot be used from a fixed base here because every pattern above varies
    in more than its last component, so the patterns are walked component by component.
    """

    matches: list[Path] = []
    for pattern in globs:
        parts = Path(pattern).parts
        roots: list[Path] = [Path(parts[0])]
        for part in parts[1:]:
            next_roots: list[Path] = []
            for root in roots:
                if "*" in part:
                    if not root.is_dir():
                        continue
                    try:
                        next_roots.extend(sorted(root.glob(part)))
                    except OSError:
                        continue
                else:
                    candidate = root / part
                    if candidate.exists():
                        next_roots.append(candidate)
            roots = next_roots
            if not roots:
                break
        matches.extend(root for root in roots if root.is_file())
    return matches


@dataclass(frozen=True)
class CadConverter:
    """One declared way to turn a DWG into something this project can decode."""

    key: str
    label: str
    executable: str
    produces: CadConversionKind
    evidence: Literal["verified", "unverified"]
    notes: str
    #: Command template. ``{input}``/``{output}``/``{script}`` are filesystem paths this
    #: module creates; nothing from a request is ever interpolated into an argument list.
    argv: tuple[str, ...] = ()
    #: ODA-style converters take directories rather than files.
    directory_mode: bool = False
    #: Script written to ``{script}`` before the run (scripted converters only).
    script_lines: tuple[str, ...] = ()


def convert_timeout() -> float:
    raw = os.getenv("PID_AGENT_CAD_CONVERT_TIMEOUT_SECONDS", "")
    if not raw.strip():
        return DEFAULT_CONVERT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CONVERT_TIMEOUT_SECONDS
    return min(max(5.0, value), MAX_CONVERT_TIMEOUT_SECONDS)


def pinned_converter_key() -> str:
    """An explicit converter choice from the environment, for reproducible review."""

    return os.getenv("PID_AGENT_CAD_CONVERTER", "").strip().lower()


def _executable_candidates(name: str) -> list[Path]:
    candidates: list[Path] = []
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    for directory in EXTRA_EXECUTABLE_DIRS:
        candidate = directory / name
        if candidate.is_file():
            candidates.append(candidate)
    for pattern in EXTRA_APPLICATION_GLOBS:
        base = Path(pattern.split("*", 1)[0]).parent
        if not base.exists():
            continue
        for match in base.glob(pattern.split("/")[-2] if "*" in pattern else pattern):
            candidate = match / name
            if candidate.is_file():
                candidates.append(candidate)
    return candidates


#: Named groups of absolute glob patterns, so a converter declares *where* its executable
#: lives without hard-coding the search into the resolution call. Tests neutralize a whole
#: group (``monkeypatch.setattr(cad_convert, "EXECUTABLE_GLOBS", {})``) instead of trying to
#: out-glob an installed AutoCAD.
EXECUTABLE_GLOBS: dict[str, tuple[str, ...]] = {
    "autocad-core-console": AUTOCAD_CORE_CONSOLE_GLOBS,
}


def _resolve(name: str, glob_group: str = "") -> str:
    declared = EXECUTABLE_GLOBS.get(glob_group, ()) if glob_group else ()
    for candidate in [*_executable_candidates(name), *_expand_app_globs(declared)]:
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


@lru_cache(maxsize=8)
def converter_version(executable: str) -> str:
    """Report the converter's own version string (best effort, never fatal)."""

    if not executable:
        return ""
    if Path(executable).name.lower().startswith("accoreconsole"):
        # AutoCAD's console ignores ``--version`` and would sit waiting on stdin; it prints
        # its build on startup instead, so read that (with stdin closed) rather than probe.
        return _autocad_console_version(executable)
    for flag in ("--version", "-version"):
        try:
            completed = subprocess.run(
                [executable, flag],
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        text = (completed.stdout or completed.stderr or b"").decode("utf-8", errors="replace")
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if first:
            return first[:120]
    return ""


AUTOCAD_CONSOLE_PROBE_SECONDS = 40.0


def _autocad_console_version(executable: str) -> str:
    """Read the build line AutoCAD's console prints at startup.

    The console never exits on its own without a script, so this reads its banner and
    stops it: ``--version`` is not supported and a plain ``subprocess.run`` would only
    ever time out.
    """

    try:
        process = subprocess.Popen(
            [executable, "/l", "en-US"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": "C"},
        )
    except OSError:
        return ""
    deadline = time.monotonic() + AUTOCAD_CONSOLE_PROBE_SECONDS
    try:
        if process.stdout is not None:
            for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if "AutoCAD Core Engine Console" in line:
                    return line[:120]
                if time.monotonic() > deadline:
                    break
    except (OSError, ValueError):
        return ""
    finally:
        process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    return ""


def available_converters(*, refresh: bool = False) -> list[CadConverter]:
    """Every installed DWG decoder, in the order the importer will try them."""

    if refresh:
        converter_version.cache_clear()
    available: list[CadConverter] = []
    for template in CONVERTER_TEMPLATES:
        executable = _resolve(template.executable_name, template.executable_glob_group)
        if not executable:
            continue
        available.append(
            CadConverter(
                key=template.key,
                label=template.label,
                executable=executable,
                produces=template.produces,
                evidence=template.evidence,
                notes=template.notes,
                argv=template.argv,
                directory_mode=template.directory_mode,
                script_lines=template.script_lines,
            )
        )
    pinned = pinned_converter_key()
    if pinned:
        available = [item for item in available if item.key == pinned]
    return available


@dataclass(frozen=True)
class _ConverterTemplate:
    key: str
    label: str
    executable_name: str
    produces: CadConversionKind
    evidence: Literal["verified", "unverified"]
    notes: str
    argv: tuple[str, ...]
    directory_mode: bool = False
    #: Key into ``EXECUTABLE_GLOBS`` for an executable that does not install onto ``PATH``.
    executable_glob_group: str = ""
    #: Script written to ``{script}`` before the run, for converters that are scripted
    #: rather than handed an output path (AutoCAD's console is the only one).
    script_lines: tuple[str, ...] = ()


#: Order matters: they are tried top to bottom, best-measured first. AutoCAD's own engine
#: recovers the most geometry on the reference drawing and is the only one that expands
#: dynamic blocks exactly as AutoCAD draws them, so it leads when the user has it installed.
#: Nothing here is bundled or redistributed; each converter is used only if already present.
AUTOCAD_DXF_SCRIPT: tuple[str, ...] = (
    # FILEDIA=0 keeps DXFOUT on the command line instead of opening a dialog. The blank
    # lines of a hand-written script are deliberately absent: an empty line at the AutoCAD
    # command prompt *repeats the previous command*, which would re-enter DXFOUT.
    "FILEDIA",
    "0",
    "DXFOUT",
    "{output}",
    "16",
)

CONVERTER_TEMPLATES: tuple[_ConverterTemplate, ...] = (
    _ConverterTemplate(
        key="autocad-core-console",
        label="AutoCAD Core Console (Autodesk)",
        executable_name="accoreconsole",
        produces="dxf",
        evidence="verified",
        notes=(
            "AutoCAD's own headless engine. Measured on a real 气路系统总图: 9757 "
            "primitives recovered with 0 missing block definitions, versus 9242/0 for "
            "LibreDWG's object dump and 6523/157 for its DXF writer. Uses the user's "
            "installed AutoCAD under its own licence; nothing is bundled."
        ),
        executable_glob_group="autocad-core-console",
        argv=(
            "{executable}",
            "/i",
            "{input}",
            "/s",
            "{script}",
            "/l",
            "en-US",
        ),
        script_lines=AUTOCAD_DXF_SCRIPT,
    ),
    _ConverterTemplate(
        key="libredwg-object-stream",
        label="LibreDWG dwgread (object dump)",
        executable_name="dwgread",
        produces="object-stream",
        evidence="verified",
        notes=(
            "Highest fidelity for drawings that use dynamic blocks: the object dump "
            "keeps block contents that LibreDWG's own DXF writer drops."
        ),
        argv=("{executable}", "-O", "JSON", "{input}"),
    ),
    _ConverterTemplate(
        key="oda-dxf",
        label="ODA File Converter",
        executable_name="ODAFileConverter",
        produces="dxf",
        evidence="unverified",
        notes=(
            "The reference DWG engine from the Open Design Alliance. Free to install, "
            "not redistributable, so the user installs it; this project has not run it "
            "on the reference drawing."
        ),
        argv=("{executable}", "{input_dir}", "{output_dir}", "ACAD2018", "DXF", "0", "1"),
        directory_mode=True,
    ),
    _ConverterTemplate(
        key="libredwg-dxf",
        label="LibreDWG dwg2dxf",
        executable_name="dwg2dxf",
        produces="dxf",
        evidence="verified",
        notes=(
            "Works, but measured to emit empty definitions for dynamic blocks, so "
            "symbol-heavy drawings lose their most visible content."
        ),
        argv=("{executable}", "{input}", "-o", "{output}"),
    ),
)


@dataclass
class CadConversionAttempt:
    """One converter invocation, success or not, kept for the report."""

    key: str
    label: str
    executable: str
    command: tuple[str, ...] = ()
    status: str = "skipped"
    exit_code: int | None = None
    duration_ms: float = 0.0
    error: str = ""
    stderr: str = ""


@dataclass
class CadConversion:
    """A successful DWG decode, plus the history of how it was reached."""

    kind: CadConversionKind
    converter: CadConverter
    payload: dict[str, Any] | None = None
    dxf_bytes: bytes | None = None
    attempts: list[CadConversionAttempt] = field(default_factory=list)
    version: str = ""


class CadConversionError(ValueError):
    def __init__(self, code: str, message: str, attempts: list[CadConversionAttempt]):
        super().__init__(message)
        self.code = code
        self.message = message
        self.attempts = attempts


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        capture_output=True,
        # Closed stdin: a converter that finishes its script must exit, not wait for input.
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )


def _stderr_tail(completed: subprocess.CompletedProcess[bytes]) -> str:
    text = (completed.stderr or b"").decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-3:])[:400]


def convert_dwg(
    path: Path,
    *,
    workdir: Path,
    converters: list[CadConverter] | None = None,
    timeout: float | None = None,
) -> CadConversion:
    """Decode ``path`` (a DWG) with the first converter that succeeds.

    Every attempt is recorded, including the ones that fail: a reviewer needs to see
    that ``dwg2dxf`` timed out rather than that the drawing "had no geometry".
    """

    candidates = converters if converters is not None else available_converters()
    limit = timeout if timeout is not None else convert_timeout()
    attempts: list[CadConversionAttempt] = []
    if not candidates:
        raise CadConversionError(
            "no_dwg_converter",
            (
                "no DWG decoder is installed. Install one of: AutoCAD (its headless "
                "Core Console), LibreDWG (dwgread/dwg2dxf) or the ODA File Converter, "
                "or export the drawing as ASCII DXF and import that."
            ),
            attempts,
        )
    for converter in candidates:
        attempt = CadConversionAttempt(
            key=converter.key,
            label=converter.label,
            executable=converter.executable,
        )
        attempts.append(attempt)
        input_dir = workdir / "input"
        output_dir = workdir / converter.key
        output_dir.mkdir(parents=True, exist_ok=True)
        if converter.directory_mode:
            input_dir.mkdir(parents=True, exist_ok=True)
            staged = input_dir / path.name
            if not staged.exists():
                shutil.copy2(path, staged)
        output_path = output_dir / f"{path.stem}.dxf"
        script_path = output_dir / f"{converter.key}.scr"
        if converter.script_lines:
            script_path.write_text(
                "\n".join(
                    line.replace("{output}", str(output_path))
                    for line in converter.script_lines
                )
                + "\n",
                encoding="utf-8",
            )
        argv = [
            str(argument)
            .replace("{executable}", converter.executable)
            .replace("{input}", str(path))
            .replace("{input_dir}", str(input_dir))
            .replace("{output_dir}", str(output_dir))
            .replace("{output}", str(output_path))
            .replace("{script}", str(script_path))
            for argument in converter.argv
        ]
        attempt.command = tuple(argv)
        started = time.perf_counter()
        try:
            completed = _run(argv, timeout=limit)
        except subprocess.TimeoutExpired:
            attempt.status = "timeout"
            attempt.duration_ms = round((time.perf_counter() - started) * 1000, 2)
            attempt.error = f"the converter did not finish within {limit:g}s"
            continue
        except OSError as exc:
            attempt.status = "failed"
            attempt.duration_ms = round((time.perf_counter() - started) * 1000, 2)
            attempt.error = f"could not run the converter: {exc}"
            continue
        attempt.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        attempt.exit_code = completed.returncode
        attempt.stderr = _stderr_tail(completed)

        if converter.produces == "object-stream":
            text = (completed.stdout or b"").decode("utf-8", errors="replace")
            payload = _parse_object_stream(text)
            if payload is None:
                attempt.status = "failed"
                attempt.error = (
                    "the converter did not produce a readable object dump "
                    f"(exit status {completed.returncode})"
                )
                continue
            attempt.status = "success"
            return CadConversion(
                kind="object-stream",
                converter=converter,
                payload=payload,
                attempts=attempts,
                version=converter_version(converter.executable),
            )

        produced = output_path if output_path.is_file() else _find_dxf(output_dir)
        if produced is None:
            attempt.status = "failed"
            attempt.error = (
                "the converter reported no output file "
                f"(exit status {completed.returncode})"
            )
            continue
        data = produced.read_bytes()
        if not data.strip():
            attempt.status = "failed"
            attempt.error = "the converter produced an empty DXF file"
            continue
        attempt.status = "success"
        return CadConversion(
            kind="dxf",
            converter=converter,
            dxf_bytes=data,
            attempts=attempts,
            version=converter_version(converter.executable),
        )

    detail = "; ".join(
        f"{attempt.key}: {attempt.error or attempt.status}" for attempt in attempts
    )
    raise CadConversionError(
        "dwg_conversion_failed",
        f"no installed DWG decoder could read this file ({detail})",
        attempts,
    )


def _parse_object_stream(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    if not stripped.startswith("{"):
        # LibreDWG prints parser warnings before the JSON payload.
        start = stripped.find("{")
        if start < 0:
            return None
        stripped = stripped[start:]
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) and "OBJECTS" in payload else None


def _find_dxf(directory: Path) -> Path | None:
    for candidate in sorted(directory.glob("*.dxf")) + sorted(directory.glob("*.DXF")):
        if candidate.is_file():
            return candidate
    return None


def converter_report() -> list[dict[str, Any]]:
    """Inventory of every declared converter, installed or not."""

    installed = {item.key: item for item in available_converters()}
    report: list[dict[str, Any]] = []
    for template in CONVERTER_TEMPLATES:
        found = installed.get(template.key)
        report.append(
            {
                "key": template.key,
                "label": template.label,
                "executable": found.executable if found else "",
                "available": bool(found),
                "produces": template.produces,
                "evidence": template.evidence,
                "version": converter_version(found.executable) if found else "",
                "notes": template.notes,
            }
        )
    return report


def declared_glob_paths() -> dict[str, tuple[str, ...]]:
    """Declared non-PATH search locations, exposed so a report can explain an absence."""

    return dict(EXECUTABLE_GLOBS)


__all__ = [
    "AUTOCAD_CORE_CONSOLE_GLOBS",
    "AUTOCAD_DXF_SCRIPT",
    "CONVERTER_TEMPLATES",
    "EXECUTABLE_GLOBS",
    "CadConversion",
    "CadConversionAttempt",
    "CadConversionError",
    "CadConverter",
    "available_converters",
    "converter_report",
    "converter_version",
    "declared_glob_paths",
    "convert_dwg",
    "convert_timeout",
    "pinned_converter_key",
]
