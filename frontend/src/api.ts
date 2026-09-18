import type { AgentImagePayload } from "./agent/visionImageTypes";
import type { AutoLayoutOptions, AutoLayoutPreview } from "./layoutTypes";
import type {
  AgentPlan,
  AgentTransaction,
  AgentTransactionAssessment,
  Document,
  DocumentSummary,
  HistoryEntry,
  Operation,
  ImportResult,
  ProjectSettings,
  EngineeringGraph,
  EngineeringObjectMatch,
  EngineeringReport,
  EngineeringTraceResult,
  ProjectEngineeringGraph,
  ProjectIndexRebuildReport,
  ReportScope,
  SemanticAgentPlan,
  SemanticAgentPlanResult,
  SymbolDefinition,
  TransactionValidation,
} from "./types";

const API_ROOT = (import.meta as ImportMeta & { env?: { VITE_API_ROOT?: string } }).env?.VITE_API_ROOT ?? "/api/v2";
export const SERVICE_TOKEN_SESSION_KEY = "pid-agent-service-token";
export const EDITOR_SNAP_GRID_SIZE = 5;

let serviceAccessToken = "";
try {
  serviceAccessToken = window.sessionStorage.getItem(SERVICE_TOKEN_SESSION_KEY) ?? "";
} catch {
  serviceAccessToken = "";
}

export function getServiceAccessToken(): string {
  return serviceAccessToken;
}

export function setServiceAccessToken(token: string, persistForSession = true): void {
  serviceAccessToken = token.trim();
  try {
    if (persistForSession && serviceAccessToken) {
      window.sessionStorage.setItem(SERVICE_TOKEN_SESSION_KEY, serviceAccessToken);
    } else {
      window.sessionStorage.removeItem(SERVICE_TOKEN_SESSION_KEY);
    }
  } catch {
    // Memory-only token use remains available when sessionStorage is blocked.
  }
}

export function clearServiceAccessToken(): void {
  setServiceAccessToken("", false);
}

function withAuthorization(headers?: HeadersInit): Headers {
  const next = new Headers(headers);
  if (serviceAccessToken) next.set("Authorization", `Bearer ${serviceAccessToken}`);
  return next;
}

export function authorizedFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  return fetch(input, { ...init, headers: withAuthorization(init?.headers) });
}

export async function downloadApiResource(path: string, fallbackFilename: string): Promise<void> {
  const response = await authorizedFetch(path);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (payload && typeof payload.message === "string") detail = payload.message;
    } catch {
      // keep the status text when the body is not JSON
    }
    throw new Error(detail);
  }
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const match = disposition.match(/filename="?([^";]+)"?/i);
  const objectUrl = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = match?.[1] ?? fallbackFilename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

export type ProviderConfig = {
  base_url?: string;
  model?: string;
  api_key?: string;
  timeout_seconds?: number;
  thinking_enabled?: boolean;
  thinking_level?: "low" | "high" | "max";
};

export type ProviderTestResult = {
  ok: boolean;
  base_url: string;
  model: string;
  method: "models" | "chat_completion";
  latency_ms: number;
  model_available: boolean | null;
  available_model_count: number | null;
  message: string;
};

export type ProviderModelsResult = {
  ok: boolean;
  base_url: string;
  models: Array<{ id: string; owned_by: string | null }>;
  count: number;
  latency_ms: number;
};

export type AgentRuntimeConfig = {
  default_timeout_seconds: number | null;
  max_timeout_seconds: number | null;
};

export type AgentSession = {
  id: string;
  document_id: string;
  actor: string;
  project_id?: string | null;
  provider: string;
  model: string;
  start_revision: number;
  end_revision?: number | null;
  status: "active" | "completed" | "failed" | "cancelled";
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
};

export type ToolApproval = {
  id: string;
  session_id: string;
  tool_name: string;
  document_id: string;
  intent_hash: string;
  status: "pending" | "approved" | "rejected" | "consumed";
  requested_by: string;
  resolved_by?: string | null;
  reason: string;
  note: string;
  created_at: string;
  resolved_at?: string | null;
  consumed_at?: string | null;
};

export type DocumentStatus = { id: string; revision: number; updated_at: string };
export type AgentPlanResponse = { plan: AgentPlan; document?: Document | null };

