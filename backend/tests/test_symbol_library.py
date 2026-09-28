import json
from pathlib import Path

import pytest

from agentcad.models import Document, Point, SymbolDefinition, SymbolElement
from agentcad.svg import render_svg
from agentcad.symbols import SymbolCatalogLoadError, SymbolRegistry

DATA_DIR = Path(__file__).parents[1] / "agentcad" / "data"
BUILTIN_FILES = (
    DATA_DIR / "symbols.json",
    DATA_DIR / "standard_symbols.json",
    DATA_DIR / "flow_symbols.json",
)

LEGACY_KEYS = {
    "ball_valve",
    "centrifugal_pump",
    "control_valve",
    "flow_indicator",
    "gas_tank",
    "gate_valve",
    "heat_exchanger",
    "heat_exchanger_horizontal_shell",
    "pressure_indicator",
    "system_interface",
    "temperature_indicator",
}

HIDDEN_BUILTIN_KEYS = {
    "system_interface",
    "off_page_connector",
    "flow_transmitter",
    "level_transmitter",
}

REQUIRED_STANDARD_KEYS = {
    "agitator",
    "air_cooler",
    "analyzer_indicator",
    "basket_strainer",
    "blind_flange",
    "butterfly_valve",
    "check_valve",
    "condenser",
    "eccentric_reducer",
    "flame_arrester",
    "flexible_hose",
    "floor_drain",
    "globe_valve",
    "level_gauge",
    "metal_expansion_joint",
    "needle_valve",
    "off_page_connector_in",
    "off_page_connector_out",
    "orifice_plate",
    "plate_heat_exchanger",
    "positive_displacement_pump",
    "rupture_disc",
    "safety_relief_valve",
    "separator_vessel",
    "steam_trap",
    "three_way_valve",
    "temperature_element",
    "vent_to_atmosphere",
}

REQUIRED_CATEGORIES = {
    "安全附件",
    "泵",
    "管件",
    "管道附件",
    "过滤设备",
    "混合设备",
    "换热设备",
    "排放与边界",
    "边界与跨图连接",
    "容器",
    "仪表",
    "阀门",
    "风机",
}


def _load_file(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload["symbols"], list)
    return payload["symbols"]


def test_builtin_symbol_json_loads_without_duplicate_or_legacy_key_override(monkeypatch):
    monkeypatch.delenv("PID_AGENT_SYMBOL_PATHS", raising=False)
    monkeypatch.delenv("AGENTCAD_SYMBOL_PATHS", raising=False)

    builtin_entries = [_load_file(path) for path in BUILTIN_FILES]
    legacy = builtin_entries[0]
    legacy_keys = {item["key"] for item in legacy}
    nonlegacy_keys = {
        item["key"] for entries in builtin_entries[1:] for item in entries
    }
    all_entries = [item for entries in builtin_entries for item in entries]
    all_keys = [item["key"] for item in all_entries]

    assert legacy_keys == LEGACY_KEYS
    assert legacy_keys.isdisjoint(nonlegacy_keys)
    assert len(all_keys) == len(set(all_keys))
    assert all(SymbolDefinition.model_validate(item) for item in all_entries)

    registry = SymbolRegistry()
    registry_keys = {item.key for item in registry.list()}
    assert registry_keys == set(all_keys) - HIDDEN_BUILTIN_KEYS
    assert HIDDEN_BUILTIN_KEYS.isdisjoint(registry_keys)
    library = registry.get("condenser").metadata["library"]
    assert library["name"] == "P&ID-Agent 内置标准图例库"
    assert library["version"] == "2026.1"
    opc_in = registry.get("off_page_connector_in")
    opc_out = registry.get("off_page_connector_out")
    assert opc_in.metadata["library"]["name"] == "P&ID-Agent 流向与跨图连接图例"
    assert opc_in.width == opc_out.width == 100
    assert opc_in.height == opc_out.height == 50
    assert opc_in.ports[0].x == 100
    assert opc_out.ports[0].x == 0
    assert opc_in.shapes[0]["points"][2] == [100, 25]


def test_rotated_opc_keeps_embedded_direction_text_upright():
    registry = SymbolRegistry()
    definition = registry.get("off_page_connector_in")
    document = Document(
        elements=[
            SymbolElement(
                id="reverse_inlet",
                symbol_key=definition.key,
                position=Point(x=100, y=100),
                width=definition.width,
                height=definition.height,
                rotation=180,
            )
        ]
    )

    svg = render_svg(document, registry)

    assert 'transform="rotate(-180.0 47 30)"' in svg


