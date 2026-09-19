import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  CAD_ACCEPT,
  cadFileExtension,
  cadIssueLine,
  cadReportLines,
  dwgConverterHint,
  isCadFileName,
} from "../src/cadImport.ts";
import type { CadCapabilities, CadImportReport } from "../src/cadImport.ts";


function report(overrides: Partial<CadImportReport> = {}): CadImportReport {
  return {
    source: {
      filename: "气路系统总图.dxf",
      format: "dxf",
      format_detail: "ASCII",
      size_bytes: 20480,
      sha256: "a".repeat(64),
      encoding: "utf-8",
      converter: "",
      converter_version: "",
      converter_command: [],
    },
    counts: {
      layers: 2,
      lines: 3,
      polylines: 1,
      circles: 2,
      texts: 4,
      fills: 0,
      elements: 10,
      primitives: 10,
      skipped: 0,
    },
    source_bounds: [0, 0, 100, 50],
    imported_bounds: [10, 10, 110, 60],
    frame: [0, 0, 100, 50],
    canvas: { width: 140, height: 90 },
    layers: ["管道", "仪表"],
    issues: [],
    operations: 10,
    revisions: 1,
    logical_mutations: 1,
    duration_ms: 12.5,
    warnings: [],
    ...overrides,
  };
}

describe("cad file names", () => {
  it("accepts exactly the two CAD extensions, case-insensitively", () => {
    assert.equal(isCadFileName("总图.DWG"), true);
    assert.equal(isCadFileName("总图.dxf"), true);
    assert.equal(cadFileExtension("PLAN.R2013.DXF"), "dxf");
    assert.equal(isCadFileName("document.json"), false);
    assert.equal(isCadFileName("dwg"), false);
    assert.equal(cadFileExtension(""), null);
  });

  it("advertises the accept attribute the file input uses", () => {
    assert.equal(CAD_ACCEPT, ".dwg,.dxf");
  });
});

describe("cadReportLines", () => {
  it("states the source, the decoder, the counts and the layers", () => {
    const text = cadReportLines(report()).join("\n");
    assert.match(text, /气路系统总图\.dxf/);
    assert.match(text, /DXF ASCII/);
    assert.match(text, /20\.0 KB/);
    assert.match(text, /内置 DXF 读取器/);
    assert.match(text, /图元 10/);
    assert.match(text, /管道、仪表/);
    assert.match(text, /140 × 90/);
  });

  it("names the external converter when the DWG path was used", () => {
    const text = cadReportLines(report({
      source: {
        filename: "总图.dwg",
        format: "dwg",
        format_detail: "AC1024",
        size_bytes: 917_504,
        sha256: "b".repeat(64),
        encoding: "",
        converter: "LibreDWG dwgread",
        converter_version: "0.13.3",
        converter_command: ["dwgread", "-O", "JSON"],
      },
    })).join("\n");
    assert.match(text, /解码器 LibreDWG dwgread · 0\.13\.3/);
  });

  it("says so explicitly when nothing was lost", () => {
    assert.match(cadReportLines(report()).join("\n"), /未能复现：无/);
  });

  it("reports every loss with its code and count", () => {
    const text = cadReportLines(report({
      issues: [
        { code: "unsupported_entity", message: "未表达的实体类型", count: 942 },
        { code: "curve_sampled", message: "曲线被采样为折线", count: 37 },
      ],
    })).join("\n");
    assert.match(text, /未能复现 2 类/);
    assert.match(text, /未表达的实体类型（unsupported_entity × 942）/);
    assert.match(text, /曲线被采样为折线（curve_sampled × 37）/);
  });

  it("keeps skipped elements and warnings visible", () => {
    const text = cadReportLines(report({
      counts: {
        layers: 2,
        lines: 3,
        polylines: 1,
        circles: 2,
        texts: 0,
        fills: 0,
        elements: 6,
        primitives: 6,
        skipped: 4,
      },
      warnings: ["文字基线未对齐"],
    })).join("\n");
    assert.match(text, /按导入选项跳过 4 个图元/);
    assert.match(text, /注意 文字基线未对齐/);
  });

  it("formats one issue line on its own", () => {
    assert.equal(cadIssueLine({ code: "x", message: "m", count: 3 }), "m（x × 3）");
  });
});

describe("dwgConverterHint", () => {
  const capabilities = (dwgImport: boolean): CadCapabilities => ({
    dxf_import: true,
    dwg_import: dwgImport,
    formats: dwgImport ? ["dxf", "dwg"] : ["dxf"],
    converters: [],
    decode_encoding: ["utf-8"],
    max_source_bytes: 1024,
    notes: [],
  });

  it("stays silent when DWG works or the probe never answered", () => {
    assert.equal(dwgConverterHint(capabilities(true)), "");
    assert.equal(dwgConverterHint(null), "");
  });

  it("says what to install when DWG cannot be decoded here", () => {
    assert.match(dwgConverterHint(capabilities(false)), /本机未安装 DWG 解码器/);
  });
});