export class ApiError extends Error {
  status: number;
  code?: string;
  retryable?: boolean;
  detail?: unknown;
  requestId?: string;

  constructor(
    message: string,
    options: {
      status: number;
      code?: string;
      retryable?: boolean;
      detail?: unknown;
      requestId?: string;
    },
  ) {
    super(options.requestId ? `${message}（request ${options.requestId}）` : message);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.retryable = options.retryable;
    this.detail = options.detail;
    this.requestId = options.requestId;
  }
}

function errorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const structured = detail as Record<string, unknown>;
    const message = typeof structured.message === "string" ? structured.message : fallback;
    if (structured.error === "provider_timeout") {
      const seconds = typeof structured.timeout_seconds === "number" ? structured.timeout_seconds : undefined;
      return seconds ? `模型在 ${seconds} 秒内未完成响应` : "模型未在规定时间内完成响应";
    }
    if (structured.error === "provider_connection_failed") return `无法连接模型服务：${message}`;
    if (structured.error === "provider_url_blocked") return `模型服务地址被网络安全策略阻止：${message}`;
    if (structured.error === "provider_response_too_large") return "模型服务响应超过服务器允许的大小。";
    if (structured.error === "authentication_required") return "此共享部署需要服务访问令牌。";
    if (structured.error === "invalid_access_token") return "服务访问令牌错误，请重新输入。";
    if (structured.error === "provider_authentication_failed") return "API Key 无效，或当前账号没有访问该模型的权限";
    if (structured.error === "provider_not_configured") return "尚未配置模型服务地址和模型名称";
    if (structured.error === "provider_vision_unsupported") return `所选模型或接口不支持图片输入：${message}`;
    if (structured.error === "invalid_agent_plan") return `模型返回的事务未通过校验：${message}`;
    return message;
  }
  return fallback;
}

function isDocumentPayload(value: unknown): value is Document {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<Document>;
  return typeof candidate.id === "string"
    && typeof candidate.name === "string"
    && typeof candidate.revision === "number"
    && Boolean(candidate.canvas && typeof candidate.canvas === "object")
    && Array.isArray(candidate.layers)
    && Array.isArray(candidate.systems)
    && Array.isArray(candidate.elements);
}

function withEditorSnapGrid(document: Document): Document {
  if (document.canvas.grid_size <= EDITOR_SNAP_GRID_SIZE) return document;
  return {
    ...document,
    canvas: {
      ...document.canvas,
      grid_size: EDITOR_SNAP_GRID_SIZE,
    },
  };
}