def test_external_symbol_override_does_not_inherit_builtin_library_metadata(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("PID_AGENT_SYMBOL_PATHS", raising=False)
    monkeypatch.delenv("AGENTCAD_SYMBOL_PATHS", raising=False)
    override_path = tmp_path / "override.json"
    override_path.write_text(
        json.dumps(
            {
                "symbols": [
                    {
                        "key": "condenser",
                        "name": "单位专用冷凝器",
                        "category": "单位图例",
                        "description": "单位覆盖定义",
                        "width": 80,
                        "height": 40,
                        "ports": [],
                        "shapes": [
                            {
                                "type": "rect",
                                "x": 0,
                                "y": 0,
                                "width": 80,
                                "height": 40,
                            }
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    overridden = SymbolRegistry(search_paths=[override_path]).get("condenser")

    assert overridden.name == "单位专用冷凝器"
    assert overridden.category == "单位图例"
    assert overridden.metadata == {}


def test_duplicate_key_in_one_symbol_file_is_rejected(tmp_path: Path):
    duplicate_path = tmp_path / "duplicate.json"
    symbol = {
        "key": "duplicate_fixture",
        "name": "重复图例",
        "category": "测试",
        "description": "用于验证同一文件内重复 key。",
        "width": 20,
        "height": 20,
        "ports": [],
        "shapes": [{"type": "line", "x1": 0, "y1": 10, "x2": 20, "y2": 10}],
    }
    duplicate_path.write_text(
        json.dumps({"symbols": [symbol, {**symbol, "name": "重复图例二"}]}),
        encoding="utf-8",
    )

    with pytest.raises(SymbolCatalogLoadError) as raised:
        SymbolRegistry(search_paths=[duplicate_path])

    assert raised.value.code == "SYMBOL_FILE_DUPLICATE_KEY"
    assert raised.value.symbol_key == "duplicate_fixture"
    assert raised.value.entry_index == 1


def test_same_key_in_separate_files_remains_a_legal_ordered_override(tmp_path: Path):
    base_path = tmp_path / "01-base.json"
    override_path = tmp_path / "02-override.json"

    def payload(name: str):
        return {
            "symbols": [
                {
                    "key": "layered_fixture",
                    "name": name,
                    "category": "测试",
                    "description": "用于验证跨文件覆盖。",
                    "width": 20,
                    "height": 20,
                    "ports": [],
                    "shapes": [
                        {"type": "line", "x1": 0, "y1": 10, "x2": 20, "y2": 10}
                    ],
                }
            ]
        }

    base_path.write_text(json.dumps(payload("基础定义")), encoding="utf-8")
    override_path.write_text(json.dumps(payload("覆盖定义")), encoding="utf-8")

    registry = SymbolRegistry(search_paths=[base_path, override_path])

    assert registry.get("layered_fixture").name == "覆盖定义"


def test_builtin_symbol_ports_are_unique_and_inside_local_geometry():
    entries = [item for path in BUILTIN_FILES for item in _load_file(path)]

    for raw in entries:
        symbol = SymbolDefinition.model_validate(raw)
        port_ids = [port.id for port in symbol.ports]
        assert len(port_ids) == len(set(port_ids)), symbol.key
        assert symbol.shapes, symbol.key
        for port in symbol.ports:
            assert 0 <= port.x <= symbol.width, (symbol.key, port.id, port.x)
            assert 0 <= port.y <= symbol.height, (symbol.key, port.id, port.y)
            assert port.name.strip(), (symbol.key, port.id)
            assert port.medium.strip(), (symbol.key, port.id)


def test_standard_library_covers_core_pid_categories_and_symbols(monkeypatch):
    monkeypatch.delenv("PID_AGENT_SYMBOL_PATHS", raising=False)
    monkeypatch.delenv("AGENTCAD_SYMBOL_PATHS", raising=False)
    registry = SymbolRegistry()
    symbols = registry.list()

    keys = {item.key for item in symbols}
    categories = {item.category for item in symbols}

    assert REQUIRED_STANDARD_KEYS <= keys
    assert REQUIRED_CATEGORIES <= categories
    assert len(symbols) >= 56
