/**
 * CAD (DWG/DXF) import: client-side contract for `POST /api/v2/imports/cad`.
 *
 * The backend reproduces the drawing (geometry, layers, text, block provenance) and
 * reports everything it could not reproduce. This module owns the two things the UI
 * needs to be honest about that:
 *
 *  - which files are CAD files at all, so the editor refuses a .txt before uploading it,
 *  - how a report is turned into sentences, so "what did the import actually do" is
 *    answered in the panel instead of being hidden behind a green toast.
 */

export type CadSourceFormat = "dwg" | "dxf";
export type CadFillMode = "solid" | "skip";

export type CadIssue = {
  code: string;
  message: string;
  count: number;
  detail?: Record<string, unknown>;
};

export type CadElementCounts = {
  layers: number;
  lines: number;
  polylines: number;
  circles: number;
  texts: number;
  fills: number;
  elements: number;
  primitives: number;
  skipped: number;
};

export type CadSourceInfo = {
  filename: string;
  format: CadSourceFormat;
  format_detail: string;
  size_bytes: number;
  sha256: string;
  encoding: string;
  converter: string;
  converter_version: string;
  converter_command: string[];
};

export type CadImportReport = {
  source: CadSourceInfo;
  counts: CadElementCounts;
  source_bounds: [number, number, number, number] | null;
  imported_bounds: [number, number, number, number] | null;
  frame: [number, number, number, number] | null;
  canvas: Record<string, number>;
  layers: string[];
  issues: CadIssue[];
  operations: number;
  revisions: number;
  transactions: number;
  duration_ms: number;
  warnings: string[];
};

export type CadImportResult = {
  document_id: string;
  document_name: string;
  revision: number;
  report: CadImportReport;
};

export type CadDryRun = {
  report: CadImportReport;
  document_name: string;
  operations: number;
  elements: number;
};

export type CadConverterInfo = {
  key: string;
  label: string;
  executable: string;
  available: boolean;
  produces: string;
  version: string;
  evidence: string;
  notes: string;
};

export type CadCapabilities = {
  dxf_import: boolean;
  dwg_import: boolean;
  formats: CadSourceFormat[];
  converters: CadConverterInfo[];
  decode_encoding: string[];
  max_source_bytes: number;
  notes: string[];
};

export type CadImportOptions = {
  name?: string;
  frame?: [number, number, number, number] | null;
  layers?: string[];
  includeText?: boolean;
  fills?: CadFillMode;
  unitScale?: number;
  curveSegments?: number;
  preserveColors?: boolean;
  maxElements?: number;
  chunkSize?: number;
};

export const CAD_ACCEPT = ".dwg,.dxf";

const CAD_EXTENSIONS = [".dwg", ".dxf"];

export function cadFileExtension(name: string): CadSourceFormat | null {
  const lowered = (name ?? "").toLowerCase();
  for (const extension of CAD_EXTENSIONS) {
    if (lowered.endsWith(extension)) return extension.slice(1) as CadSourceFormat;
  }
  return null;
}

export function isCadFileName(name: string): boolean {
  return cadFileExtension(name) !== null;
}

function number(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

/** Human sentence for one reported loss. */
export function cadIssueLine(issue: CadIssue): string {
  return `${issue.message}（${issue.code} × ${issue.count}）`;
}

/** The report as a short list of sentences the panel can render directly. */
export function cadReportLines(report: CadImportReport): string[] {
  const counts = report.counts;
  const lines: string[] = [];
  const format = report.source.format.toUpperCase();
  const detail = report.source.format_detail ? ` ${report.source.format_detail}` : "";
  lines.push(
    `来源 ${report.source.filename} · ${format}${detail} · ${(report.source.size_bytes / 1024).toFixed(1)} KB`,
  );
  if (report.source.converter) {
    lines.push(
      `解码器 ${report.source.converter}${report.source.converter_version ? ` · ${report.source.converter_version}` : ""}`,
    );
  } else {
    lines.push("解码器 内置 DXF 读取器（无需外部工具）");
  }
  lines.push(
    `图元 ${counts.elements}：线 ${counts.lines} · 折线 ${counts.polylines} · 圆 ${counts.circles} · 文字 ${counts.texts} · 填充 ${counts.fills}`,
  );
  lines.push(`图层 ${counts.layers}：${report.layers.join("、")}`);
  if (report.frame) {
    const canvasWidth = report.canvas.width ?? 0;
    const canvasHeight = report.canvas.height ?? 0;
    lines.push(
      `图面区域 ${number(canvasWidth)} × ${number(canvasHeight)}（源坐标 ${number(report.frame[0])},${number(report.frame[1])} → ${number(report.frame[2])},${number(report.frame[3])}）`,
    );
  }
  if (counts.skipped > 0) {
    lines.push(`按导入选项跳过 ${counts.skipped} 个图元`);
  }
  if (report.issues.length === 0) {
    lines.push("未能复现：无");
  } else {
    lines.push(`未能复现 ${report.issues.length} 类：`);
    for (const issue of report.issues) lines.push(cadIssueLine(issue));
  }
  for (const warning of report.warnings) lines.push(`注意 ${warning}`);
  lines.push(
    `事务 ${report.transactions} · 操作 ${report.operations} · 耗时 ${Math.round(report.duration_ms)} ms`,
  );
  return lines;
}

/**
 * What to tell the user about DWG support on this installation.
 *
 * A DXF always works (the reader ships with the project). A DWG needs an installed
 * decoder, and saying so before the upload is the difference between a five-second
 * answer and a 900 KB round trip that fails.
 */
export function dwgConverterHint(capabilities: CadCapabilities | null): string {
  if (!capabilities || capabilities.dwg_import) return "";
  return "本机未安装 DWG 解码器：可导入 DXF，或安装 LibreDWG（dwgread/dwg2dfx）后重试";
}