function normalizeEditorResponse<T>(payload: T): T {
  if (isDocumentPayload(payload)) return withEditorSnapGrid(payload) as T;
  if (Array.isArray(payload)) {
    return payload.map((item) => isDocumentPayload(item) ? withEditorSnapGrid(item) : item) as T;
  }
  if (!payload || typeof payload !== "object") return payload;

  const record = payload as Record<string, unknown>;
  let normalized: Record<string, unknown> | null = null;
  if (isDocumentPayload(record.document)) {
    normalized = { ...record, document: withEditorSnapGrid(record.document) };
  }
  if (Array.isArray(record.documents)) {
    const documents = record.documents.map((item) => isDocumentPayload(item) ? withEditorSnapGrid(item) : item);
    normalized = { ...(normalized ?? record), documents };
  }
  return (normalized ?? payload) as T;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await authorizedFetch(`${API_ROOT}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new ApiError("已手动停止操作", { status: 0, code: "aborted" });
    }
    throw error;
  }
  const requestId = response.headers.get("X-PID-Agent-Request-ID") || undefined;
  if (!response.ok) {
    const fallback = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      const detail = payload.detail;
      const structured = detail && typeof detail === "object" ? detail as Record<string, unknown> : undefined;
      throw new ApiError(errorMessage(detail, fallback), {
        status: response.status,
        code: typeof structured?.error === "string" ? structured.error : undefined,
        retryable: typeof structured?.retryable === "boolean" ? structured.retryable : undefined,
        detail,
        requestId,
      });
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(fallback, { status: response.status, requestId });
    }
  }
  if (response.status === 204) return undefined as T;
  return normalizeEditorResponse(await response.json() as T);
}

function providerPayload(provider?: ProviderConfig): ProviderConfig | undefined {
  if (!provider?.base_url && !provider?.model && !provider?.api_key && !provider?.timeout_seconds && provider?.thinking_enabled === undefined && !provider?.thinking_level) return undefined;
  return {
    base_url: provider.base_url || undefined,
    model: provider.model || undefined,
    api_key: provider.api_key || undefined,
    timeout_seconds: provider.timeout_seconds,
    thinking_enabled: provider.thinking_enabled,
    thinking_level: provider.thinking_level,
  };
}

export const api = {
  listDocuments: () => request<DocumentSummary[]>("/documents"),
  createAgentSession: (
    documentId: string,
    options?: { actor?: string; provider?: string; model?: string; metadata?: Record<string, unknown> },
  ) => request<AgentSession>("/agent/sessions", {
    method: "POST",
    body: JSON.stringify({
      document_id: documentId,
      actor: options?.actor ?? "web-user",
      provider: options?.provider ?? "",
      model: options?.model ?? "",
      metadata: options?.metadata ?? {},
    }),
  }),
  requestToolApproval: (
    sessionId: string,
    toolName: string,
    documentId: string,
    intent: Record<string, unknown>,
    reason = "",
  ) => request<ToolApproval>(`/agent/sessions/${encodeURIComponent(sessionId)}/approvals`, {
    method: "POST",
    body: JSON.stringify({
      tool_name: toolName,
      document_id: documentId,
      intent,
      requested_by: "web-user",
      reason,
    }),
  }),
  resolveToolApproval: (
    approvalId: string,
    approved: boolean,
    note = "",
  ) => request<ToolApproval>(`/agent/approvals/${encodeURIComponent(approvalId)}/resolve`, {
    method: "POST",
    body: JSON.stringify({ approved, actor: "web-user", note }),
  }),
  getAgentRuntimeConfig: () => request<AgentRuntimeConfig>("/agent/runtime-config"),
  createDocument: (name: string, options?: { folder_id?: string; metadata?: Record<string, unknown> }) =>
    request<Document>("/documents", {
      method: "POST",
      body: JSON.stringify({
        name,
        metadata: {
          ...(options?.metadata ?? {}),
          ...(options?.folder_id ? { folder_id: options.folder_id } : {}),
        },
      }),
    }),
  getDocument: (id: string) => request<Document>(`/documents/${id}`),
  getDocumentStatus: (id: string) => request<DocumentStatus>(`/documents/${id}/status`),
  moveDocumentFolder: (id: string, folder_id: string, expectedRevision: number) =>
    request<Document>(`/documents/${id}/folder`, {
      method: "PUT",
      body: JSON.stringify({ folder_id, expected_revision: expectedRevision }),
    }),
  renameDocument: (id: string, name: string, expectedRevision: number) =>
    request<Document>(`/documents/${id}/name`, {
      method: "PUT",
      body: JSON.stringify({ name, expected_revision: expectedRevision }),
    }),
  getEngineeringReport: (id: string, scope: ReportScope = "visible") =>
    request<EngineeringReport>(`/documents/${id}/engineering-report?scope=${encodeURIComponent(scope)}`),
  getEngineeringGraph: (id: string) =>
    request<EngineeringGraph>(`/documents/${id}/engineering-graph`),
  getEngineeringTrace: (
    id: string,
    ref: string,
    direction: "upstream" | "downstream" | "both" = "both",
  ) => request<EngineeringTraceResult>(
    `/documents/${encodeURIComponent(id)}/engineering-graph/trace`
    + `?ref=${encodeURIComponent(ref)}&direction=${direction}`,
  ),
  getProjectEngineeringGraph: () =>
    request<ProjectEngineeringGraph>("/project/engineering-graph"),
  findEngineeringObjects: (ref: string, limit = 50) =>
    request<EngineeringObjectMatch[]>(
      `/project/engineering-objects?ref=${encodeURIComponent(ref)}&limit=${limit}`,
    ),
  rebuildProjectIndex: (force = false) =>
    request<ProjectIndexRebuildReport>(`/project/index/rebuild?force=${force ? "true" : "false"}`, {
      method: "POST",
    }),
  engineeringReportCsvUrl: (
    id: string,
    kind: "equipment" | "lines" | "instruments" | "rules",
    scope: ReportScope = "visible",
  ) => `${API_ROOT}/documents/${encodeURIComponent(id)}/engineering-report/${kind}.csv?scope=${encodeURIComponent(scope)}`,
  getHistory: (id: string, limit = 100) => request<HistoryEntry[]>(`/documents/${id}/history?limit=${limit}`),
  deleteDocument: (id: string, expectedRevision: number) =>
    request<void>(
      `/documents/${id}?expected_revision=${encodeURIComponent(expectedRevision)}`,
      { method: "DELETE" },
    ),
  importDocument: (payload: unknown, conflictPolicy: "reject" | "regenerate" = "regenerate") =>
    request<ImportResult>(`/imports/document?conflict_policy=${conflictPolicy}`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  importProjectPackage: (payload: unknown, conflictPolicy: "reject" | "regenerate" = "regenerate") =>
    request<ImportResult>(`/imports/project-package?conflict_policy=${conflictPolicy}`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  getProjectSettings: () => request<ProjectSettings>("/project/settings"),
  updateProjectSettings: (settings: ProjectSettings) => request<ProjectSettings>("/project/settings", {
    method: "PUT",
    body: JSON.stringify(settings),
  }),
  transact: (id: string, revision: number, operations: Operation[], label: string) =>
    request<{ document: Document }>(`/documents/${id}/transactions`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: revision, operations, label }),
    }),
  previewAutoLayout: (id: string, options: AutoLayoutOptions) =>
    request<AutoLayoutPreview>(`/documents/${id}/layout/preview`, {
      method: "POST",
      body: JSON.stringify(options),
    }),
  validateTransaction: (id: string, transaction: AgentTransaction) =>
    request<TransactionValidation>(`/documents/${id}/transactions/validate`, {
      method: "POST",
      body: JSON.stringify(transaction),
    }),
  analyzeTransaction: (id: string, transaction: AgentTransaction) =>
    request<AgentTransactionAssessment>(`/documents/${id}/transactions/analyze`, {
      method: "POST",
      body: JSON.stringify(transaction),
    }),
  applyAgentPlan: (id: string, transaction: AgentTransaction) =>
    request<{ document: Document; applied_operations: number; label: string }>(`/documents/${id}/agent/apply`, {
      method: "POST",
      body: JSON.stringify(transaction),
    }),
  applySemanticAgentPlan: (
    id: string,
    sessionId: string,
    approvalId: string,
    planId: string,
    parentPlanId: string | null | undefined,
    attempt: number,
    transaction: AgentTransaction,
  ) => request<{ document: Document; applied_operations: number; label: string }>(`/documents/${id}/agent/apply-v2`, {
    method: "POST",
    body: JSON.stringify({
      session_id: sessionId,
      approval_id: approvalId,
      plan_id: planId,
      parent_plan_id: parentPlanId ?? null,
      attempt,
      transaction,
    }),
  }),
  undo: (id: string, revision?: number) => request<Document>(
    `/documents/${id}/undo${revision == null ? "" : `?expected_revision=${encodeURIComponent(revision)}`}`,
    { method: "POST" },
  ),
  redo: (id: string, revision?: number) => request<Document>(
    `/documents/${id}/redo${revision == null ? "" : `?expected_revision=${encodeURIComponent(revision)}`}`,
    { method: "POST" },
  ),
  listSymbols: () => request<SymbolDefinition[]>("/symbols"),
  listProviderModels: (provider: ProviderConfig, signal?: AbortSignal) =>
    request<ProviderModelsResult>("/agent/provider/models", { method: "POST", body: JSON.stringify(provider), signal }),
  testProvider: (provider: ProviderConfig, signal?: AbortSignal) =>
    request<ProviderTestResult>("/agent/provider/test", { method: "POST", body: JSON.stringify(provider), signal }),
  planSemanticAgent: (
    id: string,
    revision: number,
    prompt: string,
    context: string,
    provider?: ProviderConfig,
    images: AgentImagePayload[] = [],
    requireVisibleOutput = false,
    signal?: AbortSignal,
  ) => request<SemanticAgentPlanResult>(`/documents/${id}/agent/plan-v2`, {
    method: "POST",
    body: JSON.stringify({
      prompt,
      context,
      dry_run: true,
      expected_revision: revision,
      provider: providerPayload(provider),
      images,
      require_visible_output: requireVisibleOutput,
    }),
    signal,
  }),
  planSemanticAgentStream: async (
    id: string,
    revision: number,
    prompt: string,
    context: string,
    callbacks: {
      onThinking?: (delta: string) => void;
      onContent?: (delta: string) => void;
    },
    provider?: ProviderConfig,
    images: AgentImagePayload[] = [],
    requireVisibleOutput = false,
    signal?: AbortSignal,
  ): Promise<SemanticAgentPlanResult> => {
    const url = `${API_ROOT}/documents/${id}/agent/plan-v2-stream`;
    const response = await authorizedFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt,
        context,
        dry_run: true,
        expected_revision: revision,
        provider: providerPayload(provider),
        images,
        require_visible_output: requireVisibleOutput,
      }),
      signal,
    });

    if (!response.ok) {
      let detail = `请求失败: HTTP ${response.status}`;
      try {
        const json = await response.json();
        if (json.detail) detail = typeof json.detail === "string" ? json.detail : JSON.stringify(json.detail);
      } catch {
        // use fallback detail
      }
      throw new ApiError(detail, { status: response.status });
    }

    if (!response.body) {
      throw new ApiError("No response stream body", { status: response.status });
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalResult: SemanticAgentPlanResult | null = null;
    let errorMessage: string | null = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const normalized = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      const blocks = normalized.split("\n\n");
      buffer = blocks.pop() ?? "";

      for (const block of blocks) {
        if (!block.trim()) continue;
        const lines = block.split("\n");
        let eventType = "message";
        const dataLines: string[] = [];

        for (const line of lines) {
          if (line.startsWith("event:")) {
            eventType = line.slice(6).trim();
          } else if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).trim());
          }
        }

        if (!dataLines.length) continue;
        const dataStr = dataLines.join("\n");
        try {
          const dataJson = JSON.parse(dataStr);
          if (eventType === "thinking" && callbacks.onThinking) {
            callbacks.onThinking(dataJson.delta);
          } else if (eventType === "content" && callbacks.onContent) {
            callbacks.onContent(dataJson.delta);
          } else if (eventType === "complete") {
            finalResult = dataJson;
          } else if (eventType === "error") {
            errorMessage = dataJson.message;
          }
        } catch {
          // ignore partial parse error
        }
      }
    }

    if (errorMessage) {
      throw new ApiError(errorMessage, { status: 500 });
    }
    if (!finalResult) {
      throw new ApiError("流式规划未返回有效结果", { status: 500 });
    }
    return finalResult;
  },
  replanSemanticAgent: (
    id: string,
    revision: number,
    prompt: string,
    context: string,
    sessionId: string | undefined,
    failedPlan: SemanticAgentPlan,
    attempt: number,
    provider?: ProviderConfig,
    images: AgentImagePayload[] = [],
    requireVisibleOutput = false,
    signal?: AbortSignal,
  ) => request<SemanticAgentPlanResult>(`/documents/${id}/agent/replan`, {
    method: "POST",
    body: JSON.stringify({
      session_id: sessionId,
      prompt,
      context,
      expected_revision: revision,
      failed_plan: failedPlan,
      attempt,
      provider: providerPayload(provider),
      images,
      require_visible_output: requireVisibleOutput,
    }),
    signal,
  }),
  planAgent: (
    id: string,
    revision: number,
    prompt: string,
    context: string,
    provider?: ProviderConfig,
  ) => request<AgentPlanResponse>(`/documents/${id}/agent/generate`, {
    method: "POST",
    body: JSON.stringify({
      prompt,
      context,
      dry_run: true,
      expected_revision: revision,
      provider: providerPayload(provider),
    }),
  }),
  generate: (
    id: string,
    revision: number,
    prompt: string,
    context: string,
    provider?: ProviderConfig,
  ) => request<{ document: Document; plan: { explanation: string } }>(`/documents/${id}/agent/generate`, {
    method: "POST",
    body: JSON.stringify({
      prompt,
      context,
      expected_revision: revision,
      provider: providerPayload(provider),
    }),
  }),
};
