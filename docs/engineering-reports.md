# Engineering schedules and rule checks

P&ID-Agent derives engineering schedules and rule-check findings directly from the structured document. Report generation is deterministic, does not call a model provider, and never changes the document, revision, undo stack, or history.

## Report scopes

Every endpoint accepts `scope=visible` or `scope=all`.

- `visible` includes elements whose document layer and process system are both visible. Hidden connectors do not count as connections for visible-scope port checks.
- `all` includes every element while preserving its layer and system metadata in schedule rows.

Endpoint bindings are validated against the complete document in both scopes, so a selected connector cannot hide a stale reference merely because its target layer is hidden.

## JSON report

```http
GET /api/v2/documents/{document_id}/engineering-report?scope=visible
```

The versioned response uses schema `pid-agent.engineering-report` version `1` and contains:

- `equipment`: non-instrument symbol rows;
- `lines`: connector rows;
- `instruments`: symbols whose definition category is an instrument category;
- `findings`: stable rule findings;
- `counts`: row and severity counts;
- the source document ID, name, revision, and selected scope.

Rows are sorted by normalized tag and stable element ID. Findings are sorted by severity, rule code, affected IDs, and message. Runtime timestamps are intentionally absent.

## CSV downloads

```http
GET /api/v2/documents/{document_id}/engineering-report/equipment.csv?scope=visible
GET /api/v2/documents/{document_id}/engineering-report/lines.csv?scope=visible
GET /api/v2/documents/{document_id}/engineering-report/instruments.csv?scope=visible
GET /api/v2/documents/{document_id}/engineering-report/rules.csv?scope=visible
```

CSV output is UTF-8 with a BOM and CRLF line endings for spreadsheet interoperability. Nested properties and metadata use deterministic compact JSON. Response headers expose:

- `X-PID-Agent-Report-Revision`;
- `X-PID-Agent-Report-Scope`;
- `X-PID-Agent-Report-Row-Count`.

A failed request produces no partial download.

## Schedule fields

Equipment and instrument rows preserve the element ID, tag, name, symbol key/name/category, layer and system IDs/names, required and connected port counts, and structured properties.

Line rows preserve the element ID, line tag, medium, nominal diameter, routing, flow direction, layer and system, source/target endpoint descriptions, and metadata.

## Rule codes

Current deterministic checks include:

| Severity | Code | Meaning |
| --- | --- | --- |
| error | `TAG_DUPLICATE` | A symbol tag is reused in the selected scope. |
| warning | `TAG_MISSING` | A symbol has no tag. |
| warning | `SYMBOL_REQUIRED_PORT_UNCONNECTED` | A non-`none` symbol port has no selected-scope connector. |
| error | `SYMBOL_DEFINITION_MISSING` | A symbol key has no loaded definition. |
| warning | `LINE_TAG_MISSING` | A connector has no line tag. |
| warning | `LINE_MEDIUM_MISSING` | A connector has no medium. |
| warning | `LINE_DIAMETER_MISSING` | A connector has no nominal diameter. |
| error | `CONNECTOR_ENDPOINT_DANGLING` | A connector endpoint is free or unbound. |
| error | `CONNECTOR_ENDPOINT_ELEMENT_MISSING` | An endpoint references an absent element. |
| error | `CONNECTOR_ENDPOINT_PORT_MISSING` | The referenced symbol/junction port does not exist. |
| error | `CONNECTOR_ENDPOINT_INVALID_ELEMENT_TYPE` | The endpoint is bound to an unsupported element type. |
| error | `CONNECTOR_ENDPOINT_POINT_MISMATCH` | The endpoint or route coordinate no longer matches its bound port. |

Each finding includes severity, stable code, actionable message, affected element IDs, and structured details. The checker reports problems only; it never repairs the drawing silently.

## Browser workflow

Open **报表/检查** in the right sidebar. Users can:

1. choose visible or complete scope;
2. switch between equipment, line, instrument, and rule tabs;
3. filter rows by tags, medium, IDs, rule codes, or messages;
4. filter findings by severity;
5. locate affected elements on the canvas;
6. download the current tab as CSV.

Selecting **定位** selects every affected element that still exists. The normal property panel then opens for inspection or an explicit user edit.

## Python Client

```python
from pathlib import Path
from agentcad.client import AgentCADClient

with AgentCADClient("http://127.0.0.1:8000") as client:
    report = client.engineering_report("doc_123", scope="all")
    print(report.counts.errors, report.counts.warnings)
    client.export_engineering_report_csv(
        "doc_123",
        "rules",
        Path("rules.csv"),
        scope="all",
    )
```

Valid CSV kinds are `equipment`, `lines`, `instruments`, and `rules`.

## Verification

Backend tests cover deterministic ordering, visible/all scope, duplicate and missing tags, dangling and invalid endpoints, unconnected required ports, missing line metadata, UTF-8 CSV, Python Client behavior, and document immutability. Chromium acceptance covers the browser counts, filters, finding navigation, CSV download, hidden-layer scope switch, and unchanged revision.

## M4 canonical-validation boundary

`RuleFinding` remains the engineering-report domain model. It is an **adapter input**, not a second
public rule truth: the M4 `engineering-report` adapter preserves each legacy finding's code, severity
and element references under the built-in profile and then applies only the resolved canonical profile
metadata (enable / severity / waiver / rule source) in the common engine.

REST, MCP, CLI and the UI must not expose `RuleFinding` as an alternative validation contract — they
return the canonical `ValidationResult`. See
[`m4-engineering-validation.md`](m4-engineering-validation.md).
