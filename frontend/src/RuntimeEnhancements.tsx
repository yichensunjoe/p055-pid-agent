import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { authorizedFetch, downloadApiResource } from "./api";
import { messageFromError, useWorkspace } from "./store";
import type { ConnectorElement, Document, SymbolElement } from "./types";
import { requestTextInput } from "./editor/InputDialogHost";
import {
  animatedConnector,
  isOpcDefinition,
  isValveDefinition,
  normalizeFlowMedium,
  opcDirection,
  valveState,
} from "./flowRuntime";
import "./runtimeEnhancements.css";

function useDomTarget<T extends Element>(selector: string): T | null {
  const [target, setTarget] = useState<T | null>(() => document.querySelector<T>(selector));
  useEffect(() => {
    const update = () => setTarget(document.querySelector<T>(selector));
    update();
    const observer = new MutationObserver(update);
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [selector]);
  return target;
}

const targetDocumentId = (symbol: SymbolElement): string => {
  const value = symbol.properties.target_document_id;
  return typeof value === "string" ? value : "";
};

function runtimeEnhancementsEnabled(): boolean {
  return import.meta.env.MODE !== "e2e"
    || sessionStorage.getItem("pid-agent:enable-runtime-e2e") === "true";
}

function safeFilename(name: string): string {
  const normalized = name.trim().replace(/[\\/:*?"<>|]+/g, "-").replace(/\s+/g, " ");
  return normalized || "pid-drawing";
}

function responseMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback;
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return fallback;
}

export function RuntimeEnhancements() {
  return runtimeEnhancementsEnabled() ? <RuntimeEnhancementsEnabled /> : null;
}

function RuntimeEnhancementsEnabled() {
  const workspace = useWorkspace();
  const svg = useDomTarget<SVGSVGElement>('svg[data-testid="editor-canvas"]');
  const sidebar = useDomTarget<HTMLElement>('[data-testid="documents-panel"]');
  // Descendant, not child: the documents panel wraps its sections in `.left-panel-section`,
  // and a `>` combinator silently stopped matching when that wrapper was introduced — the
  // rename control's portal simply never rendered. `querySelector` returns the first match,
  // which is the 文档 heading. Do not tighten this back to a child selector without a test.
  const documentHeading = useDomTarget<HTMLElement>('[data-testid="documents-panel"] .panel-heading');
  const toolbarActions = useDomTarget<HTMLElement>(".toolbar-actions");
  const [documentsOpen, setDocumentsOpen] = useState(true);
  const [symbolsOpen, setSymbolsOpen] = useState(true);
  const [runtimeMessage, setRuntimeMessage] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [opcEditing, setOpcEditing] = useState<SymbolElement | null>(null);
  const [opcDraft, setOpcDraft] = useState("");

  const definitionMap = useMemo(
    () => new Map(workspace.symbols.map((definition) => [definition.key, definition])),
    [workspace.symbols],
  );
  const selected = workspace.document?.elements.find(
    (element) => element.id === workspace.selectedElementIds.at(-1),
  );
  const selectedConnector = selected?.type === "connector" ? selected : null;
  const selectedSymbol = selected?.type === "symbol" ? selected : null;
  const selectedDefinition = selectedSymbol ? definitionMap.get(selectedSymbol.symbol_key) : undefined;
  const selectedValve = selectedSymbol && isValveDefinition(selectedDefinition, selectedSymbol.symbol_key)
    ? selectedSymbol
    : null;
  const selectedOpc = selectedSymbol && isOpcDefinition(selectedDefinition, selectedSymbol.symbol_key)
    ? selectedSymbol
    : null;

  useEffect(() => {
    if (!sidebar) return;
    sidebar.classList.toggle("runtime-documents-collapsed", !documentsOpen);
    sidebar.classList.toggle("runtime-symbols-collapsed", !symbolsOpen);
    return () => {
      sidebar.classList.remove("runtime-documents-collapsed", "runtime-symbols-collapsed");
    };
  }, [documentsOpen, sidebar, symbolsOpen]);

  const openDocument = async (documentId: string, rememberCurrent = true) => {
    if (!documentId) return;
    const currentDoc = useWorkspace.getState().document;
    if (rememberCurrent && currentDoc) {
      sessionStorage.setItem("pid-agent:opc-return-document", currentDoc.id);
    }
    setRuntimeMessage("");
    try {
      await useWorkspace.getState().openDocument(documentId);
    } catch {
      setRuntimeMessage("关联的 P&ID 已不存在，请重新设置 OPC 目标。");
    }
  };

  useEffect(() => {
    const onDoubleClick = (event: MouseEvent) => {
      const eventTarget = event.target;
      if (!(eventTarget instanceof Element)) return;
      if (!eventTarget.closest('svg[data-testid="editor-canvas"]')) return;
      const jumpElement = eventTarget.closest("[data-opc-jump-for]");
      const group = eventTarget.closest("[data-element-id]");
      const elementId = jumpElement?.getAttribute("data-opc-jump-for") ?? group?.getAttribute("data-element-id");
      const currentDocument = useWorkspace.getState().document;
      const symbol = currentDocument?.elements.find(
        (element): element is SymbolElement => element.id === elementId && element.type === "symbol",
      );
      if (!symbol) return;
      const definition = useWorkspace.getState().symbols.find((item) => item.key === symbol.symbol_key);
      if (!isOpcDefinition(definition, symbol.symbol_key)) return;
      event.preventDefault();
      event.stopPropagation();
      const targetId = targetDocumentId(symbol);
      if (targetId) {
        void openDocument(targetId);
      } else {
        startOpcEdit(symbol);
      }
    };
    document.addEventListener("dblclick", onDoubleClick, true);
    return () => document.removeEventListener("dblclick", onDoubleClick, true);
  }, [workspace.document?.id, workspace.documents]);

  const updateConnector = (patch: Record<string, unknown>, label: string) => {
    if (!selectedConnector) return;
    void workspace.transact(
      [{ op: "update_element", element_id: selectedConnector.id, patch }],
      label,
    );
  };

  const updateSymbolProperties = (symbol: SymbolElement, patch: Record<string, unknown>, label: string) => {
    void workspace.transact(
      [{
        op: "update_element",
        element_id: symbol.id,
        patch: { properties: { ...symbol.properties, ...patch } },
      }],
      label,
    );
  };

  const startOpcEdit = (symbol: SymbolElement) => {
    const current = useWorkspace.getState().document;
    const latest = current?.elements.find(
      (element): element is SymbolElement => element.id === symbol.id && element.type === "symbol",
    );
    const existing = latest?.metadata.embedded_off_page_label;
    setOpcDraft(typeof existing === "string" ? existing : "");
    setOpcEditing(symbol);
  };

  const saveOpcEdit = () => {
    if (!opcEditing) return;
    const symbolId = opcEditing.id;
    const current = useWorkspace.getState().document;
    const latest = current?.elements.find(
      (element): element is SymbolElement => element.id === symbolId && element.type === "symbol",
    );
    setOpcEditing(null);
    if (!latest) return;
    const text = opcDraft.trim();
    const metadata = { ...latest.metadata };
    if (text) {
      metadata.embedded_off_page_label = text;
    } else {
      delete metadata.embedded_off_page_label;
    }
    void workspace.transact(
      [{ op: "update_element", element_id: symbolId, patch: { metadata } }],
      text ? `Set OPC description to ${text}` : "Clear OPC description",
    );
  };

  const renameCurrentDocument = async () => {
    const current = workspace.document;
    if (!current || renaming) return;
    const requested = await requestTextInput({ title: "重命名 P&ID 图纸", label: "图纸名称", initialValue: current.name, allowEmpty: false, confirmLabel: "保存" });
    if (!requested || requested === current.name) return;
    setRenaming(true);
    setRuntimeMessage("");
    try {
      const response = await authorizedFetch(`/api/v2/documents/${encodeURIComponent(current.id)}/name`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: requested, expected_revision: current.revision }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        setRuntimeMessage(responseMessage(payload, "图纸名称未更新，请刷新后再试。"));
        return;
      }
      const updated = await response.json() as Document;
      const active = useWorkspace.getState().document;
      if (!active || active.id !== current.id || active.revision !== current.revision) {
        setRuntimeMessage("图纸已切换，旧重命名响应未写回当前画布。");
        return;
      }
      useWorkspace.setState({ document: updated });
      await useWorkspace.getState().refreshDocument();
      setRuntimeMessage(`图纸已重命名为“${updated.name}”。`);
    } catch (error) {
      setRuntimeMessage(`重命名失败：${messageFromError(error)}`);
    } finally {
      setRenaming(false);
    }
  };

  const exportPng = async () => {
    const current = workspace.document;
    if (!current) return;
    try {
      await downloadApiResource(
        `/api/v2/documents/${encodeURIComponent(current.id)}/export.png?scale=1`,
        `${safeFilename(current.name)}.png`,
      );
    } catch (error) {
      setRuntimeMessage(`PNG 导出失败：${messageFromError(error)}`);
    }
  };

  const returnDocumentId = sessionStorage.getItem("pid-agent:opc-return-document") ?? "";
  const canReturn = returnDocumentId
    && returnDocumentId !== workspace.document?.id
    && workspace.documents.some((item) => item.id === returnDocumentId);

  const sidebarControls = sidebar ? createPortal(
    <div className="runtime-sidebar-switcher" aria-label="左侧工作区分区">
      <button
        type="button"
        className={documentsOpen ? "active" : ""}
        aria-expanded={documentsOpen}
        onClick={() => setDocumentsOpen((value) => !value)}
      >
        P&amp;ID {documentsOpen ? "收起" : "展开"}
      </button>
      <button
        type="button"
        className={symbolsOpen ? "active" : ""}
        aria-expanded={symbolsOpen}
        onClick={() => setSymbolsOpen((value) => !value)}
      >
        图例 {symbolsOpen ? "收起" : "展开"}
      </button>
    </div>,
    sidebar,
  ) : null;

  const renameControl = documentHeading ? createPortal(
    <button
      type="button"
      className="runtime-rename-document"
      disabled={!workspace.document || renaming}
      onClick={() => void renameCurrentDocument()}
      title="重命名当前 P&ID 图纸"
    >
      {renaming ? "重命名中…" : "重命名"}
    </button>,
    documentHeading,
  ) : null;

  const pngControl = toolbarActions ? createPortal(
    <button
      type="button"
      className="runtime-export-png"
      disabled={!workspace.document}
      onClick={exportPng}
      title="导出当前图纸为 PNG 图片"
    >
      导出 PNG
    </button>,
    toolbarActions,
  ) : null;

  const canvasOverlay = svg && workspace.document ? createPortal(
    <g className="runtime-process-overlays" pointerEvents="none">
      {workspace.document.elements
        .filter((element): element is ConnectorElement => element.type === "connector" && animatedConnector(element))
        .map((connector) => (
          <polyline
            key={`runtime-flow-${connector.id}`}
            data-flow-for={connector.id}
            className={`runtime-flow-line medium-${normalizeFlowMedium(connector.medium)} direction-${connector.flow_direction}`}
            points={connector.points.map((point) => `${point.x},${point.y}`).join(" ")}
          />
        ))}
      {workspace.document.elements
        .filter((element): element is SymbolElement => element.type === "symbol")
        .map((symbol) => {
          const definition = definitionMap.get(symbol.symbol_key);
          if (isValveDefinition(definition, symbol.symbol_key)) {
            const state = valveState(symbol);
            return (
              <g
                key={`runtime-valve-${symbol.id}`}
                data-valve-state-for={symbol.id}
                className={`runtime-valve-state state-${state}`}
                transform={`translate(${symbol.position.x + symbol.width - 9} ${symbol.position.y + 9})`}
              >
                <circle r="7" />
                <text y="3.5" textAnchor="middle">{state === "closed" ? "C" : "O"}</text>
              </g>
            );
          }
          if (isOpcDefinition(definition, symbol.symbol_key) && targetDocumentId(symbol)) {
            const targetId = targetDocumentId(symbol);
            return (
              <g key={`runtime-opc-${symbol.id}`}>
                <rect
                  data-opc-jump-for={symbol.id}
                  x={symbol.position.x}
                  y={symbol.position.y}
                  width={symbol.width}
                  height={symbol.height}
                  fill="transparent"
                  pointerEvents="all"
                  style={{ cursor: "pointer" }}
                  onDoubleClick={(event) => {
                    event.stopPropagation();
                    void openDocument(targetId);
                  }}
                />
                <text
                  className="runtime-opc-link-badge"
                  x={symbol.position.x + symbol.width / 2}
                  y={symbol.position.y - 7}
                  textAnchor="middle"
                  pointerEvents="all"
                  style={{ cursor: "pointer" }}
                  onClick={(event) => {
                    event.stopPropagation();
                    void openDocument(targetId);
                  }}
                >
                  打开关联图纸
                </text>
              </g>
            );
          }
          return null;
        })}
    </g>,
    svg,
  ) : null;

  const panelVisible = Boolean(selectedConnector || selectedValve || selectedOpc || runtimeMessage || canReturn);

  return <>
    {sidebarControls}
    {renameControl}
    {pngControl}
    {canvasOverlay}
    {panelVisible ? <aside className="runtime-flow-panel" aria-label="工艺流向状态">
      <div className="runtime-flow-panel-heading">
        <strong>工艺流向状态</strong>
        <span>非阻断显示</span>
      </div>

      {selectedConnector ? <section>
        <h3>选中管线</h3>
        <label>介质类别
          <select
            value={normalizeFlowMedium(selectedConnector.medium)}
            onChange={(event) => {
              const medium = event.target.value;
              updateConnector({ medium }, `Set ${selectedConnector.id} medium to ${medium}`);
            }}
          >
            <option value="water">水</option>
            <option value="gas">气体</option>
            <option value="other">其他/自定义</option>
          </select>
        </label>
        <label>流向
          <select
            value={selectedConnector.flow_direction}
            onChange={(event) => updateConnector(
              { flow_direction: event.target.value },
              `Set ${selectedConnector.id} flow direction`,
            )}
          >
            <option value="none">未指定</option>
            <option value="forward">Source → Target</option>
            <option value="reverse">Target → Source</option>
          </select>
        </label>
        <small>指定流向后以绿色动态流道显示；工程主线及导出保持不变。</small>
      </section> : null}

      {selectedValve ? <section>
        <h3>阀门状态</h3>
        <div className="runtime-segmented">
          <button
            type="button"
            className={valveState(selectedValve) === "open" ? "active" : ""}
            onClick={() => updateSymbolProperties(selectedValve, { valve_state: "open" }, "Open valve")}
          >开</button>
          <button
            type="button"
            className={valveState(selectedValve) === "closed" ? "active danger" : ""}
            onClick={() => updateSymbolProperties(selectedValve, { valve_state: "closed" }, "Close valve")}
          >关</button>
        </div>
        <small>阀门状态仅用于表达工艺状态，不再生成阻断报错。</small>
      </section> : null}

      {selectedOpc ? <section>
        <h3>OPC {opcDirection(selectedDefinition, selectedOpc)?.toUpperCase() ?? ""}</h3>
        <label>关联 P&amp;ID
          <select
            value={targetDocumentId(selectedOpc)}
            onChange={(event) => updateSymbolProperties(
              selectedOpc,
              {
                target_document_id: event.target.value,
                opc_direction: opcDirection(selectedDefinition, selectedOpc),
              },
              "Link OPC document",
            )}
          >
            <option value="">未关联</option>
            {workspace.documents
              .filter((document) => document.id !== workspace.document?.id)
              .map((document) => <option key={document.id} value={document.id}>{document.name}</option>)}
          </select>
        </label>
        <button
          type="button"
          disabled={!targetDocumentId(selectedOpc)}
          onClick={() => void openDocument(targetDocumentId(selectedOpc))}
        >跳转到关联 P&ID</button>
        <small>在画布上双击 OPC 可编辑框内文字描述；关联图纸后可通过上方按钮跳转，往返需在目标图放置相反方向 OPC。</small>
      </section> : null}

      {canReturn ? <button type="button" onClick={() => void openDocument(returnDocumentId, false)}>返回上一张 P&ID</button> : null}
      {runtimeMessage ? <div className="runtime-flow-message" role="status">{runtimeMessage}</div> : null}
    </aside> : null}
    {opcEditing ? createPortal(
      <div
        className="runtime-opc-editor-backdrop"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) setOpcEditing(null);
        }}
      >
        <div className="runtime-opc-editor" role="dialog" aria-label="OPC 文字描述">
          <h3>OPC 文字描述</h3>
          <input
            autoFocus
            value={opcDraft}
            placeholder="输入描述，例如：尾气处理系统"
            onChange={(event) => setOpcDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") saveOpcEdit();
              if (event.key === "Escape") setOpcEditing(null);
            }}
          />
          <small>文字将居中显示在 OPC 框内；清空后恢复默认箭头样式。</small>
          <div className="runtime-opc-editor-actions">
            <button type="button" onClick={() => setOpcEditing(null)}>取消</button>
            <button type="button" className="primary" onClick={saveOpcEdit}>确定</button>
          </div>
        </div>
      </div>,
      document.body,
    ) : null}
  </>;
}
