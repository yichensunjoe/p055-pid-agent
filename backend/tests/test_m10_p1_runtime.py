"""M10-P1 hard locks (Gate M10-P1 CODE GO frozen test set).

Locks, in the Gate's own words:
* class identity: agentcad.models.StrictModel IS agentcad.runtime.primitives.StrictModel
  (same for utc_now); legacy ``from agentcad.models import ...`` keeps working;
* representative pydantic validation / JSON-schema behaviour is unchanged;
* runtime modules import no models/service/store/audit/audit_models/tool_registry
  or any P&ID/M6/M7/review/release module — every non-runtime agentcad import
  must be TYPE_CHECKING-only (AST lock) and nothing P&ID loads at runtime
  (subprocess isolation, R10-8 corrected logic: startswith("agentcad.") with
  the root package excluded);
* runtime/__init__ stays lazy (no eager imports, no cycles);
* schema version is still 13.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "agentcad"
RUNTIME_DIR = PACKAGE_ROOT / "runtime"


class _ImportCollector(ast.NodeVisitor):
    """Collect every imported module with whether it sits under ``if TYPE_CHECKING``."""

    def __init__(self) -> None:
        self.imports: list[tuple[str, bool]] = []
        self._type_checking = False

    def visit_If(self, node: ast.If) -> None:
        is_tc = isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
        previous = self._type_checking
        self._type_checking = previous or is_tc
        self.generic_visit(node)
        self._type_checking = previous

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append((alias.name, self._type_checking))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level > 0:
            base = "agentcad.runtime"
            module = base + ("." + node.module if node.module else "")
        else:
            module = node.module or ""
        if module and module != "__future__":
            self.imports.append((module, self._type_checking))


def test_primitives_class_identity_frozen() -> None:
    from agentcad import harness_models, models
    from agentcad.runtime import primitives

    assert models.StrictModel is primitives.StrictModel
    assert models.utc_now is primitives.utc_now
    # The harness model module must see the very same objects.
    assert harness_models.StrictModel is primitives.StrictModel
    assert harness_models.utc_now is primitives.utc_now


def test_legacy_import_paths_keep_working() -> None:
    # Historical call sites must not break.
    from agentcad.models import StrictModel, utc_now  # noqa: F401
    from agentcad.runtime.primitives import StrictModel as RuntimeStrictModel

    assert StrictModel is RuntimeStrictModel
    assert utc_now().tzinfo is not None


def test_validation_and_schema_behaviour_unchanged() -> None:
    from pydantic import ValidationError

    from agentcad.models import Point, StrictModel

    point = Point(x=1.5, y=-2)
    assert point.model_dump() == {"x": 1.5, "y": -2.0}
    schema = Point.model_json_schema()
    assert schema.get("additionalProperties") is False

    class Sample(StrictModel):
        name: str
        count: int = 0

    assert Sample(name="a").count == 0
    with pytest.raises(ValidationError):
        Sample(name="a", unknown=True)  # extra="forbid" preserved
    sample = Sample(name="a")
    with pytest.raises(ValidationError):
        sample.count = "not-an-int"  # validate_assignment preserved


def test_runtime_sources_import_lock() -> None:
    for source_path in sorted(RUNTIME_DIR.glob("*.py")):
        collector = _ImportCollector()
        collector.visit(ast.parse(source_path.read_text(encoding="utf-8")))
        for module, type_checking_only in collector.imports:
            if not module.startswith("agentcad"):
                continue  # third-party / stdlib imports are unrestricted
            if module == "agentcad" or module.startswith("agentcad.runtime"):
                continue  # self and root package are allowed
            assert type_checking_only, (
                f"{source_path.name}: runtime import of {module!r} must be "
                "TYPE_CHECKING-only (no P&ID module may load at runtime)"
            )


def test_runtime_isolation_subprocess_lock() -> None:
    """R10-8 corrected logic: the root package 'agentcad' is not a violation."""
    code = (
        "import sys\n"
        "import agentcad.runtime\n"
        "import agentcad.runtime.primitives\n"
        "import agentcad.runtime.ports\n"
        "bad = ["
        "m for m in sys.modules"
        " if m.startswith('agentcad.') and not m.startswith('agentcad.runtime')]\n"
        "assert not bad, f'runtime leaked P&ID modules: {bad}'\n"
        "assert 'agentcad.harness' not in sys.modules, 'runtime/__init__ must stay lazy'\n"
        "print('isolation-ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "isolation-ok" in result.stdout


def test_schema_version_still_13() -> None:
    from agentcad.database_recovery import CURRENT_SCHEMA_VERSION

    assert CURRENT_SCHEMA_VERSION == 13
