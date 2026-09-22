from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import ValidationError

from .models import SymbolDefinition

HIDDEN_BUILTIN_SYMBOL_KEYS: dict[str, frozenset[str]] = {
    "symbols.json": frozenset({"system_interface"}),
    "standard_symbols.json": frozenset(
        {
            "off_page_connector",
            "temperature_transmitter",
            "flow_transmitter",
            "level_transmitter",
        }
    ),
}


class SymbolCatalogLoadError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: Path,
        entry_index: int | None = None,
        symbol_key: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.path = path
        self.entry_index = entry_index
        self.symbol_key = symbol_key


class SymbolRegistry:
    def __init__(self, search_paths: list[Path] | None = None):
        package_data = Path(__file__).parent / "data"
        builtin_paths = [
            package_data / "symbols.json",
            package_data / "standard_symbols.json",
            package_data / "flow_symbols.json",
        ]
        configured = os.getenv("PID_AGENT_SYMBOL_PATHS", os.getenv("AGENTCAD_SYMBOL_PATHS", ""))
        env_paths = [Path(item) for item in configured.split(os.pathsep) if item]
        self._search_paths = [*builtin_paths, *env_paths, *(search_paths or [])]
        self._symbols: dict[str, SymbolDefinition] = {}
        self._hidden_keys: set[str] = set()
        self.reload()

    def reload(self) -> None:
        symbols: dict[str, SymbolDefinition] = {}
        hidden_keys: set[str] = set()
        for path in self._search_paths:
            if not path.exists():
                continue
            files = sorted(path.glob("*.json")) if path.is_dir() else [path]
            for file_path in files:
                try:
                    raw_payload = file_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise SymbolCatalogLoadError(
                        "SYMBOL_FILE_READ_FAILED",
                        f"could not read symbol file: {exc}",
                        path=file_path,
                    ) from exc
                try:
                    payload = json.loads(raw_payload)
                except json.JSONDecodeError as exc:
                    raise SymbolCatalogLoadError(
                        "SYMBOL_FILE_JSON_INVALID",
                        (
                            "symbol file is not valid JSON at "
                            f"line {exc.lineno}, column {exc.colno}"
                        ),
                        path=file_path,
                    ) from exc
                if isinstance(payload, dict):
                    entries = payload.get("symbols", payload)
                    library_metadata = payload.get("library", {})
                else:
                    entries = payload
                    library_metadata = {}
                if not isinstance(entries, list):
                    raise SymbolCatalogLoadError(
                        "SYMBOL_FILE_ENTRIES_INVALID",
                        "symbol file must contain a top-level list or a 'symbols' list",
                        path=file_path,
                    )
                if not isinstance(library_metadata, dict):
                    raise SymbolCatalogLoadError(
                        "SYMBOL_LIBRARY_METADATA_INVALID",
                        "symbol library metadata must be an object",
                        path=file_path,
                    )
                file_keys: set[str] = set()
                for entry_index, raw in enumerate(entries):
                    try:
                        symbol = SymbolDefinition.model_validate(raw)
                    except ValidationError as exc:
                        first = exc.errors(
                            include_url=False,
                            include_context=False,
                            include_input=False,
                        )[0]
                        location = ".".join(str(item) for item in first["loc"]) or "entry"
                        raise SymbolCatalogLoadError(
                            "SYMBOL_FILE_SCHEMA_INVALID",
                            (
                                f"symbol entry {entry_index} is invalid at "
                                f"{location}: {first['msg']}"
                            ),
                            path=file_path,
                            entry_index=entry_index,
                        ) from exc
                    if symbol.key in file_keys:
                        raise SymbolCatalogLoadError(
                            "SYMBOL_FILE_DUPLICATE_KEY",
                            (
                                f"symbol key {symbol.key!r} appears more than once "
                                "in the same file"
                            ),
                            path=file_path,
                            entry_index=entry_index,
                            symbol_key=symbol.key,
                        )
                    file_keys.add(symbol.key)
                    if library_metadata:
                        symbol = symbol.model_copy(
                            update={
                                "metadata": {
                                    "library": library_metadata,
                                    **symbol.metadata,
                                }
                            }
                        )
                    symbols[symbol.key] = symbol
                    if symbol.key in HIDDEN_BUILTIN_SYMBOL_KEYS.get(file_path.name, frozenset()):
                        hidden_keys.add(symbol.key)
                    else:
                        hidden_keys.discard(symbol.key)
        self._symbols = symbols
        self._hidden_keys = hidden_keys

    def fingerprint(self) -> str:
        """A stable hash of the catalog validators actually consult.

        Validation evidence binds the document hash and the profile fingerprint, but the
        drafting and graph checks also read this catalog: the same drawing can produce
        different findings after a symbol is added, edited or hidden. Without this, two
        runs that disagree would look unreproducible (remote baseline R2 §3.4). Hidden
        keys are excluded because nothing can reference them.
        """

        payload = [
            {"key": key, **symbol.model_dump(mode="json")}
            for key, symbol in sorted(self._symbols.items())
            if key not in self._hidden_keys
        ]
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def list(self) -> list[SymbolDefinition]:
        return sorted(
            (symbol for key, symbol in self._symbols.items() if key not in self._hidden_keys),
            key=lambda item: (item.category, item.name),
        )

    def exists(self, key: str) -> bool:
        """Whether the catalogue defines this key at all, hidden or not."""

        return key in self._symbols

    def is_hidden(self, key: str) -> bool:
        """Whether the key is defined but withheld from the model's catalogue listing.

        ``list()`` excludes these, so ``as_prompt_catalog`` never shows them and a model can
        only use one by already knowing the key. Distinguishing this from "not defined" is
        the whole point of the audit: a suppressed-but-renderable symbol is a visibility bug
        with a one-line fix, while a key that does not exist is a catalogue gap.
        """

        return key in self._hidden_keys

    def get(self, key: str) -> SymbolDefinition:
        try:
            return self._symbols[key]
        except KeyError as exc:
            raise KeyError(f"unknown symbol: {key}") from exc

    def as_prompt_catalog(self) -> str:
        def port_side(symbol: SymbolDefinition, x: float, y: float) -> str:
            distances = {
                "left": abs(x),
                "right": abs(symbol.width - x),
                "top": abs(y),
                "bottom": abs(symbol.height - y),
            }
            side, distance = min(distances.items(), key=lambda item: (item[1], item[0]))
            tolerance = max(1.0, min(symbol.width, symbol.height) * 0.08)
            return side if distance <= tolerance else "interior"

        rows = [
            "Catalog and selection contract:",
            "- Match the user's exact equipment and valve function; never use a visually similar generic symbol when an exact catalog symbol exists.",
            "- ball_valve: explicit ball valve or generic quick isolation only; gate_valve: gate/full-bore isolation; globe_valve: globe/manual throttling; control_valve: actuated process control; check_valve: one-way non-return; butterfly_valve: butterfly/large-line isolation; needle_valve: instrument root or fine throttling; safety_relief_valve: overpressure protection.",
            "- Port direction is process semantics. Forward flow is out/bidirectional → in/bidirectional. Port side is spatial geometry and controls how the pipe must leave/approach the symbol.",
            "- Build connectivity with real ports and semantic connectors; never draw decorative pipes or standalone arrow text.",
            "- Use off_page_connector_in/out for cross-drawing boundaries and set properties.target_document_id.",
            "- Preserve unrelated elements, document identity and expected_revision.",
            "",
            "Available symbols:",
        ]
        for symbol in self.list():
            ports = ", ".join(
                f"{port.id}:{port.name}[flow={port.direction},medium={port.medium},"
                f"side={port_side(symbol, port.x, port.y)},offset=({port.x},{port.y})]"
                for port in symbol.ports
            ) or "none"
            capabilities = ", ".join(
                f"{key}={value}"
                for key, value in symbol.metadata.items()
                if key in {"capability", "opc_direction"}
            )
            suffix = f"; capabilities={capabilities}" if capabilities else ""
            rows.append(
                f"- {symbol.key}: {symbol.name} / {symbol.category}; "
                f"size={symbol.width}x{symbol.height}; ports={ports}{suffix}; {symbol.description}"
            )
        return "\n".join(rows)
