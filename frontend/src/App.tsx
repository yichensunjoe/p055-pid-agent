import { useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import { AutomaticAgentRunner } from "./agent/AutomaticAgentRunner";
import { AgentStreamingViewer } from "./agent/AgentStreamingViewer";
import { VisionImageInput } from "./agent/VisionImageInput";
import { shouldRequireVisibleOutput } from "./agent/visibleOutputIntent";
import { automaticAgentVerdict } from "./agent/automaticAgentLoop";
import { ProposalAccounting } from "./agent/ProposalAccounting";
import { toAgentImagePayload, type VisionAttachment } from "./agent/visionImageTypes";
import { EditorCanvas, type AgentCanvasPreview, type CanvasCommandId, type CanvasCommandRequest, type CanvasFocusRequest, type CanvasViewportRequest } from "./editor/EditorCanvas";
import { CommandPalette } from "./editor/CommandPalette";
import { CreateDocumentDialog } from "./editor/CreateDocumentDialog";
import { DocumentTree } from "./editor/DocumentTree";
import { CreateFolderDialog, RenameFolderDialog } from "./editor/FolderDialogs";
import { BasicShapesToolbar } from "./editor/BasicShapesToolbar";
import { ExperienceSettings } from "./editor/ExperienceSettings";
import { DraftingPanel } from "./editor/DraftingPanel";
import { EngineeringGraphPanel } from "./editor/EngineeringGraphPanel";
import { EngineeringReportPanel } from "./editor/EngineeringReportPanel";
import { ValidationPanel } from "./editor/ValidationPanel";
import { ViewNavigator } from "./editor/ViewNavigator";
import { elementPaletteCommands, type PaletteCommand } from "./editor/paletteCommands";
import { currentNavigationZone, deriveNavigationZones, loadNamedViews, persistNamedViews, sanitizeNamedViews, type CanvasView, type NamedCanvasView, type NavigationZone } from "./editor/navigationViews";
import { rectForElement } from "./editor/editorGeometry";
import { isElementEditLocked, readEditorGroupId } from "./editor/selectionEditing";
import { HistoryPanel } from "./editor/HistoryPanel";
import { LayerSystemPanel } from "./editor/LayerSystemPanel";
import { PropertyInspector } from "./editor/PropertyInspector";
import { SymbolPalette } from "./editor/SymbolPalette";
import { api, ApiError, clearServiceAccessToken, describeTypesafeKeySource, downloadApiResource, getServiceAccessToken, getTypesafeApiKey, readTypesafePreference, setServiceAccessToken, setTypesafeApiKey, writeTypesafePreferences, type ProviderConfig, type ProviderTestResult, type TextPlanResult, type TypesafeProviderStatus } from "./api";
import { CAD_ACCEPT, cadReportLines, dwgConverterHint, isCadFileName, type CadCapabilities } from "./cadImport";
import { documentDeletionConfirmation } from "./documentDeletion";
import {
  PROVIDER_PRESETS,
  presetForBaseUrl,
} from "./providerPresets";
import { parseImportJson } from "./projectImport";
import { commandForShortcut, resolvedShortcutMap, shortcutFromKeyboardEvent, useEditorPreferences, useResolvedAppearance } from "./editorPreferences";
import { installE2EBridge } from "./e2eBridge";
import { useWorkspace } from "./store";
import type { ProjectFolder, SemanticAgentPlanResult, SemanticOperation, Tool } from "./types";
import { CIRCLE_VARIETIES, LINE_VARIETIES, RECTANGLE_VARIETIES, SHAPE_DRAG_MIME } from "./editor/shapeVarieties";
import "./issue1.css";

const tools: Array<{ id: Tool; label: string; key: string }> = [
  { id: "select", label: "选择", key: "V" },
  { id: "line", label: "直线", key: "L" },
  { id: "connector", label: "工艺管线", key: "P" },
  { id: "junction", label: "连接节点", key: "J" },
  { id: "rectangle", label: "矩形", key: "R" },
  { id: "circle", label: "圆", key: "C" },
  { id: "text", label: "文字", key: "T" },
];

function ToolIcon({ id }: { id: Tool }) {
  const c = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  switch (id) {
    case "select": return (<svg {...c}><path d="M3.5 2.5 L3.5 12 L6.5 9.2 L8.3 13 L9.8 12.4 L8 8.8 L12 8.8 Z" fill="currentColor" stroke="none" /></svg>);
    case "line": return (<svg {...c}><path d="M3 13 L13 3" /></svg>);
    case "connector": return (<svg {...c}><path d="M4 3 L4 8 L12 8 L12 13 M10 11 L12 13 L14 11" /></svg>);
    case "junction": return (<svg {...c}><circle cx="8" cy="8" r="3" fill="currentColor" stroke="none" /></svg>);
    case "rectangle": return (<svg {...c}><rect x="3" y="4.5" width="10" height="7" /></svg>);
    case "circle": return (<svg {...c}><circle cx="8" cy="8" r="4.5" /></svg>);
    case "text": return (<svg {...c}><path d="M3 3.5 L13 3.5 M8 3.5 L8 12.5 M6 12.5 L10 12.5" /></svg>);
    default: return null;
  }
}

function VarietyPreview({ tool, variety }: { tool: "line" | "rectangle" | "circle"; variety: string }) {
  const dash = variety === "dashed" ? "3 2" : undefined;
  if (tool === "line") return <svg width={22} height={14} viewBox="0 0 22 14"><path d="M2 7 L20 7" fill="none" stroke="currentColor" strokeWidth={1.6} strokeDasharray={dash} /></svg>;
  if (tool === "rectangle") return <svg width={22} height={14} viewBox="0 0 22 14"><rect x={2} y={2} width={18} height={10} rx={variety === "rounded" ? 3 : 0} fill="none" stroke="currentColor" strokeWidth={1.6} strokeDasharray={dash} /></svg>;
  return <svg width={22} height={14} viewBox="0 0 22 14"><circle cx={11} cy={7} r={5.5} fill={variety === "filled" ? "currentColor" : "none"} stroke="currentColor" strokeWidth={1.6} strokeDasharray={dash} /></svg>;
}

function SimpleToolButton({ tool, label, shortcut, active, onSelect }: { tool: Tool; label: string; shortcut: string; active: boolean; onSelect: () => void }) {
  return (
    <button type="button" className={`tool-icon ${active ? "active" : ""}`} onClick={onSelect} title={`${label} (${shortcut})`}>
      <ToolIcon id={tool} />
    </button>
  );
}

function ShapeToolButton({ tool, label, shortcut, active }: { tool: "line" | "rectangle" | "circle"; label: string; shortcut: string; active: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const lineVariety = useWorkspace((s) => s.lineVariety);
  const rectangleVariety = useWorkspace((s) => s.rectangleVariety);
  const circleVariety = useWorkspace((s) => s.circleVariety);
  const setLineVariety = useWorkspace((s) => s.setLineVariety);
  const setRectangleVariety = useWorkspace((s) => s.setRectangleVariety);
  const setCircleVariety = useWorkspace((s) => s.setCircleVariety);
  const current = tool === "line" ? lineVariety : tool === "rectangle" ? rectangleVariety : circleVariety;
  const setVariety = tool === "line" ? setLineVariety : tool === "rectangle" ? setRectangleVariety : setCircleVariety;
  const options = tool === "line" ? LINE_VARIETIES : tool === "rectangle" ? RECTANGLE_VARIETIES : CIRCLE_VARIETIES;
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => { if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open]);
  return (
    <div className="tool-split" ref={ref}>
      <button
        type="button"
        className={`tool-icon ${active ? "active" : ""} ${open ? "menu-open" : ""}`}
        onClick={() => setOpen((value) => !value)}
        title={`${label} (${shortcut}) — 点开选种类，种类可拖到画布`}
      >
        <ToolIcon id={tool} />
        <span className="tool-caret" aria-hidden>▾</span>
      </button>
      {open ? (
        <div className="tool-menu" role="menu">
          <div className="tool-menu-title">{label}种类</div>
          {options.map((opt) => (
            <button
              key={opt.id}
              type="button"
              role="menuitem"
              className={`tool-menu-item ${current === opt.id ? "active" : ""}`}
              draggable
              onDragStart={(event) => { event.dataTransfer.setData(SHAPE_DRAG_MIME, `${tool}:${opt.id}`); event.dataTransfer.effectAllowed = "copy"; }}
              onClick={() => { setVariety(opt.id as never); setOpen(false); }}
              title={`${opt.label}（点击选用或拖到画布）`}
            >
              <VarietyPreview tool={tool} variety={opt.id} />
              <span>{opt.label}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

type RightPanel = "properties" | "groups" | "history" | "reports" | "graph" | "drafting" | "agent";

function operationDescription(operation: SemanticOperation): string {
  switch (operation.op) {
    case "add_element": return `新增 ${operation.element.type}${operation.element.id ? ` · ${operation.element.id}` : ""}`;
    case "update_element": return `修改 ${operation.element_id} · ${Object.keys(operation.patch).join(", ") || "空 patch"}`;
    case "delete_element": return `删除 ${operation.element_id} · ${operation.connection_policy ?? "reject_if_connected"}`;
    case "replace_symbol": return `替换设备 ${operation.element_id} → ${operation.symbol_key}`;
    case "reconnect_connector": return `重连 ${operation.connector_id}.${operation.endpoint} → ${operation.element_id ? `${operation.element_id}.${operation.port_id}` : "自由端点"}`;
    case "connect_ports": return `连接 ${operation.source_element_id}.${operation.source_port_id} → ${operation.target_element_id}.${operation.target_port_id}${operation.waypoints?.length ? ` · ${operation.waypoints.length} 个折点` : ""}`;
    case "instrument_tap": return `仪表测点 ${operation.instrument_label} · ${operation.main_connector_id} @ (${operation.junction_point.x}, ${operation.junction_point.y})`;
    case "add_layer": return `新增图层 ${operation.layer.name}`;
    case "update_layer": return `修改图层 ${operation.layer_id}`;
    case "delete_layer": return `删除图层 ${operation.layer_id}`;
    case "add_system": return `新增系统 ${operation.system.name}`;
    case "update_system": return `修改系统 ${operation.system_id}`;
    case "delete_system": return `删除系统 ${operation.system_id}`;
    case "clear_document": return "清空文档";
  }
}

export default function App() {
  const state = useWorkspace();
  const preferences = useEditorPreferences();
  const resolvedAppearance = useResolvedAppearance();
  const shortcutMap = useMemo(() => resolvedShortcutMap(preferences.shortcutOverrides), [preferences.shortcutOverrides]);
  const [prompt, setPrompt] = useState("");
  const [context, setContext] = useState("");
  const [referenceImages, setReferenceImages] = useState<VisionAttachment[]>([]);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [showApiKey, setShowApiKey] = useState(false);
  const [typesafeKey, setTypesafeKey] = useState(() => getTypesafeApiKey());
  const [showTypesafeKey, setShowTypesafeKey] = useState(false);
  const [typesafeBaseUrl, setTypesafeBaseUrl] = useState(() => readTypesafePreference("baseUrl", "https://api.typesafe.ai"));
  const [typesafeModel, setTypesafeModel] = useState(() => readTypesafePreference("model", "jev-latest"));
  const [typesafeEnabled, setTypesafeEnabled] = useState(() => readTypesafePreference("enabled", "false") === "true");
  const [typesafeTest, setTypesafeTest] = useState<{ ok: boolean; message: string } | null>(null);
  const [typesafeStatus, setTypesafeStatus] = useState<TypesafeProviderStatus | null>(null);
  const [nlSentence, setNlSentence] = useState("");
  const [drawingTextPlan, setDrawingTextPlan] = useState(false);
  const [nlResult, setNlResult] = useState<TextPlanResult | null>(null);
  const [nlError, setNlError] = useState("");
  const [nlSource, setNlSource] = useState<{ revision: number; specDigest: string } | null>(null);
  const [testingTypesafe, setTestingTypesafe] = useState(false);
  const [serviceToken, setServiceToken] = useState(() => getServiceAccessToken());
  const [showServiceToken, setShowServiceToken] = useState(false);
  const [thinkingEnabled, setThinkingEnabled] = useState(true);
  const [thinkingLevel, setThinkingLevel] = useState<ProviderConfig["thinking_level"]>("high");
  const [testingProvider, setTestingProvider] = useState(false);
  const [providerTest, setProviderTest] = useState<ProviderTestResult | null>(null);
  const [providerTestError, setProviderTestError] = useState("");
  const [providerPreset, setProviderPreset] = useState("custom");
  const [availableModels, setAvailableModels] = useState<Array<{ id: string; owned_by: string | null }>>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [modelDiscoveryError, setModelDiscoveryError] = useState("");
  const [canvasPointerActive, setCanvasPointerActive] = useState(false);
  const [rightPanel, setRightPanel] = useState<RightPanel>("properties");
  const [planningAgent, setPlanningAgent] = useState(false);
  const [repairingAgent, setRepairingAgent] = useState(false);
  const [applyingAgent, setApplyingAgent] = useState(false);
  const [automaticAgentRunning, setAutomaticAgentRunning] = useState(false);
  const [agentError, setAgentError] = useState("");
  const [pendingPlan, setPendingPlan] = useState<SemanticAgentPlanResult | null>(null);
  const [streamingThinking, setStreamingThinking] = useState("");
  const [streamingContent, setStreamingContent] = useState("");
  const [canvasFocusRequest, setCanvasFocusRequest] = useState<CanvasFocusRequest | null>(null);
  const [canvasCommandRequest, setCanvasCommandRequest] = useState<CanvasCommandRequest | null>(null);
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);
  const [createDocumentOpen, setCreateDocumentOpen] = useState(false);
  const [createFolderOpen, setCreateFolderOpen] = useState(false);
  const [renameFolderTarget, setRenameFolderTarget] = useState<ProjectFolder | null>(null);
  const [createDocumentFolderId, setCreateDocumentFolderId] = useState<string | undefined>(undefined);
  const [experienceSettingsOpen, setExperienceSettingsOpen] = useState(false);
  const [viewNavigatorOpen, setViewNavigatorOpen] = useState(false);
  const [canvasView, setCanvasView] = useState<CanvasView | null>(null);
  const [canvasViewportRequest, setCanvasViewportRequest] = useState<CanvasViewportRequest | null>(null);
  const [leftPanelTab, setLeftPanelTab] = useState<"all" | "documents" | "symbols">("all");
  const [namedViews, setNamedViews] = useState<NamedCanvasView[]>([]);
  const [importError, setImportError] = useState("");
  const [cadReport, setCadReport] = useState<{ title: string; documentId: string | null; lines: string[] } | null>(null);
  const [cadCapabilities, setCadCapabilities] = useState<CadCapabilities | null>(null);
  const documentImportRef = useRef<HTMLInputElement>(null);
  const projectImportRef = useRef<HTMLInputElement>(null);
  const cadImportRef = useRef<HTMLInputElement>(null);
  const cadPlanRef = useRef<HTMLInputElement>(null);
  const planningAbortControllerRef = useRef<AbortController | null>(null);
  const testProviderAbortControllerRef = useRef<AbortController | null>(null);
  const discoverModelsAbortControllerRef = useRef<AbortController | null>(null);

  const projectFolders = useMemo(() => {
    return ((state.projectSettings.metadata?.folders as ProjectFolder[] | undefined) ?? []);
  }, [state.projectSettings.metadata]);

  const stopAgentPlanning = () => {
    if (planningAbortControllerRef.current) {
      planningAbortControllerRef.current.abort();
      planningAbortControllerRef.current = null;
    }
    setPlanningAgent(false);
    setRepairingAgent(false);
    setAgentError("已手动停止生成。");
  };

  const stopProviderTest = () => {
    if (testProviderAbortControllerRef.current) {
      testProviderAbortControllerRef.current.abort();
      testProviderAbortControllerRef.current = null;
    }
    setTestingProvider(false);
    setProviderTestError("已停止测试连接。");
  };

  const stopProviderDiscovery = () => {
    if (discoverModelsAbortControllerRef.current) {
      discoverModelsAbortControllerRef.current.abort();
      discoverModelsAbortControllerRef.current = null;
    }
    setLoadingModels(false);
    setModelDiscoveryError("已停止模型发现。");
  };

  useEffect(() => { void state.loadWorkspace(); }, []);
  // Ask once what this installation can decode. A failure here is not an error the user
  // needs: the import still works for DXF, and the backend answers per-request anyway.
  useEffect(() => {
    let cancelled = false;
    void api.cadImportCapabilities()
      .then((capabilities) => { if (!cancelled) setCadCapabilities(capabilities); })
      .catch(() => { if (!cancelled) setCadCapabilities(null); });
    return () => { cancelled = true; };
  }, []);
  // Whether the server already holds a TypeSafe key decides what an empty field means, and the
  // answer is not knowable from the browser: the key may live only in the server's environment.
  useEffect(() => {
    let cancelled = false;
    void api.typesafeStatus()
      .then((status) => { if (!cancelled) setTypesafeStatus(status); })
      .catch(() => { if (!cancelled) setTypesafeStatus(null); });
    return () => { cancelled = true; };
  }, []);
  useEffect(() => {
    if (import.meta.env.MODE !== "e2e") return;
    return installE2EBridge(() => pendingPlan, setPendingPlan);
  }, [pendingPlan]);
  // Keyed by the selection *contents*, not by the array object: any mutation re-creates
  // the selection array (`filter` over the surviving ids), and a reference-keyed effect
  // would then treat an ordinary edit as a fresh canvas selection and eject the user from
  // whichever analysis panel they were working in.
  const selectionKey = state.selectedElementIds.join("\u0000");
  const selectionRevealsProperties = state.selectionRevealsProperties;
  const hasSelection = state.selectedElementIds.length > 0;
  useEffect(() => {
    // Canvas selections reveal 属性; panel-driven highlights (定位/追踪) must not eject the user
    // from the analysis panel they were reading.
    if (!hasSelection || !selectionRevealsProperties) return;
    setRightPanel("properties");
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed by selection contents on purpose
  }, [selectionKey, selectionRevealsProperties, hasSelection]);
  useEffect(() => {
    const document = state.document;
    if (!pendingPlan) return;
    if (!document) {
      setPendingPlan(null);
      setAgentError("当前文档已删除，旧 Agent 预览已自动清除。");
      return;
    }
    const expectedRevision = pendingPlan.compiled_plan?.transaction.expected_revision
      ?? pendingPlan.plan.transaction.expected_revision;
    const wrongDocument = pendingPlan.assessment.document_id !== document.id;
    const staleRevision = expectedRevision !== null && expectedRevision !== undefined && expectedRevision !== document.revision;
    if (!wrongDocument && !staleRevision) return;
    setPendingPlan(null);
    setAgentError("文档或 revision 已变化，旧 Agent 预览已自动清除。");
  }, [state.document?.id, state.document?.revision, pendingPlan?.plan.plan_id]);
  useEffect(() => {
    if (!state.document) return;
    const check = () => void state.checkForExternalUpdates(!canvasPointerActive);
    check();
    const timer = window.setInterval(check, 1500);
    return () => window.clearInterval(timer);
  }, [state.document?.id, canvasPointerActive]);
  useEffect(() => {
    const releasePointer = () => setCanvasPointerActive(false);
    window.addEventListener("pointerup", releasePointer);
    window.addEventListener("pointercancel", releasePointer);
    return () => {
      window.removeEventListener("pointerup", releasePointer);
      window.removeEventListener("pointercancel", releasePointer);
    };
  }, []);
  useEffect(() => {
    const document = state.document;
    setNamedViews(document ? loadNamedViews(document.id) : []);
    setCanvasView(document ? { x: 0, y: 0, width: document.canvas.width, height: document.canvas.height } : null);
    setCanvasViewportRequest(null);
    setViewNavigatorOpen(false);
    setReferenceImages([]);
  }, [state.document?.id]);

  const applyServiceToken = () => {
    setServiceAccessToken(serviceToken, true);
    state.clearError();
    void state.loadWorkspace();
  };

  const clearServiceToken = () => {
    clearServiceAccessToken();
    setServiceToken("");
    state.clearError();
    void state.loadWorkspace();
  };

  const providerConfig = (): ProviderConfig => ({
    base_url: baseUrl.trim() || undefined,
    model: model.trim() || undefined,
    api_key: apiKey.trim() || undefined,
    thinking_enabled: thinkingEnabled,
    thinking_level: thinkingEnabled ? thinkingLevel : undefined,
  });

  const selectProviderPreset = (presetId: string) => {
    setProviderPreset(presetId);
    const preset = PROVIDER_PRESETS.find((item) => item.id === presetId);
    if (preset && preset.id !== "custom") {
      setBaseUrl(preset.baseUrl);
    }
    setModel("");
    setApiKey("");
    setAvailableModels([]);
    setModelDiscoveryError("");
    setProviderTest(null);
    setProviderTestError("");
  };

  const changeProviderBaseUrl = (value: string) => {
    const presetId = presetForBaseUrl(value);
    setBaseUrl(value);
    setProviderPreset(presetId);
    setModel("");
    setApiKey("");
    setAvailableModels([]);
    setModelDiscoveryError("");
    setProviderTest(null);
    setProviderTestError("");
  };

  const discoverProviderModels = async (silent = false) => {
    if (!baseUrl.trim()) return;
    if (discoverModelsAbortControllerRef.current) discoverModelsAbortControllerRef.current.abort();
    const controller = new AbortController();
    discoverModelsAbortControllerRef.current = controller;
    setLoadingModels(true);
    setModelDiscoveryError("");
    try {
      const result = await api.listProviderModels({
        base_url: baseUrl.trim(),
        api_key: apiKey.trim() || undefined,
      }, controller.signal);
      setAvailableModels(result.models);
      if (result.models.length) {
        setModel((current) => result.models.some((item) => item.id === current) ? current : result.models[0].id);
      } else if (!silent) {
        setModelDiscoveryError("服务连接成功，但 /models 没有返回可用模型。仍可手工输入模型名称。");
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        setAvailableModels([]);
        setModelDiscoveryError(error instanceof ApiError ? error.message : String(error));
      }
    } finally {
      if (discoverModelsAbortControllerRef.current === controller) {
        discoverModelsAbortControllerRef.current = null;
      }
      setLoadingModels(false);
    }
  };

  useEffect(() => {
    const preset = PROVIDER_PRESETS.find((item) => item.id === providerPreset);
    if (!baseUrl.trim() || (preset?.requiresApiKey && !apiKey.trim())) {
      setAvailableModels([]);
      return;
    }
    const timer = window.setTimeout(() => { void discoverProviderModels(true); }, 450);
    return () => window.clearTimeout(timer);
  }, [baseUrl, apiKey, providerPreset]);

  const scopedContext = () => {
    const document = state.document;
    if (!document || state.selectedElementIds.length === 0) return context.trim();
    const selected = document.elements.filter((element) => state.selectedElementIds.includes(element.id));
    const selectedIds = new Set(selected.map((element) => element.id));
    const connected = document.elements.filter((element) => element.type === "connector" && (
      (element.source?.element_id && selectedIds.has(element.source.element_id))
      || (element.target?.element_id && selectedIds.has(element.target.element_id))
    ));
    return [
      context.trim(),
      "",
      "Local modification scope:",
      `The user selected ${selected.length} element(s). Prefer modifying these elements and their directly connected pipes. Preserve unrelated elements unless the instruction explicitly requires a wider change.`,
      JSON.stringify({ selected, directly_connected_connectors: connected }, null, 2),
    ].filter(Boolean).join("\n");
  };

  const refreshTypesafeStatus = async () => {
    const status = await api.typesafeStatus().catch(() => null);
    setTypesafeStatus(status);
    return status;
  };

  const verifyTypesafe = async () => {
    setTestingTypesafe(true);
    setTypesafeTest(null);
    try {
      const result = await api.verifyTypesafe({
        base_url: typesafeBaseUrl.trim() || undefined,
        model: typesafeModel.trim() || undefined,
        api_key: typesafeKey.trim() || undefined,
      });
      setTypesafeTest({ ok: true, message: `Key 有效 · ${result.model} · ${Math.round(result.latency_ms)} ms` });
    } catch (error) {
      setTypesafeTest({ ok: false, message: error instanceof ApiError ? error.message : String(error) });
    } finally {
      setTestingTypesafe(false);
      // A failure here is often "no key anywhere", which the hint above the field should say.
      void refreshTypesafeStatus();
    }
  };

  const drawFromSentence = async (dryRun: boolean) => {
    const document = state.document;
    if (!document || !nlSentence.trim() || drawingTextPlan) return;
    setDrawingTextPlan(true);
    setNlError("");
    try {
      const result = await api.planTextDrawing(
        document.id,
        nlSentence.trim(),
        {
          base_url: typesafeBaseUrl.trim() || undefined,
          model: typesafeModel.trim() || undefined,
          api_key: typesafeKey.trim() || undefined,
        },
        dryRun,
      );
      setNlResult(result);
      if (result.committed && typeof result.revision === "number") {
        const active = useWorkspace.getState().document;
        if (!active || active.id !== document.id) {
          setNlError("图纸已切换，出图结果未写回当前画布。");
          return;
        }
        const fresh = await api.getDocument(document.id);
        const documents = await api.listDocuments();
        useWorkspace.setState({
          document: fresh,
          documents,
          selectedElementIds: [],
          error: null,
          syncState: "synced",
          syncMessage: `已同步至 r${fresh.revision}`,
          pendingExternalRevision: null,
        });
      }
      if (result.committed && result.spec_digest) {
        setNlSource({ revision: result.revision ?? document.revision, specDigest: result.spec_digest });
      }
    } catch (error) {
      const receipt = error instanceof ApiError ? error.detail : undefined;
      if (
        error instanceof ApiError &&
        receipt &&
        typeof receipt === "object" &&
        ((receipt as { code?: string }).code === "typesafe_spec_partial" ||
          (receipt as { code?: string }).code === "typesafe_spec_no_device")
      ) {
        const detail = receipt as {
          code?: string;
          undelivered?: string[];
          skipped?: string[];
        };
        const lines = [
          detail.code === "typesafe_spec_partial"
            ? "句子只兑现了一部分，图纸未写入。未兑现："
            : "没有可画的设备，图纸未写入。",
          ...(detail.undelivered ?? []).map((item) => `· ${item}`),
          ...(detail.skipped ?? []).map((item) => `· ${item}`),
        ];
        setNlError(lines.join("\n"));
      } else {
        setNlError(error instanceof ApiError ? error.message : String(error));
      }
    } finally {
      setDrawingTextPlan(false);
    }
  };

  const editFromSentence = async (dryRun: boolean) => {
    const document = state.document;
    const source = nlSource;
    if (!document || !nlSentence.trim() || drawingTextPlan || !source) return;
    setDrawingTextPlan(true);
    setNlError("");
    try {
      const result = await api.editTextDrawing(
        document.id,
        nlSentence.trim(),
        source.revision,
        source.specDigest,
        {
          base_url: typesafeBaseUrl.trim() || undefined,
          model: typesafeModel.trim() || undefined,
          api_key: typesafeKey.trim() || undefined,
        },
        dryRun,
      );
      setNlResult(result);
      if (result.committed && typeof result.revision === "number") {
        const active = useWorkspace.getState().document;
        if (!active || active.id !== document.id) {
          setNlError("图纸已切换，改图结果未写回当前画布。");
          return;
        }
        const fresh = await api.getDocument(document.id);
        const documents = await api.listDocuments();
        useWorkspace.setState({
          document: fresh,
          documents,
          selectedElementIds: [],
          error: null,
          syncState: "synced",
          syncMessage: `已同步至 r${fresh.revision}`,
          pendingExternalRevision: null,
        });
      }
      if (result.committed && result.spec_digest) {
        setNlSource({ revision: result.revision ?? document.revision, specDigest: result.spec_digest });
      }
    } catch (error) {
      const receipt = error instanceof ApiError ? error.detail : undefined;
      if (
        error instanceof ApiError &&
        receipt &&
        typeof receipt === "object" &&
        ((receipt as { code?: string }).code === "typesafe_spec_partial" ||
          (receipt as { code?: string }).code === "typesafe_spec_no_device")
      ) {
        const detail = receipt as {
          code?: string;
          undelivered?: string[];
          skipped?: string[];
        };
        const lines = [
          detail.code === "typesafe_spec_partial"
            ? "句子只兑现了一部分，图纸未写入。未兑现："
            : "没有可画的变更，图纸未写入。",
          ...(detail.undelivered ?? []).map((item) => `· ${item}`),
          ...(detail.skipped ?? []).map((item) => `· ${item}`),
        ];
        setNlError(lines.join("\n"));
      } else {
        setNlError(error instanceof ApiError ? error.message : String(error));
      }
    } finally {
      setDrawingTextPlan(false);
    }
  };

  const planAgent = async () => {
    if (!prompt.trim() || !state.document) return;
    if (planningAbortControllerRef.current) planningAbortControllerRef.current.abort();
    const controller = new AbortController();
    planningAbortControllerRef.current = controller;
    setPlanningAgent(true);
    setAgentError("");
    setPendingPlan(null);
    setStreamingThinking("");
    setStreamingContent("");
    try {
      if (typesafeEnabled) {
        // TypeSafe plans by judgment instead of by streaming text: code builds the candidates, the
        // model answers narrow choices, and anything below the confidence floor is skipped rather
        // than drawn. Same result envelope, so the preview and apply path below is unchanged.
        writeTypesafePreferences({ baseUrl: typesafeBaseUrl, model: typesafeModel, enabled: typesafeEnabled });
        setTypesafeApiKey(typesafeKey);
        const response = await api.planSemanticAgentWithTypesafe(
          state.document.id,
          state.document.revision,
          prompt.trim(),
          scopedContext(),
          {
            base_url: typesafeBaseUrl.trim() || undefined,
            model: typesafeModel.trim() || undefined,
            api_key: typesafeKey.trim() || undefined,
          },
          undefined,
          shouldRequireVisibleOutput(prompt, state.document.elements.length),
          controller.signal,
        );
        setPendingPlan(response);
        if (response.plan.explanation.startsWith("TypeSafe")) {
          setStreamingContent(response.plan.explanation);
        }
        return;
      }
      const response = await api.planSemanticAgentStream(
        state.document.id,
        state.document.revision,
        prompt.trim(),
        scopedContext(),
        {
          onThinking: (delta) => setStreamingThinking((prev) => prev + delta),
          onContent: (delta) => setStreamingContent((prev) => prev + delta),
        },
        providerConfig(),
        toAgentImagePayload(referenceImages),
        shouldRequireVisibleOutput(prompt, state.document.elements.length),
        controller.signal,
      );
      setPendingPlan(response);
    } catch (error) {
      if (!controller.signal.aborted) {
        setAgentError(error instanceof ApiError ? error.message : String(error));
      }
    } finally {
      if (planningAbortControllerRef.current === controller) {
        planningAbortControllerRef.current = null;
      }
      setPlanningAgent(false);
    }
  };

  const replanAgent = async () => {
    const document = state.document;
    if (!document || !pendingPlan || !prompt.trim()) return;
    if (planningAbortControllerRef.current) planningAbortControllerRef.current.abort();
    const controller = new AbortController();
    planningAbortControllerRef.current = controller;
    const attempt = Math.min(5, pendingPlan.attempt + 1);
    setRepairingAgent(true);
    setAgentError("");
    try {
      const response = await api.replanSemanticAgent(
        document.id,
        document.revision,
        prompt.trim(),
        scopedContext(),
        pendingPlan.session_id,
        pendingPlan.plan,
        attempt,
        providerConfig(),
        toAgentImagePayload(referenceImages),
        shouldRequireVisibleOutput(prompt, document.elements.length),
        controller.signal,
      );
      setPendingPlan(response);
    } catch (error) {
      if (!controller.signal.aborted) {
        setAgentError(error instanceof ApiError ? error.message : String(error));
      }
    } finally {
      if (planningAbortControllerRef.current === controller) {
        planningAbortControllerRef.current = null;
      }
      setRepairingAgent(false);
    }
  };

  const applyAgentPlan = async () => {
    const document = state.document;
    const compiled = pendingPlan?.compiled_plan;
    if (!document || !pendingPlan || !compiled || !automaticAgentVerdict(pendingPlan).mayProceed) {
      return;
    }
    const expectedRevision = compiled.transaction.expected_revision;
    if (expectedRevision !== null && expectedRevision !== undefined && expectedRevision !== document.revision) {
      setAgentError(`预览基于 r${expectedRevision}，当前网页已是 r${document.revision}。请按当前 revision 局部重规划。`);
      return;
    }
    setApplyingAgent(true);
    setAgentError("");
    try {
      const sessionId = pendingPlan.session_id
        ?? (await api.createAgentSession(document.id, {
          actor: "web-user",
          provider: baseUrl.trim(),
          model: model.trim(),
          metadata: { surface: "web", workflow: "manual-agent-apply" },
        })).id;
      const requestedApproval = await api.requestToolApproval(
        sessionId,
        "apply_compiled_agent_transaction",
        document.id,
        { transaction: compiled.transaction },
        "用户已查看 Agent 预览并点击应用。",
      );
      const approval = await api.resolveToolApproval(
        requestedApproval.id,
        true,
        "用户显式点击“应用”确认本次工程变更。",
      );
      const result = await api.applySemanticAgentPlan(
        document.id,
        sessionId,
        approval.id,
        pendingPlan.plan.plan_id,
        pendingPlan.parent_plan_id,
        pendingPlan.attempt,
        compiled.transaction,
      );
      const activeDocument = useWorkspace.getState().document;
      if (!activeDocument || activeDocument.id !== document.id || activeDocument.revision !== document.revision) {
        setAgentError("图纸已切换，旧 Agent 应用响应未写回当前画布。");
        return;
      }
      const documents = await api.listDocuments();
      const existing = new Set(result.document.elements.map((element) => element.id));
      useWorkspace.setState({
        document: result.document,
        documents,
        selectedElementIds: pendingPlan.assessment.affected_element_ids.filter((id) => existing.has(id)),
        error: null,
        syncState: "synced",
        syncMessage: `已同步至 r${result.document.revision}`,
        pendingExternalRevision: null,
      });
      setPendingPlan(null);
      setPrompt("");
      setReferenceImages([]);
    } catch (error) {
      setAgentError(error instanceof ApiError ? error.message : String(error));
    } finally {
      setApplyingAgent(false);
    }
  };

  const discardAgentPlan = () => {
    setPendingPlan(null);
    setAgentError("");
  };

  const focusCanvasElement = (elementId: string) => {
    const document = state.document;
    if (!document?.elements.some((element) => element.id === elementId)) return;
    state.setSelection([elementId]);
    setCanvasFocusRequest((current) => ({ ids: [elementId], nonce: (current?.nonce ?? 0) + 1 }));
  };

  const dispatchCanvasCommand = (id: CanvasCommandId) => {
    setCanvasCommandRequest((current) => ({ id, nonce: (current?.nonce ?? 0) + 1 }));
  };

  const testCustomProvider = async () => {
    if (!baseUrl.trim() || !model.trim()) return;
    if (testProviderAbortControllerRef.current) testProviderAbortControllerRef.current.abort();
    const controller = new AbortController();
    testProviderAbortControllerRef.current = controller;
    setTestingProvider(true);
    setProviderTest(null);
    setProviderTestError("");
    try {
      const result = await api.testProvider(providerConfig(), controller.signal);
      setProviderTest(result);
    } catch (error) {
      if (!controller.signal.aborted) {
        setProviderTestError(error instanceof ApiError ? error.message : String(error));
      }
    } finally {
      if (testProviderAbortControllerRef.current === controller) {
        testProviderAbortControllerRef.current = null;
      }
      setTestingProvider(false);
    }
  };

  const syncActionable = state.syncState === "pending" || state.syncState === "error";
  const tabs: Array<{ id: RightPanel; label: string }> = [
    { id: "properties", label: "属性" },
    { id: "groups", label: "图层/系统" },
    { id: "history", label: "历史" },
    { id: "reports", label: "报表/检查" },
    { id: "graph", label: "工程图谱" },
    { id: "drafting", label: "整理" },
    { id: "agent", label: "Agent" },
  ];
  const busyAgent = planningAgent || repairingAgent || applyingAgent || automaticAgentRunning;
  const agentImages = useMemo(() => toAgentImagePayload(referenceImages), [referenceImages]);
  const requireVisibleOutput = shouldRequireVisibleOutput(prompt, state.document?.elements.length ?? 0);
  // A plan may be previewed as a proposal on one axis and still not be offerable: the ghost
  // preview and the apply button both read the two-axis verdict, not `valid` alone.
  const pendingPlanVerdict = pendingPlan ? automaticAgentVerdict(pendingPlan) : null;
  const pendingPlanMayApply = Boolean(pendingPlan && pendingPlanVerdict?.mayProceed);
  // A response that does not carry the accounting contract is neither approvable nor a
  // repairable proposal: the fix is a fresh response, not another model round on this one.
  const pendingPlanViolatesContract = pendingPlanVerdict?.branch === "assessment_contract_violation";
  const agentCanvasPreview: AgentCanvasPreview | null = pendingPlanMayApply && pendingPlan?.compiled_plan
    ? {
        planId: pendingPlan.plan.plan_id,
        expectedRevision: pendingPlan.compiled_plan.transaction.expected_revision,
        operations: pendingPlan.compiled_plan.transaction.operations,
      }
    : null;
  const selectedElements = state.document?.elements.filter((element) => state.selectedElementIds.includes(element.id)) ?? [];
  const selectedConnectors = selectedElements.filter((element) => element.type === "connector");
  const alignableSelection = selectedElements.filter((element) => element.type !== "connector");
  const activeElementId = state.selectedElementIds.at(-1);
  const activeElement = selectedElements.find((element) => element.id === activeElementId) ?? selectedElements.at(-1);
  const selectedGroupIds = new Set(selectedElements.map(readEditorGroupId).filter((value): value is string => Boolean(value)));
  const selectedLockedElements = selectedElements.filter(isElementEditLocked);
  const lockedLayerIds = new Set(state.document?.layers.filter((layer) => layer.locked).map((layer) => layer.id) ?? []);
  const selectedOnLockedLayers = selectedElements.filter((element) => lockedLayerIds.has(element.layer_id));
  const selectionEditingBlocked = selectedLockedElements.length > 0 || selectedOnLockedLayers.length > 0;
  const hasRouteLocks = selectedConnectors.some((connector) => Array.isArray(connector.metadata.locked_route_points) && connector.metadata.locked_route_points.length > 0);
  const visibleLayerIds = new Set(state.document?.layers.filter((layer) => layer.visible).map((layer) => layer.id) ?? []);
  const visibleSystemIds = new Set(state.document?.systems.filter((system) => system.visible).map((system) => system.id) ?? []);
  const visibleElements = state.document?.elements.filter((element) => visibleLayerIds.has(element.layer_id) && visibleSystemIds.has(element.system_id)) ?? [];
  const navigationZones = deriveNavigationZones(visibleElements.map((element) => ({ id: element.id, bounds: rectForElement(element) })));
  const activeZone = currentNavigationZone(navigationZones, canvasView);
  const shortcut = (commandId: string) => shortcutMap[commandId] || undefined;
  const paletteCommands: PaletteCommand[] = [
    { id: "canvas:fit-all", label: "适应全部内容", description: "缩放到全部可见元素", keywords: ["fit all", "zoom"], shortcut: shortcut("canvas:fit-all"), enabled: Boolean(visibleElements.length), group: "command" },
    { id: "canvas:fit-selection", label: "适应当前选择", description: "缩放到选中元素", keywords: ["fit selection", "focus"], shortcut: shortcut("canvas:fit-selection"), enabled: selectedElements.length > 0, group: "command" },
    { id: "canvas:reset-zoom", label: "重置为 100%", description: "保持当前中心重置缩放", keywords: ["100", "zoom reset"], enabled: Boolean(state.document), group: "command" },
    { id: "canvas:fit-agent-preview", label: "定位 Agent 画布预览", description: "适应当前 ghost preview", keywords: ["agent", "preview"], enabled: Boolean(agentCanvasPreview), group: "command" },
    { id: "canvas:avoid-obstacles", label: "选中管线避障布线", description: "确定性绕开设备、文字和节点", keywords: ["route", "obstacle", "避障"], shortcut: shortcut("canvas:avoid-obstacles"), enabled: selectedConnectors.length > 0 && !selectionEditingBlocked, group: "command" },
    { id: "canvas:reroute-selection", label: "重排选中管线", description: "保留锁定锚点并重新正交布线", keywords: ["reroute", "管线"], enabled: selectedConnectors.length > 0 && !selectionEditingBlocked, group: "command" },
    { id: "canvas:clear-route-locks", label: "清除选中管线锚点", description: "移除所有锁定路由点", keywords: ["unlock", "anchor"], enabled: hasRouteLocks && !selectionEditingBlocked, group: "command" },
    { id: "canvas:align-left", label: "左对齐", enabled: alignableSelection.length > 1 && !selectionEditingBlocked, group: "command" },
    { id: "canvas:align-center", label: "水平居中", enabled: alignableSelection.length > 1 && !selectionEditingBlocked, group: "command" },
    { id: "canvas:align-right", label: "右对齐", enabled: alignableSelection.length > 1 && !selectionEditingBlocked, group: "command" },
    { id: "canvas:align-top", label: "顶部对齐", enabled: alignableSelection.length > 1 && !selectionEditingBlocked, group: "command" },
    { id: "canvas:align-middle", label: "垂直居中", enabled: alignableSelection.length > 1 && !selectionEditingBlocked, group: "command" },
    { id: "canvas:align-bottom", label: "底部对齐", enabled: alignableSelection.length > 1 && !selectionEditingBlocked, group: "command" },
    { id: "canvas:distribute-horizontal", label: "水平等距分布", enabled: alignableSelection.length > 2 && !selectionEditingBlocked, group: "command" },
    { id: "canvas:distribute-vertical", label: "垂直等距分布", enabled: alignableSelection.length > 2 && !selectionEditingBlocked, group: "command" },
    { id: "workspace:undo", label: "撤销", shortcut: shortcut("workspace:undo"), enabled: true, group: "command" },
    { id: "workspace:redo", label: "重做", shortcut: shortcut("workspace:redo"), enabled: true, group: "command" },
    { id: "workspace:duplicate", label: "复制选择", shortcut: shortcut("workspace:duplicate"), enabled: selectedElements.length > 0, group: "command" },
    { id: "workspace:delete", label: "删除选择", shortcut: shortcut("workspace:delete"), enabled: selectedElements.length > 0 && !selectedLockedElements.length && !selectedOnLockedLayers.length, group: "command" },
    { id: "workspace:group", label: "分组选中元素", shortcut: shortcut("workspace:group"), enabled: selectedElements.length > 1 && !selectedLockedElements.length && !selectedOnLockedLayers.length, group: "command" },
    { id: "workspace:ungroup", label: "解除选中分组", shortcut: shortcut("workspace:ungroup"), enabled: selectedGroupIds.size > 0 && !selectedLockedElements.length && !selectedOnLockedLayers.length, group: "command" },
    { id: "workspace:lock", label: "锁定选中元素", shortcut: shortcut("workspace:lock"), enabled: selectedElements.some((element) => !isElementEditLocked(element)) && !selectedOnLockedLayers.length, group: "command" },
    { id: "workspace:unlock", label: "解锁选中元素", shortcut: shortcut("workspace:unlock"), enabled: selectedLockedElements.length > 0 && !selectedOnLockedLayers.length, group: "command" },
    { id: "workspace:select-type", label: "选择同类型元素", enabled: Boolean(activeElement), group: "command" },
    { id: "workspace:select-layer", label: "选择同图层元素", enabled: Boolean(activeElement), group: "command" },
    { id: "workspace:select-system", label: "选择同系统元素", enabled: Boolean(activeElement), group: "command" },
    { id: "workspace:select-tag", label: "选择同管线编号", enabled: activeElement?.type === "connector" && Boolean(activeElement.process_tag), group: "command" },
    { id: "workspace:select-group", label: "选择当前分组", enabled: Boolean(activeElement && readEditorGroupId(activeElement)), group: "command" },
    { id: "workspace:select-route-family", label: "选择同路由族", enabled: activeElement?.type === "connector", group: "command" },
    { id: "workspace:invert-selection", label: "反向选择", shortcut: shortcut("workspace:invert-selection"), enabled: visibleElements.length > 0, group: "command" },
    { id: "workspace:select-all", label: "选择全部元素", shortcut: shortcut("workspace:select-all"), enabled: Boolean(state.document?.elements.length), group: "command" },
    { id: "workspace:tool-select", label: "切换到选择工具", shortcut: shortcut("workspace:tool-select"), enabled: true, group: "command" },
    { id: "workspace:tool-line", label: "切换到直线工具", shortcut: shortcut("workspace:tool-line"), enabled: true, group: "command" },
    { id: "workspace:tool-connector", label: "切换到工艺管线工具", shortcut: shortcut("workspace:tool-connector"), enabled: true, group: "command" },
    { id: "workspace:tool-junction", label: "切换到连接节点工具", shortcut: shortcut("workspace:tool-junction"), enabled: true, group: "command" },
    { id: "workspace:tool-rectangle", label: "切换到矩形工具", shortcut: shortcut("workspace:tool-rectangle"), enabled: true, group: "command" },
    { id: "workspace:tool-circle", label: "切换到圆工具", shortcut: shortcut("workspace:tool-circle"), enabled: true, group: "command" },
    { id: "workspace:tool-text", label: "切换到文字工具", shortcut: shortcut("workspace:tool-text"), enabled: true, group: "command" },
    { id: "workspace:agent-panel", label: "打开 Agent 面板", enabled: true, group: "command" },
    { id: "views:open", label: "打开大图视图导航", description: "自动分区与命名视图", shortcut: shortcut("views:open"), enabled: Boolean(state.document), group: "command" },
    { id: "settings:open", label: "打开编辑偏好", description: "主题与快捷键", shortcut: shortcut("settings:open"), enabled: true, group: "command" },
    ...navigationZones.map((zone) => ({ id: `view:${zone.id}`, label: `分区 ${zone.label}`, description: `${zone.elementCount} 个可见元素`, keywords: ["zone", "section", zone.label], enabled: true, group: "view" as const })),
    ...namedViews.map((view) => ({ id: `named-view:${view.id}`, label: view.name, description: "命名视图", keywords: ["saved view", "bookmark"], enabled: true, group: "view" as const })),
    ...elementPaletteCommands(state.document?.elements ?? []),
  ];

  const navigateToBounds = (bounds: NavigationZone["bounds"]) => {
    setCanvasViewportRequest((current) => ({ bounds, nonce: (current?.nonce ?? 0) + 1 }));
  };
  const navigateToView = (view: CanvasView) => {
    setCanvasViewportRequest((current) => ({ view, nonce: (current?.nonce ?? 0) + 1 }));
  };
  const updateNamedViews = (next: NamedCanvasView[]) => {
    const documentId = state.document?.id;
    const sanitized = sanitizeNamedViews(next);
    setNamedViews(sanitized);
    if (documentId) persistNamedViews(documentId, sanitized);
  };
  const saveNamedView = (name: string, view: CanvasView) => {
    const id = `view_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`;
    updateNamedViews([...namedViews, { id, name, view: { ...view }, createdAt: Date.now() }]);
  };
  const executeCommandId = (id: string) => {
    const command = paletteCommands.find((item) => item.id === id);
    if (command && !command.enabled) return false;
    if (id.startsWith("canvas:")) dispatchCanvasCommand(id.slice("canvas:".length) as CanvasCommandId);
    else if (id === "workspace:undo") void state.undo();
    else if (id === "workspace:redo") void state.redo();
    else if (id === "workspace:duplicate") void state.duplicateSelection();
    else if (id === "workspace:delete") void state.deleteSelection();
    else if (id === "workspace:group") void state.groupSelection();
    else if (id === "workspace:ungroup") void state.ungroupSelection();
    else if (id === "workspace:lock") void state.setSelectionLocked(true);
    else if (id === "workspace:unlock") void state.setSelectionLocked(false);
    else if (id === "workspace:select-type") state.selectByScope("type");
    else if (id === "workspace:select-layer") state.selectByScope("layer");
    else if (id === "workspace:select-system") state.selectByScope("system");
    else if (id === "workspace:select-tag") state.selectByScope("process_tag");
    else if (id === "workspace:select-group") state.selectByScope("group");
    else if (id === "workspace:select-route-family") state.selectByScope("route_family");
    else if (id === "workspace:invert-selection") state.selectByScope("invert");
    else if (id === "workspace:select-all") state.selectAll();
    else if (id === "workspace:tool-select") state.setTool("select");
    else if (id === "workspace:tool-line") state.setTool("line");
    else if (id === "workspace:tool-connector") state.setTool("connector");
    else if (id === "workspace:tool-junction") state.setTool("junction");
    else if (id === "workspace:tool-rectangle") state.setTool("rectangle");
    else if (id === "workspace:tool-circle") state.setTool("circle");
    else if (id === "workspace:tool-text") state.setTool("text");
    else if (id === "workspace:agent-panel") setRightPanel("agent");
    else if (id === "settings:open") setExperienceSettingsOpen(true);
    else if (id === "views:open") setViewNavigatorOpen(true);
    else if (id.startsWith("view:")) {
      const zone = navigationZones.find((item) => `view:${item.id}` === id);
      if (zone) navigateToBounds(zone.bounds);
      else return false;
    } else if (id.startsWith("named-view:")) {
      const named = namedViews.find((item) => `named-view:${item.id}` === id);
      if (named) navigateToView(named.view);
      else return false;
    } else return false;
    return true;
  };
  const importJsonFile = async (file: File | undefined, kind: "document" | "project") => {
    if (!file) return;
    setImportError("");
    try {
      const payload = parseImportJson(await file.text(), kind);
      if (kind === "document") await state.importDocumentPayload(payload);
      else await state.importProjectPackagePayload(payload);
    } catch (error) {
      setImportError(error instanceof Error ? error.message : String(error));
    }
  };

  /**
   * DWG/DXF intake. Two deliberate behaviours:
   *
   *  - the file never leaves the client unless its extension is a CAD format, so a
   *    mistyped upload is refused instantly instead of after a 900 KB round trip;
   *  - the resulting report is kept in the panel. "Imported" is not the same claim as
   *    "reproduced faithfully", and the user is the one who has to judge that.
   */
  const importCadFile = async (file: File | undefined, mode: "import" | "plan") => {
    if (!file) return;
    setImportError("");
    setCadReport(null);
    if (!isCadFileName(file.name)) {
      setImportError(`请选择 DWG 或 DXF 图纸（当前文件：${file.name}）`);
      return;
    }
    try {
      if (mode === "plan") {
        const dryRun = await api.planCadDrawing(file);
        setCadReport({
          title: `解析结果 · ${file.name}（未写入）`,
          documentId: null,
          lines: cadReportLines(dryRun.report),
        });
        return;
      }
      const result = await state.importCadDrawing(file);
      setCadReport({
        title: `已导入 ${result.document_name}`,
        documentId: result.document_id,
        lines: cadReportLines(result.report),
      });
    } catch (error) {
      setImportError(error instanceof Error ? error.message : String(error));
    }
  };

  const executePaletteCommand = (command: PaletteCommand) => {
    if (command.elementId) {
      focusCanvasElement(command.elementId);
      return;
    }
    executeCommandId(command.id);
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const pressed = shortcutFromKeyboardEvent(event);
      const commandId = commandForShortcut(pressed, shortcutMap);
      if (commandId === "palette:open") {
        event.preventDefault();
        setCommandPaletteOpen((current) => !current);
        return;
      }
      if (commandPaletteOpen) {
        if (event.key === "Escape") {
          event.preventDefault();
          setCommandPaletteOpen(false);
        }
        return;
      }
      if (
        event.target instanceof HTMLInputElement
        || event.target instanceof HTMLTextAreaElement
        || event.target instanceof HTMLSelectElement
        || (event.target instanceof HTMLElement && event.target.isContentEditable)
      ) return;
      if (commandId && executeCommandId(commandId)) {
        event.preventDefault();
        return;
      }
      if (event.key === "Escape") {
        state.clearSelection();
        state.setTool("select");
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [commandPaletteOpen, shortcutMap, state.document, state.selectedElementIds, namedViews, navigationZones, canvasView]);

  const typesafeKeySource = describeTypesafeKeySource(typesafeStatus);

  return (
    <div className="app-shell" data-testid="app-shell" data-theme={resolvedAppearance} data-document-id={state.document?.id ?? ""} data-revision={state.document?.revision ?? ""}>
      <header className="topbar">
        <div className="brand"><strong>P&amp;ID-Agent</strong><span>轻量 P&amp;ID 人机协同工作区</span></div>
        <div className="toolbar">
          {tools.map((tool) => {
            const shortcut = shortcutMap[`workspace:tool-${tool.id}`] ?? tool.key;
            if (tool.id === "line" || tool.id === "rectangle" || tool.id === "circle") {
              return <ShapeToolButton key={tool.id} tool={tool.id} label={tool.label} shortcut={shortcut} active={state.tool === tool.id} />;
            }
            return <SimpleToolButton key={tool.id} tool={tool.id} label={tool.label} shortcut={shortcut} active={state.tool === tool.id} onSelect={() => state.setTool(tool.id)} />;
          })}
          <div style={{ width: 1, height: 18, background: "var(--border)", margin: "0 4px" }} />
          <BasicShapesToolbar />
        </div>
        <div className="toolbar-actions">
          <button type="button" data-testid="command-palette-trigger" className="command-palette-trigger" onClick={() => setCommandPaletteOpen(true)} title={`命令面板 (${shortcutMap["palette:open"]})`}>命令 <kbd>{shortcutMap["palette:open"]}</kbd></button>
          <button type="button" data-testid="view-navigator-trigger" onClick={() => setViewNavigatorOpen(true)} disabled={!state.document} title={`视图导航 (${shortcutMap["views:open"]})`}>视图</button>
          <button type="button" data-testid="experience-settings-trigger" onClick={() => setExperienceSettingsOpen(true)} title={`编辑偏好 (${shortcutMap["settings:open"]})`}>{resolvedAppearance === "dark" ? "深色" : "浅色"}</button>
          <button onClick={() => void state.duplicateSelection()} disabled={!state.selectedElementIds.length}>复制</button>
          <button onClick={() => void state.undo()}>撤销</button>
          <button onClick={() => void state.redo()}>重做</button>
          {state.document ? <button type="button" onClick={() => void downloadApiResource(`/api/v2/documents/${state.document!.id}/export.svg`, `${state.document!.id}.svg`)}>导出 SVG</button> : null}
          {state.document ? <button type="button" data-testid="export-document-json" onClick={() => void downloadApiResource(`/api/v2/documents/${state.document!.id}/export-v1.json`, `${state.document!.id}.pid.json`)}>导出 JSON</button> : null}
          <button type="button" data-testid="export-project-package" onClick={() => void downloadApiResource("/api/v2/project/export.json", "pid-agent-project.pid.json")}>导出项目包</button>
        </div>
      </header>

      <main className="workspace">
        <aside className="sidebar documents-panel" data-testid="documents-panel">
          <div className="left-panel-nav">
            <button
              type="button"
              className={`left-panel-nav-btn ${leftPanelTab === "all" ? "active" : ""}`}
              onClick={() => setLeftPanelTab("all")}
              title="显示全部（文档与图例）"
            >
              全部
            </button>
            <button
              type="button"
              className={`left-panel-nav-btn ${leftPanelTab === "documents" ? "active" : ""}`}
              onClick={() => setLeftPanelTab("documents")}
              title="聚焦图纸管理与项目分类"
            >
              📁 图纸 ({state.documents.length})
            </button>
            <button
              type="button"
              className={`left-panel-nav-btn ${leftPanelTab === "symbols" ? "active" : ""}`}
              onClick={() => setLeftPanelTab("symbols")}
              title="聚焦单位图例"
            >
              📐 图例
            </button>
          </div>

          {leftPanelTab !== "symbols" ? (
            <div className="left-panel-section">
              <div className="panel-heading"><h2>文档</h2><button data-testid="create-document" onClick={() => { state.clearError(); setCreateDocumentOpen(true); }}>新建</button></div>
              <div className="document-import-actions">
                <button type="button" data-testid="import-document-json" disabled={state.importing} onClick={() => documentImportRef.current?.click()}>导入 JSON</button>
                <button type="button" data-testid="import-project-package" disabled={state.importing} onClick={() => projectImportRef.current?.click()}>导入项目包</button>
                <button
                  type="button"
                  data-testid="import-cad-drawing"
                  disabled={state.importing}
                  title={dwgConverterHint(cadCapabilities) || "导入 DWG / DXF 图纸，原样复现几何并打开"}
                  onClick={() => cadImportRef.current?.click()}
                >
                  导入 DWG/DXF
                </button>
                <button
                  type="button"
                  data-testid="plan-cad-drawing"
                  disabled={state.importing}
                  title="只解析并报告图面内容，不写入任何文档"
                  onClick={() => cadPlanRef.current?.click()}
                >
                  仅解析图纸
                </button>
                <input ref={documentImportRef} data-testid="import-document-input" type="file" accept="application/json,.json" hidden onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ""; void importJsonFile(file, "document"); }} />
                <input ref={projectImportRef} data-testid="import-project-input" type="file" accept="application/json,.json" hidden onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ""; void importJsonFile(file, "project"); }} />
                <input ref={cadImportRef} data-testid="import-cad-input" type="file" accept={CAD_ACCEPT} hidden onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ""; void importCadFile(file, "import"); }} />
                <input ref={cadPlanRef} data-testid="plan-cad-input" type="file" accept={CAD_ACCEPT} hidden onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ""; void importCadFile(file, "plan"); }} />
              </div>
              {dwgConverterHint(cadCapabilities) ? <p className="cad-import-hint" data-testid="cad-import-hint">{dwgConverterHint(cadCapabilities)}</p> : null}
              {importError || state.error ? <div className="document-import-error" role="alert"><span>{importError || state.error}</span><button type="button" onClick={() => { setImportError(""); state.clearError(); }}>关闭</button></div> : null}
              {cadReport ? (
                <div className="cad-import-report" data-testid="cad-import-report">
                  <div className="cad-import-report-head">
                    <strong data-testid="cad-import-report-title">{cadReport.title}</strong>
                    <button type="button" data-testid="cad-import-report-close" onClick={() => setCadReport(null)}>关闭报告</button>
                  </div>
                  <ul data-testid="cad-import-report-body">
                    {cadReport.lines.map((line, index) => (
                      <li key={`${index}-${line.slice(0, 12)}`} data-testid={`cad-import-report-line-${index}`}>{line}</li>
                    ))}
                  </ul>
                  <p className="cad-import-report-note">
                    导入只复现图面几何与图层，不推断管线、设备或仪表语义；未列出的 CAD 构造仍未被表达。
                  </p>
                </div>
              ) : null}
              <details className="service-access-settings">
                <summary>共享部署访问令牌</summary>
                <label>Service token
                  <div className="secret-input-row">
                    <input
                      data-testid="service-token-input"
                      type={showServiceToken ? "text" : "password"}
                      value={serviceToken}
                      onChange={(event: ChangeEvent<HTMLInputElement>) => setServiceToken(event.target.value)}
                      onKeyDown={(event) => { if (event.key === "Enter") applyServiceToken(); }}
                      placeholder="共享部署要求的 Bearer token"
                      autoComplete="off"
                      spellCheck={false}
                    />
                    <button type="button" onClick={() => setShowServiceToken(!showServiceToken)}>{showServiceToken ? "隐藏" : "显示"}</button>
                  </div>
                </label>
                <div className="provider-actions">
                  <button type="button" data-testid="service-token-apply" onClick={applyServiceToken}>应用并重新连接</button>
                  <button type="button" data-testid="service-token-clear" onClick={clearServiceToken}>清除</button>
                </div>
                <p>令牌仅保存在当前浏览器标签页的 sessionStorage；不会写入 URL 或 localStorage，关闭标签页后失效。</p>
              </details>
              <div className="project-summary" data-testid="project-summary"><strong>{state.projectSettings.name}</strong><span>{state.documents.length} 个文档</span></div>
              <div className="document-list">
                <DocumentTree
                  documents={state.documents}
                  activeDocumentId={state.document?.id}
                  folders={projectFolders}
                  busy={state.loading || state.importing || state.isMutating || busyAgent}
                  onOpenDocument={(id) => void state.openDocument(id)}
                  onDeleteDocument={(doc) => {
                    if (window.confirm(documentDeletionConfirmation(doc.name))) {
                      void state.deleteDocument(doc.id);
                    }
                  }}
                  onCreateDocumentInFolder={(folderId) => {
                    state.clearError();
                    setCreateDocumentFolderId(folderId);
                    setCreateDocumentOpen(true);
                  }}
                  onCreateFolder={() => {
                    state.clearError();
                    setCreateFolderOpen(true);
                  }}
                  onRenameFolder={(folder) => {
                    setRenameFolderTarget(folder);
                  }}
                  onDeleteFolder={(folder) => {
                    if (window.confirm(`确定要删除项目分类「${folder.name}」吗？\n该分类下的所有图纸将保留并转入「未分类」。`)) {
                      void state.deleteFolder(folder.id);
                    }
                  }}
                  onMoveDocument={(docId, folderId) => {
                    void state.moveDocumentToFolder(docId, folderId);
                  }}
                />
              </div>
            </div>
          ) : null}

          {leftPanelTab === "all" ? <div className="divider" /> : null}

          {leftPanelTab !== "documents" ? (
            <div className="left-panel-section symbols-section">
              <h2>单位图例</h2>
              <SymbolPalette />
            </div>
          ) : null}
        </aside>

        <section className="canvas-stage" data-testid="canvas-stage" onPointerDownCapture={() => setCanvasPointerActive(true)} onPointerUpCapture={() => setCanvasPointerActive(false)} onPointerCancelCapture={() => setCanvasPointerActive(false)}>
          {state.document ? <>
            <div className="document-bar">
              <strong>{state.document.name}</strong>
              <span>revision {state.document.revision}</span>
              <span>{state.document.elements.length} elements</span>
              <span>{state.selectedElementIds.length} selected</span>
              <button className={`sync-badge sync-${state.syncState}`} onClick={() => syncActionable && void state.refreshDocument()} disabled={!syncActionable} title={state.pendingExternalRevision ? `服务器 revision ${state.pendingExternalRevision}` : undefined}>{state.syncMessage}</button>
              <button type="button" className="document-view-button" onClick={() => setViewNavigatorOpen(true)}>分区 {activeZone?.label ?? "—"} · 命名视图 {namedViews.length}</button>
              <span>框选 · Shift 多选 · 右键快捷操作 · {shortcutMap["palette:open"]} 命令面板</span>
            </div>
            <EditorCanvas agentPreview={agentCanvasPreview} focusRequest={canvasFocusRequest} commandRequest={canvasCommandRequest} viewportRequest={canvasViewportRequest} onViewChange={setCanvasView} />
          </> : <div className="empty-canvas">没有打开的文档</div>}
        </section>

        <aside className="sidebar right-panel">
          <div className="right-panel-tabs" role="tablist" aria-label="右侧面板">
            {tabs.map((tab) => <button key={tab.id} type="button" className={rightPanel === tab.id ? "active" : ""} onClick={() => setRightPanel(tab.id)} role="tab" aria-selected={rightPanel === tab.id}>{tab.label}{tab.id === "properties" && state.selectedElementIds.length ? <span>{state.selectedElementIds.length}</span> : null}</button>)}
          </div>
          {rightPanel === "properties" ? <section className="inspector-panel" role="tabpanel"><h2>元素属性</h2><PropertyInspector /></section> : null}
          {rightPanel === "groups" ? <section className="inspector-panel" role="tabpanel"><h2>图层与工艺系统</h2><LayerSystemPanel /></section> : null}
          {rightPanel === "history" ? <section className="inspector-panel" role="tabpanel"><h2>Revision 历史</h2><HistoryPanel /></section> : null}
          {rightPanel === "reports" ? (
            <section className="inspector-panel" role="tabpanel">
              <h2>工程报表与规则检查</h2>
              <EngineeringReportPanel />
              <h3>工程校验（M4）</h3>
              <ValidationPanel />
            </section>
          ) : null}
          {rightPanel === "graph" ? <section className="inspector-panel" role="tabpanel"><h2>工程语义图（派生）</h2><EngineeringGraphPanel /></section> : null}
          {rightPanel === "drafting" ? <section className="inspector-panel" role="tabpanel"><h2>确定性整理（M3）</h2><DraftingPanel /></section> : null}
          <section className="agent-panel" role="tabpanel" hidden={rightPanel !== "agent"}>
            <h2>P&amp;ID Agent</h2>
            <label>自然语言指令<textarea value={prompt} onChange={(event: ChangeEvent<HTMLTextAreaElement>) => setPrompt(event.target.value)} placeholder="例如：把选中的阀门替换为球阀，并保持原有管线连接。" rows={5} /></label>
            <VisionImageInput scopeId={state.document?.id ?? ""} images={referenceImages} onChange={setReferenceImages} disabled={busyAgent} />
            <details className="agent-context-settings">
              <summary>工艺与设计上下文</summary>
              <label>补充上下文<textarea value={context} onChange={(event: ChangeEvent<HTMLTextAreaElement>) => setContext(event.target.value)} placeholder="粘贴工艺原则、设备要求、位号规则、管线说明等。" rows={7} /></label>
            </details>
            {state.selectedElementIds.length ? <div className="agent-scope">局部修改范围：已选择 {state.selectedElementIds.length} 个元素，并附带其直接相连管线</div> : <div className="agent-scope agent-scope-wide">未选择元素：Agent 将以整张图为范围</div>}
            <AutomaticAgentRunner
              prompt={prompt}
              context={scopedContext()}
              provider={providerConfig()}
              images={agentImages}
              requireVisibleOutput={requireVisibleOutput}
              disabled={busyAgent}
              onRunningChange={setAutomaticAgentRunning}
              onApplied={() => {
                setPendingPlan(null);
                setAgentError("");
                setPrompt("");
                setReferenceImages([]);
              }}
            />
            {planningAgent ? (
              <div className="agent-running-row">
                <button className="primary" disabled>模型规划并编译中…</button>
                <button type="button" className="danger" onClick={stopAgentPlanning}>🛑 停止生成</button>
              </div>
            ) : (
              <button className="primary" disabled={busyAgent || !prompt.trim()} onClick={() => void planAgent()}>仅生成事务预览（手动模式）</button>
            )}
            <AgentStreamingViewer
              thinking={streamingThinking}
              content={streamingContent}
              isStreaming={planningAgent || repairingAgent}
              onStop={stopAgentPlanning}
            />
            <details className="agent-provider-settings">
              <summary>模型服务与高级设置{model ? ` · ${model}` : ""}</summary>
              <label>服务预设<select value={providerPreset} onChange={(event: ChangeEvent<HTMLSelectElement>) => selectProviderPreset(event.target.value)}>{PROVIDER_PRESETS.map((preset) => <option key={preset.id} value={preset.id}>{preset.label}</option>)}</select></label>
              <label>Base URL<input value={baseUrl} onChange={(event: ChangeEvent<HTMLInputElement>) => changeProviderBaseUrl(event.target.value)} placeholder="例如 http://127.0.0.1:11434/v1" /></label>
              <label>API Key<div className="secret-input-row"><input type={showApiKey ? "text" : "password"} value={apiKey} onChange={(event: ChangeEvent<HTMLInputElement>) => setApiKey(event.target.value)} placeholder="只需输入当前服务的 API Key；本地服务可留空" autoComplete="off" spellCheck={false} /><button type="button" onClick={() => setShowApiKey(!showApiKey)}>{showApiKey ? "隐藏" : "显示"}</button></div></label>
              {loadingModels ? <div className="provider-model-status">正在读取模型列表…</div> : null}
              {availableModels.length ? <label>可用模型<select value={availableModels.some((item) => item.id === model) ? model : ""} onChange={(event: ChangeEvent<HTMLSelectElement>) => setModel(event.target.value)}><option value="" disabled>选择模型</option>{availableModels.map((item) => <option key={item.id} value={item.id}>{item.id}{item.owned_by ? ` · ${item.owned_by}` : ""}</option>)}</select></label> : null}
              <label>Model name（可手工覆盖）<input value={model} onChange={(event: ChangeEvent<HTMLInputElement>) => setModel(event.target.value)} placeholder="从列表选择，或直接输入模型名称" /></label>
              <label className="provider-thinking-toggle"><span>思考模式</span><input type="checkbox" checked={thinkingEnabled} onChange={(event: ChangeEvent<HTMLInputElement>) => setThinkingEnabled(event.target.checked)} /></label>
              <label>思考等级<select value={thinkingLevel ?? "high"} disabled={!thinkingEnabled} onChange={(event: ChangeEvent<HTMLSelectElement>) => setThinkingLevel(event.target.value as ProviderConfig["thinking_level"])}><option value="low">低</option><option value="high">高</option><option value="max">最大</option></select></label>
              {/* These labels must not contain another field's accessible name: the browser suite
                  selects fields by role + name, and Playwright's name match is a substring match,
                  so "TypeSafe Base URL" silently made "Base URL" ambiguous. */}
              <div className="typesafe-settings">
                <label className="provider-thinking-toggle"><span>用 TypeSafe 判读并画图</span><input type="checkbox" checked={typesafeEnabled} onChange={(event: ChangeEvent<HTMLInputElement>) => { setTypesafeEnabled(event.target.checked); writeTypesafePreferences({ baseUrl: typesafeBaseUrl, model: typesafeModel, enabled: event.target.checked }); }} /></label>
                <div className="typesafe-hint">开启后，本面板的生成请求改由 TypeSafe System One 判读：代码先按图纸与符号目录列出候选，模型只回答“指的是哪一个”，置信度低于阈值的子句会被跳过而不是猜着画。</div>
                <div className={`typesafe-hint typesafe-key-source typesafe-key-source-${typesafeKeySource.tone}`}>{typesafeKeySource.message}</div>
                <label>TypeSafe Key<div className="secret-input-row"><input type={showTypesafeKey ? "text" : "password"} value={typesafeKey} onChange={(event: ChangeEvent<HTMLInputElement>) => { setTypesafeKey(event.target.value); setTypesafeApiKey(event.target.value); }} placeholder={typesafeKeySource.placeholder} autoComplete="off" spellCheck={false} /><button type="button" onClick={() => setShowTypesafeKey(!showTypesafeKey)}>{showTypesafeKey ? "隐藏" : "显示"}</button></div></label>
                <label>TypeSafe 判读服务地址<input value={typesafeBaseUrl} onChange={(event: ChangeEvent<HTMLInputElement>) => setTypesafeBaseUrl(event.target.value)} placeholder="https://api.typesafe.ai" /></label>
                <label>TypeSafe 判读模型<input value={typesafeModel} onChange={(event: ChangeEvent<HTMLInputElement>) => setTypesafeModel(event.target.value)} placeholder="jev-latest" /></label>
                <div className="provider-actions">
                  <button type="button" onClick={() => void verifyTypesafe()} disabled={testingTypesafe}>{testingTypesafe ? "正在核对…" : "测试 TypeSafe Key"}</button>
                  <button type="button" onClick={() => void refreshTypesafeStatus()}>刷新服务端配置</button>
                </div>
                {typesafeTest ? <div className={typesafeTest.ok ? "provider-model-status" : "provider-model-status error"}>{typesafeTest.message}</div> : null}
                <div className="nl-draw">
                  <label>自然语言一句话出图<input value={nlSentence} onChange={(event: ChangeEvent<HTMLInputElement>) => setNlSentence(event.target.value)} placeholder="例如：添加一个缓冲罐 V-101，添加一台燃料盐泵 P-101，把 V-101 接到 P-101" autoComplete="off" /></label>
                  <div className="typesafe-hint">对一张空图纸生效：代码读句造候选，TypeSafe 只判断代码查不了的子句，冻结的确定性链负责布局与落图。只建图、不改已有图纸；不确定的子句会列出并跳过。</div>
                  <div className="provider-actions">
                    <button type="button" onClick={() => void drawFromSentence(true)} disabled={drawingTextPlan || !nlSentence.trim()}>{drawingTextPlan ? "处理中…" : "预览"}</button>
                    <button type="button" onClick={() => void drawFromSentence(false)} disabled={drawingTextPlan || !nlSentence.trim()}>生成图纸</button>
                    <button type="button" onClick={() => void editFromSentence(true)} disabled={drawingTextPlan || !nlSentence.trim() || !nlSource} title={nlSource ? "预览这句话对现有图纸的语义改图" : "先用「生成图纸」创建一张自然语言图纸"}>预览改图</button>
                    <button type="button" onClick={() => void editFromSentence(false)} disabled={drawingTextPlan || !nlSentence.trim() || !nlSource}>改图</button>
                  </div>
                  {nlError ? <div className="provider-model-status error" style={{ whiteSpace: "pre-line" }}>{nlError}</div> : null}
                  {nlResult ? <div className="nl-draw-result">
                    <div>{nlResult.committed ? `已画入 r${nlResult.revision}` : "预览（未写入图纸）"} · {nlResult.spec.entities.length} 台设备 · {nlResult.spec.connections.length} 条连接 · 模型判读 {nlResult.judgment_count} 次{nlResult.completeness !== "complete" ? ` · 完整度 ${nlResult.completeness}` : ""}</div>
                    {nlResult.notes.map((note) => <div key={note}>· {note}</div>)}
                    {nlResult.undelivered.map((item) => <div key={item}>未兑现：{item}</div>)}
                    {nlResult.skipped.map((item) => <div key={item} className="provider-model-status error">跳过：{item}</div>)}
                    {nlResult.unknown_tags.map((tag) => <div key={tag}>未识别的位号：{tag}</div>)}
                  </div> : null}
                </div>
              </div>
              <div className="provider-actions">
                <button type="button" onClick={() => void discoverProviderModels()} disabled={loadingModels || !baseUrl.trim()}>{loadingModels ? "读取中…" : "刷新模型列表"}</button>
                {loadingModels ? <button type="button" className="danger-inline" onClick={stopProviderDiscovery}>停止发现</button> : null}
                <button type="button" onClick={() => void testCustomProvider()} disabled={testingProvider || !baseUrl.trim() || !model.trim()}>{testingProvider ? "正在测试…" : "测试连接"}</button>
                {testingProvider ? <button type="button" className="danger-inline" onClick={stopProviderTest}>停止测试</button> : null}
              </div>
              {modelDiscoveryError ? <div className="provider-test provider-test-error">{modelDiscoveryError}</div> : null}
              {providerTest ? <div className={`provider-test provider-test-${providerTest.model_available === false ? "warning" : "success"}`}><strong>{providerTest.message}</strong><span>{providerTest.model} · {providerTest.latency_ms} ms · {providerTest.method}</span></div> : null}
              {providerTestError ? <div className="provider-test provider-test-error">{providerTestError}</div> : null}
              <p>{PROVIDER_PRESETS.find((preset) => preset.id === providerPreset)?.note}。预设只填写公开 Base URL。API Key 仅保存在当前页面内存，并随模型列表、测试或生成请求发送，不写入数据库或浏览器存储。</p>
            </details>

            {pendingPlan ? <details className={`agent-result-drawer agent-preview ${pendingPlanMayApply ? "agent-preview-valid" : "agent-preview-invalid"}`} open>
              <summary><strong>{pendingPlanViolatesContract ? "响应契约不兼容，请刷新或重新生成" : pendingPlanMayApply ? "待确认语义事务" : pendingPlanVerdict?.completeness === "partial" ? "事务不完整，需继续规划" : "事务需要修复"}</strong><span>plan {pendingPlan.plan.plan_id.slice(0, 8)} · attempt {pendingPlan.attempt}</span></summary>
              <p>{pendingPlan.plan.explanation || "模型未提供说明"}</p>
              <dl>
                <div><dt>Label</dt><dd>{pendingPlan.plan.transaction.label}</dd></div>
                <div><dt>Revision</dt><dd>r{pendingPlan.plan.transaction.expected_revision ?? pendingPlan.assessment.current_revision}</dd></div>
                <div><dt>语义操作</dt><dd>{pendingPlan.assessment.semantic_operation_count}</dd></div>
                <div><dt>编译操作</dt><dd>{pendingPlan.assessment.compiled_operation_count}</dd></div>
                <div><dt>结果元素数</dt><dd>{pendingPlan.assessment.resulting_element_count ?? "—"}</dd></div>
              </dl>
              <ProposalAccounting result={pendingPlan} />
              {pendingPlan.assessment.rejected_operations?.length ? <section className="agent-rejected-operations">
                <h3>被拒操作（{pendingPlan.assessment.rejected_operations.length}）</h3>
                <ol>
                  {pendingPlan.assessment.rejected_operations.slice(0, 20).map((receipt) => <li key={receipt.operation_id}>
                    <div><strong>{receipt.reason_code}</strong><code>#{receipt.original_index} {receipt.operation_kind}</code></div>
                    <p>{receipt.message}</p>
                    {receipt.suggestions.length ? <ul>{receipt.suggestions.map((suggestion) => <li key={suggestion}>{suggestion}</li>)}</ul> : null}
                  </li>)}
                </ol>
                {pendingPlan.assessment.rejected_operations.length > 20 ? <div className="agent-preview-more">其余 {pendingPlan.assessment.rejected_operations.length - 20} 项未展开</div> : null}
              </section> : null}
              {pendingPlan.annotation_metrics ? <section className="agent-annotation-metrics">
                <h3>标签自动润色</h3>
                <dl>
                  <div><dt>重复标签</dt><dd>{pendingPlan.annotation_metrics.before.duplicate_label_count} → {pendingPlan.annotation_metrics.after.duplicate_label_count}</dd></div>
                  <div><dt>文字互相重叠</dt><dd>{pendingPlan.annotation_metrics.before.text_text_overlaps} → {pendingPlan.annotation_metrics.after.text_text_overlaps}</dd></div>
                  <div><dt>文字覆盖设备</dt><dd>{pendingPlan.annotation_metrics.before.text_symbol_overlaps} → {pendingPlan.annotation_metrics.after.text_symbol_overlaps}</dd></div>
                  <div><dt>文字压住管线</dt><dd>{pendingPlan.annotation_metrics.before.text_connector_intersections} → {pendingPlan.annotation_metrics.after.text_connector_intersections}</dd></div>
                </dl>
                <p>新增标签 {pendingPlan.annotation_metrics.generated_text_ids.length} · 移动 {pendingPlan.annotation_metrics.moved_text_ids.length} · 删除重复 {pendingPlan.annotation_metrics.deleted_text_ids.length} · 引线 {pendingPlan.annotation_metrics.leader_line_ids.length}</p>
              </section> : null}
              <ol className="agent-operation-list">
                {pendingPlan.plan.transaction.operations.slice(0, 30).map((operation, index) => <li key={index}><code>{index}</code><span>{operationDescription(operation)}</span></li>)}
              </ol>
              {pendingPlan.plan.transaction.operations.length > 30 ? <div className="agent-preview-more">其余 {pendingPlan.plan.transaction.operations.length - 30} 项未展开</div> : null}
              {pendingPlan.assessment.issues.length ? <section className="agent-repair-issues">
                <h3>结构化问题</h3>
                {pendingPlan.assessment.issues.map((issue, index) => {
                  const focusId = issue.element_id || issue.connector_id;
                  const canFocus = Boolean(focusId && state.document?.elements.some((element) => element.id === focusId));
                  return <article key={`${issue.code}-${index}`}>
                  <div><strong>{issue.code}</strong><code>{issue.field_path}</code>{canFocus && focusId ? <button type="button" className="agent-issue-focus" onClick={() => focusCanvasElement(focusId)}>画布定位</button> : null}</div>
                  <p>{issue.message}</p>
                  {Object.entries(issue.available_values).map(([name, values]) => <div className="agent-available-values" key={name}><span>{name}</span><code>{values.slice(0, 20).join(", ") || "—"}</code></div>)}
                  {issue.suggestions.length ? <ul>{issue.suggestions.map((suggestion) => <li key={suggestion}>{suggestion}</li>)}</ul> : null}
                </article>;
                })}
              </section> : null}
              <div className="agent-preview-actions">
                <button type="button" className="confirm" disabled={busyAgent || !pendingPlanMayApply} onClick={() => void applyAgentPlan()}>{applyingAgent ? "正在应用…" : "确认应用"}</button>
                {repairingAgent ? (
                  <button type="button" className="danger" onClick={stopAgentPlanning}>🛑 停止重规划</button>
                ) : (
                  // Enabled for a partial plan: "valid but incomplete" is exactly the case that
                  // must be repaired, and the old condition disabled this button for it. Disabled
                  // for a contract violation, whose fix is a fresh response rather than a replan.
                  <button type="button" className="repair" disabled={busyAgent || pendingPlan.attempt >= 5 || pendingPlanMayApply || pendingPlanViolatesContract} onClick={() => void replanAgent()}>{`按回执重规划${pendingPlan.attempt ? `（${pendingPlan.attempt + 1}/5）` : ""}`}</button>
                )}
                <button type="button" disabled={busyAgent} onClick={discardAgentPlan}>放弃预览</button>
              </div>
            </details> : null}

            {agentError ? <div className="error-box"><strong>Agent 操作未完成</strong><span>{agentError}</span>{pendingPlan ? <button onClick={() => void replanAgent()} disabled={busyAgent || pendingPlan.attempt >= 5}>按当前 revision 局部重规划</button> : <button onClick={() => void planAgent()} disabled={busyAgent}>重新生成预览</button>}</div> : null}
            <div className="agent-note">自动完成会在服务端结构化校验失败后连续重规划，并在检测到重复错误或达到 5 次上限时停止。模型设置与详细结果默认折叠，切换属性、图层或历史面板不会中断正在执行的请求。</div>
          </section>
        </aside>
      </main>
      <CreateDocumentDialog
        open={createDocumentOpen}
        busy={state.loading}
        error={state.error}
        folders={projectFolders}
        defaultFolderId={createDocumentFolderId}
        onClose={() => {
          setCreateDocumentOpen(false);
          setCreateDocumentFolderId(undefined);
        }}
        onCreate={(name, folderId) => state.createDocument(name, folderId)}
      />
      <CreateFolderDialog
        open={createFolderOpen}
        busy={state.loading}
        error={state.error}
        onClose={() => setCreateFolderOpen(false)}
        onCreate={(name) => state.createFolder(name)}
      />
      {renameFolderTarget ? (
        <RenameFolderDialog
          open={Boolean(renameFolderTarget)}
          folderId={renameFolderTarget.id}
          currentName={renameFolderTarget.name}
          busy={state.loading}
          error={state.error}
          onClose={() => setRenameFolderTarget(null)}
          onRename={(folderId, newName) => state.renameFolder(folderId, newName)}
        />
      ) : null}
      <CommandPalette
        open={commandPaletteOpen}
        commands={paletteCommands}
        onClose={() => setCommandPaletteOpen(false)}
        onExecute={executePaletteCommand}
      />
      <ViewNavigator
        open={viewNavigatorOpen}
        zones={navigationZones}
        currentZoneId={activeZone?.id}
        namedViews={namedViews}
        currentView={canvasView}
        onClose={() => setViewNavigatorOpen(false)}
        onOpenZone={(zone) => { navigateToBounds(zone.bounds); setViewNavigatorOpen(false); }}
        onOpenNamedView={(view) => { navigateToView(view.view); setViewNavigatorOpen(false); }}
        onSaveNamedView={saveNamedView}
        onRenameNamedView={(id, name) => updateNamedViews(namedViews.map((view) => view.id === id ? { ...view, name: name.slice(0, 80) } : view))}
        onDeleteNamedView={(id) => updateNamedViews(namedViews.filter((view) => view.id !== id))}
      />
      <ExperienceSettings open={experienceSettingsOpen} onClose={() => setExperienceSettingsOpen(false)} />
    </div>
  );
}
