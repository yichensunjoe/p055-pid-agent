"""Tests for the external DWG converter adapter.

The adapter is where an outside process enters the pipeline, so these tests use fake
converters (small executable scripts on a controlled ``PATH``) rather than whatever
happens to be installed: they pin the preference order, the fallback when a converter
fails, the timeout path, the "reported success but produced nothing" path, and the
evidence label that decides whether the resulting drawing gets a warning.
"""

from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path

import pytest
from cad_fixtures import ObjectStreamBuilder, simple_dxf

from agentcad import cad_convert

READER = """#!/usr/bin/env python3
import json, sys
payload = json.loads(sys.stdin.read() or "{}")
"""


def _write_executable(path: Path, body: str) -> Path:
    """Install a fake converter that runs under this interpreter.

    The shebang pins the absolute interpreter path on purpose: the fixture narrows
    ``PATH`` to the fake bin directory, so ``/usr/bin/env python3`` would find nothing.
    """

    lines = body.splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    path.write_text(f"#!{sys.executable}\n" + "\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture()
def isolated_path(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A ``PATH`` that contains only the fake converters this test installs."""

    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(cad_convert, "EXTRA_EXECUTABLE_DIRS", ())
    monkeypatch.setattr(cad_convert, "EXTRA_APPLICATION_GLOBS", ())
    # The declared application globs are neutralized too: a developer machine that happens
    # to have AutoCAD installed must not change what these tests observe.
    monkeypatch.setattr(cad_convert, "EXECUTABLE_GLOBS", {})
    monkeypatch.delenv("PID_AGENT_CAD_CONVERTER", raising=False)
    cad_convert.converter_version.cache_clear()
    return bindir


def _object_stream_payload() -> str:
    builder = ObjectStreamBuilder()
    builder.model_space([builder.line((0.0, 0.0), (10.0, 5.0))])
    return json.dumps(builder.build())


def _install_reader(bindir: Path, *, exit_code: int = 0, payload: str | None = None) -> Path:
    body = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({(payload if payload is not None else _object_stream_payload())!r})\n"
        f"sys.exit({exit_code})\n"
    )
    return _write_executable(bindir / "dwgread", body)


def _install_dxf_writer(bindir: Path, *, exit_code: int = 0, write: bool = True) -> Path:
    body = (
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "target = pathlib.Path(sys.argv[3]) if len(sys.argv) > 3 else pathlib.Path('out.dxf')\n"
        f"target.write_bytes({simple_dxf()!r})\n"
        if write
        else "#!/usr/bin/env python3\nimport sys\n"
    )
    body += f"sys.exit({exit_code})\n"
    return _write_executable(bindir / "dwg2dxf", body)


def _dwg_bytes() -> bytes:
    return b"AC1032" + b"\x00" * 64


def test_no_converter_installed_reports_a_usable_message(isolated_path, tmp_path) -> None:
    with pytest.raises(cad_convert.CadConversionError) as excinfo:
        cad_convert.convert_dwg(tmp_path / "drawing.dwg", workdir=tmp_path)

    assert excinfo.value.code == "no_dwg_converter"
    assert "DXF" in excinfo.value.message


def test_object_stream_converter_is_preferred(isolated_path, tmp_path) -> None:
    _install_reader(isolated_path)
    source = tmp_path / "drawing.dwg"
    source.write_bytes(_dwg_bytes())

    conversion = cad_convert.convert_dwg(source, workdir=tmp_path)

    assert conversion.kind == "object-stream"
    assert conversion.converter.key == "libredwg-object-stream"
    assert conversion.payload is not None
    assert conversion.attempts[0].status == "success"
    assert conversion.attempts[0].command[1:3] == ("-O", "JSON")


def test_dxf_converter_is_used_when_it_is_the_only_one(isolated_path, tmp_path) -> None:
    _install_dxf_writer(isolated_path)
    source = tmp_path / "drawing.dwg"
    source.write_bytes(_dwg_bytes())

    conversion = cad_convert.convert_dwg(source, workdir=tmp_path)

    assert conversion.kind == "dxf"
    assert conversion.converter.key == "libredwg-dxf"
    assert conversion.dxf_bytes is not None
    assert b"SECTION" in conversion.dxf_bytes


def test_failing_converter_falls_back_and_records_the_attempt(isolated_path, tmp_path) -> None:
    # The reader emits nothing; the DXF writer succeeds.
    _install_reader(isolated_path, payload="")
    _install_dxf_writer(isolated_path)
    source = tmp_path / "drawing.dwg"
    source.write_bytes(_dwg_bytes())

    conversion = cad_convert.convert_dwg(source, workdir=tmp_path)

    assert conversion.kind == "dxf"
    statuses = [(attempt.key, attempt.status) for attempt in conversion.attempts]
    assert statuses == [
        ("libredwg-object-stream", "failed"),
        ("libredwg-dxf", "success"),
    ]
    assert "object dump" in conversion.attempts[0].error


def test_a_converter_that_produces_nothing_is_not_a_success(isolated_path, tmp_path) -> None:
    _install_reader(isolated_path, payload="")
    source = tmp_path / "drawing.dwg"
    source.write_bytes(_dwg_bytes())

    with pytest.raises(cad_convert.CadConversionError) as excinfo:
        cad_convert.convert_dwg(source, workdir=tmp_path)

    assert excinfo.value.code == "dwg_conversion_failed"
    assert excinfo.value.attempts[0].status == "failed"


def test_converter_timeout_is_reported_as_a_timeout(isolated_path, tmp_path) -> None:
    _write_executable(
        isolated_path / "dwgread",
        "#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n",
    )
    source = tmp_path / "drawing.dwg"
    source.write_bytes(_dwg_bytes())

    with pytest.raises(cad_convert.CadConversionError) as excinfo:
        cad_convert.convert_dwg(source, workdir=tmp_path, timeout=0.5)

    assert excinfo.value.attempts[0].status == "timeout"
    assert "did not finish" in excinfo.value.attempts[0].error


def test_partial_output_with_a_warning_prefix_is_still_parsed(isolated_path, tmp_path) -> None:
    """LibreDWG prints parser warnings before the JSON payload."""

    _install_reader(
        isolated_path,
        payload="WARNING: unknown section\n" + _object_stream_payload(),
    )
    source = tmp_path / "drawing.dwg"
    source.write_bytes(_dwg_bytes())

    conversion = cad_convert.convert_dwg(source, workdir=tmp_path)

    assert conversion.kind == "object-stream"
    assert conversion.payload is not None
    assert "OBJECTS" in conversion.payload


def test_pinned_converter_environment_variable_selects_one(
    isolated_path, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_reader(isolated_path)
    _install_dxf_writer(isolated_path)
    monkeypatch.setenv("PID_AGENT_CAD_CONVERTER", "libredwg-dxf")
    source = tmp_path / "drawing.dwg"
    source.write_bytes(_dwg_bytes())

    conversion = cad_convert.convert_dwg(source, workdir=tmp_path)

    assert conversion.converter.key == "libredwg-dxf"


def test_version_is_read_from_the_executable(isolated_path) -> None:
    _write_executable(
        isolated_path / "dwgread",
        "#!/usr/bin/env python3\nimport sys\nsys.stdout.write('dwgread 9.9.9\\n')\n",
    )
    cad_convert.converter_version.cache_clear()

    converters = cad_convert.available_converters()
    version = cad_convert.converter_version(converters[0].executable)

    assert version == "dwgread 9.9.9"


def test_converter_inventory_declares_evidence_for_every_candidate(isolated_path) -> None:
    _install_reader(isolated_path)
    report = {item["key"]: item for item in cad_convert.converter_report()}

    assert report["libredwg-object-stream"]["available"] is True
    assert report["libredwg-object-stream"]["evidence"] == "verified"
    assert report["libredwg-dxf"]["available"] is False
    assert report["oda-dxf"]["evidence"] == "unverified"
    assert all(item["notes"] for item in report.values())


def test_oda_directory_mode_conversion_is_attempted(isolated_path, tmp_path) -> None:
    body = (
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "out = pathlib.Path(sys.argv[2])\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        f"(out / 'converted.dxf').write_bytes({simple_dxf()!r})\n"
    )
    _write_executable(isolated_path / "ODAFileConverter", body)
    source = tmp_path / "drawing.dwg"
    source.write_bytes(_dwg_bytes())

    conversion = cad_convert.convert_dwg(source, workdir=tmp_path)

    assert conversion.converter.key == "oda-dxf"
    assert conversion.converter.evidence == "unverified"
    assert conversion.dxf_bytes is not None


def test_converter_arguments_are_literal_argv_and_a_staged_path(isolated_path, tmp_path) -> None:
    """Metacharacters in a filename never reach the command, and the source is untouched.

    The converter is handed a fixed ``input/source.dwg`` inside its own work directory,
    so shell metacharacters in the operator's name cannot be interpreted by anything; the
    argument list is still passed as argv, never through a shell.
    """

    _install_reader(isolated_path)
    source = tmp_path / "weird; rm -rf $(x).dwg"
    source.write_bytes(_dwg_bytes())

    conversion = cad_convert.convert_dwg(source, workdir=tmp_path)

    assert conversion.kind == "object-stream"
    command = conversion.attempts[0].command
    assert not any("weird" in argument for argument in command)
    assert str(tmp_path / "input" / cad_convert.STAGED_SOURCE_NAME) in command
    assert source.exists(), "the operator's own file must be left alone"


def test_timeout_configuration_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PID_AGENT_CAD_CONVERT_TIMEOUT_SECONDS", "30")
    assert cad_convert.convert_timeout() == 30.0
    # A very small value is clamped upward: a converter given 0.1s would report a
    # timeout on every drawing and look like a decode failure.
    monkeypatch.setenv("PID_AGENT_CAD_CONVERT_TIMEOUT_SECONDS", "0.1")
    assert cad_convert.convert_timeout() == 5.0
    monkeypatch.setenv("PID_AGENT_CAD_CONVERT_TIMEOUT_SECONDS", "999999")
    assert cad_convert.convert_timeout() == cad_convert.MAX_CONVERT_TIMEOUT_SECONDS
    monkeypatch.setenv("PID_AGENT_CAD_CONVERT_TIMEOUT_SECONDS", "not a number")
    assert cad_convert.convert_timeout() == cad_convert.DEFAULT_CONVERT_TIMEOUT_SECONDS
    monkeypatch.delenv("PID_AGENT_CAD_CONVERT_TIMEOUT_SECONDS")
    assert cad_convert.convert_timeout() == cad_convert.DEFAULT_CONVERT_TIMEOUT_SECONDS


def test_environment_locale_is_forced_to_c_for_stable_parsing(
    isolated_path, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Converter output is parsed, so its locale must not change its formatting."""

    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    body = (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "sys.stderr.write('locale=' + os.environ.get('LC_ALL', '') + '\\n')\n"
        f"sys.stdout.write({_object_stream_payload()!r})\n"
    )
    _write_executable(isolated_path / "dwgread", body)
    source = tmp_path / "drawing.dwg"
    source.write_bytes(_dwg_bytes())

    conversion = cad_convert.convert_dwg(source, workdir=tmp_path)

    assert conversion.kind == "object-stream"
    assert "locale=C" in conversion.attempts[0].stderr


# --------------------------------------------------------------------------- AutoCAD #
#
# AutoCAD's headless Core Console is the highest-fidelity decoder available — it is the
# engine that owns the DWG format — but it is also the most awkward to drive: it is not a
# PATH executable, its name differs per platform, and it is driven by a *script* rather
# than by an output argument. These tests pin each of those properties with fakes, so they
# hold on a machine that has never seen AutoCAD installed.


def _install_fake_console(directory: Path, body: str) -> Path:
    """Install a fake ``accoreconsole`` (the name the version probe keys on)."""

    directory.mkdir(parents=True, exist_ok=True)
    return _write_executable(directory / "accoreconsole", body)


def _console_glob(tmp_path: Path) -> str:
    """A glob pattern shaped like the real macOS AutoCAD layout, rooted in ``tmp_path``."""

    return str(tmp_path / "Autodesk" / "AutoCAD *" / "AutoCAD *.app" / "Contents" / "Helpers"
               / "AcCoreConsole.app" / "Contents" / "MacOS" / "accoreconsole")


def test_autocad_console_is_found_in_a_year_stamped_application_bundle(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "Autodesk" / "AutoCAD 2027" / "AutoCAD 2027.app" / "Contents" / "Helpers" / "AcCoreConsole.app" / "Contents" / "MacOS"
    _install_fake_console(bundle, "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    monkeypatch.setattr(cad_convert, "EXTRA_EXECUTABLE_DIRS", ())
    monkeypatch.setattr(cad_convert, "EXTRA_APPLICATION_GLOBS", ())
    monkeypatch.setattr(cad_convert, "EXECUTABLE_GLOBS", {"autocad-core-console": (_console_glob(tmp_path),)})
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    converters = {item.key: item for item in cad_convert.available_converters()}

    assert "autocad-core-console" in converters
    assert converters["autocad-core-console"].executable == str(bundle / "accoreconsole")
    assert converters["autocad-core-console"].produces == "dxf"


def test_autocad_console_is_normalized_away_when_not_installed(
    isolated_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No AutoCAD means the declared search finds nothing rather than inventing a path."""

    monkeypatch.setattr(
        cad_convert,
        "EXECUTABLE_GLOBS",
        {"autocad-core-console": ("/nonexistent/AutoCAD */AcCoreConsole",)},
    )
    keys = [item.key for item in cad_convert.available_converters()]

    assert "autocad-core-console" not in keys


def test_autocad_console_leads_the_decoder_order() -> None:
    """Order is a fidelity claim: the official engine is tried before the free ones."""

    keys = [template.key for template in cad_convert.CONVERTER_TEMPLATES]

    assert keys[0] == "autocad-core-console"
    assert keys.index("autocad-core-console") < keys.index("libredwg-object-stream")
    assert keys.index("libredwg-object-stream") < keys.index("libredwg-dxf")
    assert keys == sorted(keys, key=lambda key: keys.index(key))  # stable, no duplicates
    assert len(set(keys)) == len(keys)


def test_autocad_script_avoids_the_command_repeat_trap() -> None:
    """An empty line at AutoCAD's command prompt repeats the previous command.

    A hand-written script with blank lines re-enters ``DXFOUT`` and leaves the console
    waiting for input, which is exactly the bug this pins.
    """

    lines = list(cad_convert.AUTOCAD_DXF_SCRIPT)

    assert all(line.strip() for line in lines)
    assert lines[0] == "FILEDIA" and lines[1] == "0"
    assert lines[2] == "DXFOUT"
    assert lines[3] == "{output}"


def test_scripted_converter_writes_its_script_and_reads_it_back(
    isolated_path, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scripted path is exercised end to end with a fake console.

    The fake reads the ``.scr`` it was handed, follows it to the output path, and writes
    that file — so a wrong ``{script}`` substitution or a wrong ``{output}`` substitution
    fails the test rather than passing silently.
    """

    body = (
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"(pathlib.Path(sys.argv[4]).read_text(encoding='utf-8'))\n"
        "script = pathlib.Path(sys.argv[4]).read_text(encoding='utf-8').splitlines()\n"
        "target = pathlib.Path(script[3])\n"
        "target.parent.mkdir(parents=True, exist_ok=True)\n"
        f"target.write_bytes({simple_dxf()!r})\n"
    )
    _install_fake_console(isolated_path, body)
    monkeypatch.setattr(cad_convert, "EXTRA_EXECUTABLE_DIRS", ())  # keep the isolated PATH
    source = tmp_path / "drawing.dwg"
    source.write_bytes(_dwg_bytes())

    conversion = cad_convert.convert_dwg(source, workdir=tmp_path, converters=[
        converter for converter in cad_convert.available_converters()
        if converter.key == "autocad-core-console"
    ])

    assert conversion.kind == "dxf"
    assert conversion.dxf_bytes is not None and b"SECTION" in conversion.dxf_bytes
    script = tmp_path / "autocad-core-console" / cad_convert.CONVERTER_SCRIPT_NAME
    assert script.is_file()
    assert str(script) in conversion.attempts[0].command


def test_converter_paths_never_contain_the_operators_filename(isolated_path, tmp_path) -> None:
    """A filename is data, never a path, an argument or a script line (M4-0.1).

    The names below are the ones that would matter if this were got wrong: AutoCAD's
    console executes a *command script*, so a newline in a filename becomes a second
    command, and the same names double as path separators and shell metacharacters.
    """

    body = (
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "script = pathlib.Path([a for a in sys.argv if a.endswith('.scr')][0])\n"
        "record = script.parent / 'observed.txt'\n"
        "lines = ['argv=' + repr(sys.argv)]\n"
        "lines.append('script=' + repr(script.read_text(encoding='utf-8')))\n"
        "lines.append('files=' + repr(sorted(p.name for p in script.parent.iterdir())))\n"
        "record.write_text(chr(10).join(lines), encoding='utf-8')\n"
        "target = pathlib.Path(script.read_text(encoding='utf-8').splitlines()[3])\n"
        f"target.write_bytes({simple_dxf()!r})\n"
    )
    _install_fake_console(isolated_path, body)

    hostile = 'evil"; DXFOUT ok;\nQUIT.dwg'
    source = tmp_path / "hostile" / hostile
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_dwg_bytes())
    conversion = cad_convert.convert_dwg(source, workdir=tmp_path, converters=[
        converter for converter in cad_convert.available_converters()
        if converter.key == "autocad-core-console"
    ])

    observed = (tmp_path / "autocad-core-console" / "observed.txt").read_text(encoding="utf-8")
    assert hostile not in observed, observed
    assert "evil" not in observed, observed
    assert cad_convert.STAGED_SOURCE_NAME in observed
    assert conversion.kind == "dxf"


def test_a_non_zero_exit_is_a_failure_even_with_usable_looking_output(
    isolated_path, tmp_path
) -> None:
    """M4-0.5: a converter that exited non-zero may have written half a file."""

    _install_reader(isolated_path, exit_code=1)
    _install_dxf_writer(isolated_path)
    source = tmp_path / "drawing.dwg"
    source.write_bytes(_dwg_bytes())

    conversion = cad_convert.convert_dwg(source, workdir=tmp_path)

    statuses = [(attempt.key, attempt.status) for attempt in conversion.attempts]
    assert statuses == [
        ("libredwg-object-stream", "failed"),
        ("libredwg-dxf", "success"),
    ]
    assert "exited with status 1" in conversion.attempts[0].error
    assert conversion.converter.key == "libredwg-dxf"


def test_a_non_zero_exit_on_a_dxf_converter_fails_too(isolated_path, tmp_path) -> None:
    _install_dxf_writer(isolated_path, exit_code=3)
    source = tmp_path / "drawing.dwg"
    source.write_bytes(_dwg_bytes())

    with pytest.raises(cad_convert.CadConversionError) as excinfo:
        cad_convert.convert_dwg(source, workdir=tmp_path)

    assert excinfo.value.attempts[0].status == "failed"
    assert "exited with status 3" in excinfo.value.attempts[0].error
    assert excinfo.value.attempts[0].exit_code == 3


def test_the_version_probe_cannot_hang_on_a_silent_process(
    isolated_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4-0.6: the deadline must hold when the process prints nothing at all.

    The fake prints a partial line and never writes a newline, which is the case a
    blocking ``for line in stdout`` cannot escape from.
    """

    monkeypatch.setattr(cad_convert, "AUTOCAD_CONSOLE_PROBE_SECONDS", 1.0)
    _install_fake_console(
        isolated_path,
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "sys.stdout.write('AutoCAD Core Engine Console - starting')\n"
        "sys.stdout.flush()\n"
        "time.sleep(300)\n",
    )
    cad_convert.converter_version.cache_clear()
    started = time.monotonic()

    version = cad_convert.converter_version(str(isolated_path / "accoreconsole"))
    elapsed = time.monotonic() - started

    assert elapsed < 15.0, f"the probe took {elapsed:.1f}s, so it is not bounded"
    assert version == ""


def test_autocad_build_is_read_from_the_startup_banner(
    isolated_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--version`` is not a thing the console answers, so the banner is the evidence."""

    monkeypatch.setattr(cad_convert, "AUTOCAD_CONSOLE_PROBE_SECONDS", 10.0)
    _install_fake_console(
        isolated_path,
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "sys.stdout.write('AutoCAD Core Engine Console - Copyright 2026 Autodesk, Inc. (X.60.M.161)\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",  # the real console never exits without a script
    )
    cad_convert.converter_version.cache_clear()

    version = cad_convert.converter_version(str(isolated_path / "accoreconsole"))

    assert "X.60.M.161" in version


def test_declared_glob_paths_are_reportable() -> None:
    """Capabilities can explain *where* it looked, not only that it failed."""

    declared = cad_convert.declared_glob_paths()

    assert "autocad-core-console" in declared
    patterns = declared["autocad-core-console"]
    assert patterns, "the declared search locations must not be empty"
    assert all("autocad" in pattern.lower() for pattern in patterns)
    assert any("AcCoreConsole" in pattern for pattern in patterns)


def test_the_real_converter_is_only_used_when_it_exists() -> None:
    """On a machine without LibreDWG the capability list must not claim DWG support."""

    installed = [item.key for item in cad_convert.available_converters()]
    report = cad_convert.converter_report()
    available = {item["key"] for item in report if item["available"]}
    assert available == set(installed)
