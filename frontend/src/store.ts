import { create } from "zustand";
import { api } from "./api";
import { nextDocumentIdAfterDeletion } from "./documentDeletion";
import { mutationOriginCanApply, mutationResponseCanApply, type WorkspaceMutationOrigin } from "./storeRequestGuard";
import {
  directLockedOperationTargets,
  expandSelectionByGroups,
  isElementEditLocked,
  metadataWithEditorLock,
  metadataWithGroup,
  normalizedGroupMembers,
  readEditorGroupId,
  semanticSelection,
  type SelectionScope,
} from "./editor/selectionEditing";
import type {
  ConnectorEndpoint,
  Document,
  DocumentSummary,
  Element,
  LineVariety,
  CircleVariety,
  Operation,
  Point,
  ProjectFolder,
  ProjectSettings,
  RectangleVariety,
  SymbolDefinition,
  Tool,
} from "./types";

const newElementId = () => `el_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`;
const newGroupId = () => `group_${crypto.randomUUID().replaceAll("-", "").slice(0, 12)}`;
let documentRequestGeneration = 0;
let mutationRequestGeneration = 0;

export function messageFromError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function shiftPoint(point: Point, dx: number, dy: number): Point {
  return { x: point.x + dx, y: point.y + dy };
}

function shiftedEndpoint(
  endpoint: ConnectorEndpoint | null | undefined,
  idMap: Map<string, string>,
  dx: number,
  dy: number,
): ConnectorEndpoint | null | undefined {
  if (!endpoint) return endpoint;
  const mapped = endpoint.element_id ? idMap.get(endpoint.element_id) : undefined;
  return {
    element_id: mapped,
    port_id: mapped ? endpoint.port_id : undefined,
    point: shiftPoint(endpoint.point, dx, dy),
  };
}

function duplicateElement(element: Element, idMap: Map<string, string>, offset: number): Element {
  const clone = structuredClone(element);
  clone.id = idMap.get(element.id)!;
  if (clone.type === "line") {
    clone.start = shiftPoint(clone.start, offset, offset);
    clone.end = shiftPoint(clone.end, offset, offset);
  } else if (clone.type === "rectangle") {
    clone.x += offset;
    clone.y += offset;
  } else if (clone.type === "circle") {
    clone.center = shiftPoint(clone.center, offset, offset);
  } else if (clone.type === "text" || clone.type === "symbol" || clone.type === "junction") {
    clone.position = shiftPoint(clone.position, offset, offset);
  } else {
    clone.points = clone.points.map((point) => shiftPoint(point, offset, offset));
    if (clone.type === "connector") {
      clone.source = shiftedEndpoint(clone.source, idMap, offset, offset);
      clone.target = shiftedEndpoint(clone.target, idMap, offset, offset);
      const locked = clone.metadata.locked_route_points;
      if (Array.isArray(locked)) {
        clone.metadata.locked_route_points = locked.map((value) => {
          if (!value || typeof value !== "object") return value;
          const point = value as { x?: unknown; y?: unknown };
          return typeof point.x === "number" && typeof point.y === "number"
            ? shiftPoint({ x: point.x, y: point.y }, offset, offset)
            : value;
        });
      }
      clone.routing = "manual";
    }
  }
  return clone;
}


function visibleElements(document: Document): Element[] {
  const visibleLayers = new Set(document.layers.filter((layer) => layer.visible).map((layer) => layer.id));
  const visibleSystems = new Set(document.systems.filter((system) => system.visible).map((system) => system.id));
  return document.elements.filter((element) => visibleLayers.has(element.layer_id) && visibleSystems.has(element.system_id));
}

type SyncState = "idle" | "checking" | "synced" | "pending" | "updated" | "error";

type State = {
  documents: DocumentSummary[];
  document: Document | null;
  symbols: SymbolDefinition[];
  tool: Tool;
  selectedElementIds: string[];
  /** True when the last selection change came from the canvas/toolbar (reveal 属性).
   *  Panels that highlight results pass `revealProperties: false` so the user keeps reading the panel. */
  selectionRevealsProperties: boolean;
  selectedSymbolKey: string | null;
  lineVariety: LineVariety;
  rectangleVariety: RectangleVariety;
  circleVariety: CircleVariety;
  loading: boolean;
  importing: boolean;
  projectSettings: ProjectSettings;
  isMutating: boolean;
  error: string | null;
  syncState: SyncState;
  syncMessage: string;
  pendingExternalRevision: number | null;
  loadWorkspace: () => Promise<void>;
  createDocument: (name: string, folderId?: string) => Promise<boolean>;
  createFolder: (name: string) => Promise<boolean>;
  renameFolder: (folderId: string, newName: string) => Promise<boolean>;
  deleteFolder: (folderId: string) => Promise<boolean>;
  moveDocumentToFolder: (documentId: string, folderId: string) => Promise<boolean>;
  deleteDocument: (id: string) => Promise<void>;
  importDocumentPayload: (payload: unknown) => Promise<void>;
  importProjectPackagePayload: (payload: unknown) => Promise<void>;
  clearError: () => void;
  openDocument: (id: string) => Promise<void>;
  setTool: (tool: Tool) => void;
  setSelection: (ids: string[], options?: { expandGroups?: boolean; revealProperties?: boolean }) => void;
  toggleSelection: (id: string, options?: { expandGroups?: boolean }) => void;
  clearSelection: () => void;
  selectAll: () => void;
  selectByScope: (scope: SelectionScope, activeId?: string) => void;
  groupSelection: () => Promise<void>;
  ungroupSelection: () => Promise<void>;
  setSelectionLocked: (locked: boolean) => Promise<void>;
  chooseSymbol: (key: string) => void;
  setLineVariety: (variety: LineVariety) => void;
  setRectangleVariety: (variety: RectangleVariety) => void;
  setCircleVariety: (variety: CircleVariety) => void;
  transact: (operations: Operation[], label: string) => Promise<void>;
  duplicateSelection: () => Promise<void>;
  deleteSelection: () => Promise<void>;
  undo: () => Promise<void>;
  redo: () => Promise<void>;
  checkForExternalUpdates: (applyChanges?: boolean) => Promise<void>;
  refreshDocument: () => Promise<void>;
  generate: (
    prompt: string,
    context: string,
    provider?: {
      base_url?: string;
      model?: string;
      timeout_seconds?: number;
      thinking_enabled?: boolean;
      thinking_level?: "low" | "high" | "max";
    },
  ) => Promise<string>;
};

export const useWorkspace = create<State>((set, get) => ({
  documents: [],
  document: null,
  symbols: [],
  tool: "select",
  selectedElementIds: [],
  selectionRevealsProperties: true,
  selectedSymbolKey: null,
  lineVariety: "solid",
  rectangleVariety: "solid",
  circleVariety: "solid",
  loading: false,
  importing: false,
  projectSettings: { name: "P&ID Project", metadata: {} },
  isMutating: false,
  error: null,
  syncState: "idle",
  syncMessage: "尚未同步",
  pendingExternalRevision: null,

  loadWorkspace: async () => {
    set({ loading: true, error: null, syncState: "checking", syncMessage: "正在载入工作区…" });
    try {
      const [documents, symbols, projectSettings] = await Promise.all([
        api.listDocuments(),
        api.listSymbols(),
        api.getProjectSettings(),
      ]);
      const document = documents.length === 0
        ? await api.createDocument("新建 P&ID")
        : await api.getDocument(documents[0].id);
      set({
        documents: await api.listDocuments(),
        symbols,
        projectSettings,
        document,
        selectedElementIds: [],
        loading: false,
        syncState: "synced",
        syncMessage: `已同步至 r${document.revision}`,
        pendingExternalRevision: null,
      });
    } catch (error) {
      set({
        error: messageFromError(error),
        loading: false,
        syncState: "error",
        syncMessage: "工作区载入失败",
      });
    }
  },

  createDocument: async (name, folderId) => {
    const normalizedName = name.trim();
    if (!normalizedName) return false;
    set({ loading: true, error: null, syncState: "checking", syncMessage: "正在创建文档…" });
    try {
      const document = await api.createDocument(normalizedName, { folder_id: folderId });
      set({
        document,
        documents: await api.listDocuments(),
        selectedElementIds: [],
        loading: false,
        syncState: "synced",
        syncMessage: `已同步至 r${document.revision}`,
        pendingExternalRevision: null,
      });
      return true;
    } catch (error) {
      set({
        error: messageFromError(error),
        loading: false,
        syncState: "error",
        syncMessage: "文档创建失败",
      });
      return false;
    }
  },

  createFolder: async (name) => {
    const normalized = name.trim();
    if (!normalized) return false;
    const currentSettings = get().projectSettings;
    const existingFolders = ((currentSettings.metadata?.folders as ProjectFolder[] | undefined) ?? []);
    const newFolder: ProjectFolder = {
      id: `folder_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
      name: normalized,
      created_at: new Date().toISOString(),
    };
    const nextSettings: ProjectSettings = {
      ...currentSettings,
      metadata: {
        ...(currentSettings.metadata ?? {}),
        folders: [...existingFolders, newFolder],
      },
    };
    try {
      const saved = await api.updateProjectSettings(nextSettings);
      set({ projectSettings: saved });
      return true;
    } catch (error) {
      set({ error: messageFromError(error) });
      return false;
    }
  },

  renameFolder: async (folderId, newName) => {
    const normalized = newName.trim();
    if (!normalized) return false;
    const currentSettings = get().projectSettings;
    const existingFolders = ((currentSettings.metadata?.folders as ProjectFolder[] | undefined) ?? []);
    const nextSettings: ProjectSettings = {
      ...currentSettings,
      metadata: {
        ...(currentSettings.metadata ?? {}),
        folders: existingFolders.map((f) => (f.id === folderId ? { ...f, name: normalized } : f)),
      },
    };
    try {
      const saved = await api.updateProjectSettings(nextSettings);
      set({ projectSettings: saved });
      return true;
    } catch (error) {
      set({ error: messageFromError(error) });
      return false;
    }
  },

  deleteFolder: async (folderId) => {
    const currentSettings = get().projectSettings;
    const existingFolders = ((currentSettings.metadata?.folders as ProjectFolder[] | undefined) ?? []);
    const nextSettings: ProjectSettings = {
      ...currentSettings,
      metadata: {
        ...(currentSettings.metadata ?? {}),
        folders: existingFolders.filter((f) => f.id !== folderId),
      },
    };
    try {
      const saved = await api.updateProjectSettings(nextSettings);
      const docs = await api.listDocuments();
      set({ projectSettings: saved, documents: docs });
      return true;
    } catch (error) {
      set({ error: messageFromError(error) });
      return false;
    }
  },

  moveDocumentToFolder: async (documentId, folderId) => {
    const docs = get().documents;
    const target = docs.find((d) => d.id === documentId);
    if (!target) return false;
    try {
      const updated = await api.moveDocumentFolder(documentId, folderId, target.revision);
      const nextDocs = await api.listDocuments();
      set((state) => ({
        documents: nextDocs,
        document: state.document?.id === documentId ? updated : state.document,
      }));
      return true;
    } catch (error) {
      set({ error: messageFromError(error) });
      return false;
    }
  },

  deleteDocument: async (id) => {
    const before = get();
    const target = before.documents.find((document) => document.id === id);
    if (!target) {
      set({ error: "要删除的文档已不在当前列表中，请刷新后重试。" });
      return;
    }
    const requestGeneration = ++documentRequestGeneration;
    mutationRequestGeneration += 1;
    set({
      loading: true,
      isMutating: false,
      error: null,
      syncState: "checking",
      syncMessage: `正在删除 ${target.name}…`,
    });
    let deleted = false;
    try {
      await api.deleteDocument(id, target.revision);
      deleted = true;
      if (requestGeneration !== documentRequestGeneration) {
        const latest = get();
        const deletedIsStillCurrent = latest.document?.id === id;
        set({
          documents: latest.documents.filter((document) => document.id !== id),
          document: deletedIsStillCurrent ? null : latest.document,
          selectedElementIds: deletedIsStillCurrent ? [] : latest.selectedElementIds,
          pendingExternalRevision: deletedIsStillCurrent
            ? null
            : latest.pendingExternalRevision,
        });
        return;
      }

      const optimisticDocuments = before.documents.filter((document) => document.id !== id);
      const optimisticNextId = nextDocumentIdAfterDeletion(
        before.documents,
        optimisticDocuments,
        id,
        before.document?.id ?? null,
      );
      const keptCurrentDocument = before.document !== null
        && optimisticNextId === before.document.id
        && id !== before.document.id;
      set({
        documents: optimisticDocuments,
        document: keptCurrentDocument ? before.document : null,
        selectedElementIds: keptCurrentDocument ? before.selectedElementIds : [],
        syncState: "checking",
        syncMessage: `已删除 ${target.name} · 正在刷新文档列表…`,
        pendingExternalRevision: null,
      });

      const documents = await api.listDocuments();
      if (requestGeneration !== documentRequestGeneration) return;
      const nextId = nextDocumentIdAfterDeletion(
        before.documents,
        documents,
        id,
        before.document?.id ?? null,
      );
      const keepCurrentAfterRefresh = before.document !== null
        && nextId === before.document.id
        && id !== before.document.id;
      const document = keepCurrentAfterRefresh
        ? before.document
        : nextId
          ? await api.getDocument(nextId)
          : null;
      if (requestGeneration !== documentRequestGeneration) return;
      set({
        documents,
        document,
        selectedElementIds: keepCurrentAfterRefresh ? before.selectedElementIds : [],
        loading: false,
        syncState: document ? "synced" : "idle",
        syncMessage: document
          ? `已删除 ${target.name} · 已打开 ${document.name}`
          : `已删除 ${target.name} · 当前项目为空`,
        pendingExternalRevision: null,
      });
    } catch (error) {
      if (requestGeneration !== documentRequestGeneration) return;
      set({
        error: deleted
          ? `${target.name} 已删除，但文档列表刷新失败：${messageFromError(error)}`
          : messageFromError(error),
        loading: false,
        syncState: "error",
        syncMessage: deleted
          ? `${target.name} 已删除 · 请刷新工作区`
          : `${target.name} 删除未完成`,
      });
    }
  },

  importDocumentPayload: async (payload) => {
    set({ importing: true, error: null, syncState: "checking", syncMessage: "正在导入文档…" });
    try {
      const result = await api.importDocument(payload);
      const document = result.documents[0];
      set({
        document,
        documents: await api.listDocuments(),
        selectedElementIds: [],
        importing: false,
        syncState: "synced",
        syncMessage: `已导入 ${document.name} · r${document.revision}`,
        pendingExternalRevision: null,
      });
    } catch (error) {
      set({
        error: messageFromError(error),
        importing: false,
        syncState: "error",
        syncMessage: "文档导入失败，现有工程未修改",
      });
      throw error;
    }
  },

  importProjectPackagePayload: async (payload) => {
    set({ importing: true, error: null, syncState: "checking", syncMessage: "正在导入项目包…" });
    try {
      const result = await api.importProjectPackage(payload);
      const document = result.documents[0];
      set({
        document,
        documents: await api.listDocuments(),
        projectSettings: result.project ?? get().projectSettings,
        selectedElementIds: [],
        importing: false,
        syncState: "synced",
        syncMessage: `已导入项目包 · ${result.documents.length} 个文档`,
        pendingExternalRevision: null,
      });
    } catch (error) {
      set({
        error: messageFromError(error),
        importing: false,
        syncState: "error",
        syncMessage: "项目包导入失败，现有工程未修改",
      });
      throw error;
    }
  },

  clearError: () => set({ error: null }),

  openDocument: async (id) => {
    const requestGeneration = ++documentRequestGeneration;
    mutationRequestGeneration += 1;
    set({
      loading: true,
      isMutating: false,
      error: null,
      selectedElementIds: [],
      syncState: "checking",
      syncMessage: "正在打开文档…",
    });
    try {
      const document = await api.getDocument(id);
      if (requestGeneration !== documentRequestGeneration) return;
      const documents = get().documents.some((item) => item.id === id)
        ? get().documents
        : await api.listDocuments();
      if (requestGeneration !== documentRequestGeneration) return;
      set({
        document,
        documents,
        loading: false,
        syncState: "synced",
        syncMessage: `已同步至 r${document.revision}`,
        pendingExternalRevision: null,
      });
    } catch (error) {
      if (requestGeneration !== documentRequestGeneration) return;
      set({
        error: messageFromError(error),
        loading: false,
        syncState: "error",
        syncMessage: "文档打开失败",
      });
    }
  },

  setTool: (tool) => set({ tool, selectedElementIds: tool === "select" ? get().selectedElementIds : [] }),
  setSelection: (ids, options) => {
    const document = get().document;
    const unique = [...new Set(ids)];
    set({
      selectedElementIds: document && options?.expandGroups !== false ? expandSelectionByGroups(document.elements, unique) : unique,
      selectionRevealsProperties: options?.revealProperties !== false,
    });
  },
  toggleSelection: (id, options) => {
    const document = get().document;
    const selected = get().selectedElementIds;
    const targets = document && options?.expandGroups !== false ? expandSelectionByGroups(document.elements, [id]) : [id];
    const selectedSet = new Set(selected);
    const remove = targets.every((target) => selectedSet.has(target));
    set({ selectedElementIds: remove
      ? selected.filter((item) => !targets.includes(item))
      : [...new Set([...selected, ...targets])] });
  },
  clearSelection: () => set({ selectedElementIds: [] }),
  selectAll: () => {
    const document = get().document;
    set({ selectedElementIds: document ? visibleElements(document).map((item) => item.id) : [] });
  },
  selectByScope: (scope, requestedActiveId) => {
    const document = get().document;
    if (!document) return;
    const activeId = requestedActiveId ?? get().selectedElementIds.at(-1) ?? null;
    const visible = visibleElements(document);
    set({ selectedElementIds: semanticSelection(visible, activeId, scope, get().selectedElementIds) });
  },
  groupSelection: async () => {
    const document = get().document;
    if (!document) return;
    const selected = document.elements.filter((element) => get().selectedElementIds.includes(element.id));
    if (selected.length < 2) {
      set({ error: "至少选择两个元素才能分组。" });
      return;
    }
    const lockedLayers = new Set(document.layers.filter((layer) => layer.locked).map((layer) => layer.id));
    const blocked = selected.filter((element) => isElementEditLocked(element) || lockedLayers.has(element.layer_id));
    if (blocked.length) {
      set({ error: `锁定元素不能分组：${blocked.map((element) => element.id).join(", ")}` });
      return;
    }
    const groupId = newGroupId();
    await get().transact(selected.map((element) => ({ op: "update_element", element_id: element.id, patch: { metadata: metadataWithGroup(element, groupId) } })), `Group ${selected.length} elements`);
    get().setSelection(selected.map((element) => element.id));
  },
  ungroupSelection: async () => {
    const document = get().document;
    if (!document) return;
    const selected = document.elements.filter((element) => get().selectedElementIds.includes(element.id) && readEditorGroupId(element));
    if (!selected.length) return;
    await get().transact(selected.map((element) => ({ op: "update_element", element_id: element.id, patch: { metadata: metadataWithGroup(element, null) } })), `Ungroup ${selected.length} elements`);
    get().setSelection(selected.map((element) => element.id), { expandGroups: false });
  },
  setSelectionLocked: async (locked) => {
    const document = get().document;
    if (!document) return;
    const selected = document.elements.filter((element) => get().selectedElementIds.includes(element.id) && isElementEditLocked(element) !== locked);
    if (!selected.length) return;
    await get().transact(selected.map((element) => ({ op: "update_element", element_id: element.id, patch: { metadata: metadataWithEditorLock(element, locked) } })), `${locked ? "Lock" : "Unlock"} ${selected.length} elements`);
  },
  chooseSymbol: (selectedSymbolKey) => set({ selectedSymbolKey, tool: "symbol", selectedElementIds: [] }),
  setLineVariety: (lineVariety) => set({ lineVariety, tool: "line", selectedElementIds: [] }),
  setRectangleVariety: (rectangleVariety) => set({ rectangleVariety, tool: "rectangle", selectedElementIds: [] }),
  setCircleVariety: (circleVariety) => set({ circleVariety, tool: "circle", selectedElementIds: [] }),

  transact: async (operations, label) => {
    const document = get().document;
    if (!document) return;
    const lockedTargets = directLockedOperationTargets(document.elements, operations);
    if (lockedTargets.length) {
      const error = new Error(`元素已锁定，事务已取消：${lockedTargets.join(", ")}`);
      set({ error: error.message });
      throw error;
    }
    const origin: WorkspaceMutationOrigin = {
      documentId: document.id,
      revision: document.revision,
      documentGeneration: documentRequestGeneration,
      mutationGeneration: ++mutationRequestGeneration,
    };
    set({ isMutating: true, error: null, syncState: "checking", syncMessage: "正在提交修改…" });
    try {
      const result = await api.transact(document.id, document.revision, operations, label);
      const documents = await api.listDocuments();
      if (!mutationResponseCanApply(
        origin,
        get().document,
        documentRequestGeneration,
        mutationRequestGeneration,
        result.document,
      )) return;
      const existing = new Set(result.document.elements.map((item) => item.id));
      set({
        document: result.document,
        documents,
        selectedElementIds: get().selectedElementIds.filter((id) => existing.has(id)),
        isMutating: false,
        error: null,
        syncState: "synced",
        syncMessage: `已同步至 r${result.document.revision}`,
        pendingExternalRevision: null,
      });
    } catch (error) {
      if (!mutationOriginCanApply(
        origin,
        get().document,
        documentRequestGeneration,
        mutationRequestGeneration,
      )) return;
      const latest = await api.getDocument(document.id).catch(() => null);
      if (!mutationOriginCanApply(
        origin,
        get().document,
        documentRequestGeneration,
        mutationRequestGeneration,
      )) return;
      set({
        error: messageFromError(error),
        document: latest ?? get().document,
        isMutating: false,
        syncState: latest ? "updated" : "error",
        syncMessage: latest ? `已载入服务器最新 r${latest.revision}` : "提交失败",
        pendingExternalRevision: null,
      });
      throw error;
    }
  },

  duplicateSelection: async () => {
    const document = get().document;
    const selectedIds = get().selectedElementIds;
    if (!document || selectedIds.length === 0) return;
    const selected = document.elements.filter((element) => selectedIds.includes(element.id));
    const idMap = new Map(selected.map((element) => [element.id, newElementId()]));
    const sourceGroups = normalizedGroupMembers(selected);
    const groupMap = new Map([...sourceGroups.keys()].map((groupId) => [groupId, newGroupId()]));
    const offset = document.canvas.grid_size;
    const copies = selected
      .map((element) => {
        const copy = duplicateElement(element, idMap, offset);
        const groupId = readEditorGroupId(element);
        copy.metadata = metadataWithGroup(copy, groupId ? groupMap.get(groupId) ?? null : null);
        return copy;
      })
      .sort((left, right) => Number(left.type === "connector") - Number(right.type === "connector"));
    await get().transact(
      copies.map((element) => ({ op: "add_element", element }) as Operation),
      `Duplicate ${copies.length} element(s)`,
    );
    set({ selectedElementIds: copies.map((element) => element.id) });
  },

  deleteSelection: async () => {
    const ids = get().selectedElementIds;
    if (ids.length === 0) return;
    await get().transact(
      ids.map((element_id) => ({ op: "delete_element", element_id } as Operation)),
      `Delete ${ids.length} element(s)`,
    );
    set({ selectedElementIds: [] });
  },

  undo: async () => {
    const document = get().document;
    if (!document) return;
    const origin: WorkspaceMutationOrigin = {
      documentId: document.id,
      revision: document.revision,
      documentGeneration: documentRequestGeneration,
      mutationGeneration: ++mutationRequestGeneration,
    };
    set({ isMutating: true, syncState: "checking", syncMessage: "正在撤销…" });
    try {
      const updated = await api.undo(document.id, document.revision);
      if (!mutationResponseCanApply(
        origin,
        get().document,
        documentRequestGeneration,
        mutationRequestGeneration,
        updated,
      )) return;
      set({
        document: updated,
        selectedElementIds: [],
        isMutating: false,
        syncState: "synced",
        syncMessage: `已同步至 r${updated.revision}`,
        pendingExternalRevision: null,
      });
    } catch (error) {
      if (!mutationOriginCanApply(
        origin,
        get().document,
        documentRequestGeneration,
        mutationRequestGeneration,
      )) return;
      set({
        error: messageFromError(error),
        isMutating: false,
        syncState: "error",
        syncMessage: "撤销失败",
      });
    }
  },

  redo: async () => {
    const document = get().document;
    if (!document) return;
    const origin: WorkspaceMutationOrigin = {
      documentId: document.id,
      revision: document.revision,
      documentGeneration: documentRequestGeneration,
      mutationGeneration: ++mutationRequestGeneration,
    };
    set({ isMutating: true, syncState: "checking", syncMessage: "正在重做…" });
    try {
      const updated = await api.redo(document.id, document.revision);
      if (!mutationResponseCanApply(
        origin,
        get().document,
        documentRequestGeneration,
        mutationRequestGeneration,
        updated,
      )) return;
      set({
        document: updated,
        selectedElementIds: [],
        isMutating: false,
        syncState: "synced",
        syncMessage: `已同步至 r${updated.revision}`,
        pendingExternalRevision: null,
      });
    } catch (error) {
      if (!mutationOriginCanApply(
        origin,
        get().document,
        documentRequestGeneration,
        mutationRequestGeneration,
      )) return;
      set({
        error: messageFromError(error),
        isMutating: false,
        syncState: "error",
        syncMessage: "重做失败",
      });
    }
  },

  checkForExternalUpdates: async (applyChanges = true) => {
    const document = get().document;
    if (!document || get().loading || get().isMutating || get().syncState === "checking") return;
    set({ syncState: "checking", syncMessage: "正在检查外部修改…" });
    try {
      const status = await api.getDocumentStatus(document.id);
      const current = get().document;
      if (!current || current.id !== document.id) {
        set({ syncState: "idle", syncMessage: "文档已切换" });
        return;
      }
      if (status.revision <= current.revision) {
        set({ syncState: "synced", syncMessage: `已同步至 r${current.revision}` });
        return;
      }
      if (!applyChanges) {
        set({
          syncState: "pending",
          syncMessage: `检测到外部更新 r${status.revision}`,
          pendingExternalRevision: status.revision,
        });
        return;
      }
      await get().refreshDocument();
    } catch {
      set({ syncState: "error", syncMessage: "自动同步检查失败" });
    }
  },

  refreshDocument: async () => {
    const current = get().document;
    if (!current) return;
    const requestGeneration = documentRequestGeneration;
    set({ syncState: "checking", syncMessage: "正在载入外部修改…" });
    try {
      const latest = await api.getDocument(current.id);
      if (
        requestGeneration !== documentRequestGeneration
        || get().document?.id !== current.id
      ) return;
      const documents = await api.listDocuments();
      if (
        requestGeneration !== documentRequestGeneration
        || get().document?.id !== current.id
      ) return;
      const existing = new Set(latest.elements.map((item) => item.id));
      set({
        document: latest,
        documents,
        selectedElementIds: get().selectedElementIds.filter((id) => existing.has(id)),
        syncState: latest.revision > current.revision ? "updated" : "synced",
        syncMessage: latest.revision > current.revision
          ? `已载入外部更新 r${latest.revision}`
          : `已同步至 r${latest.revision}`,
        pendingExternalRevision: null,
      });
    } catch (error) {
      if (
        requestGeneration !== documentRequestGeneration
        || get().document?.id !== current.id
      ) return;
      set({
        syncState: "error",
        syncMessage: "外部修改载入失败",
        error: messageFromError(error),
      });
    }
  },

  generate: async (prompt, context, provider) => {
    const document = get().document;
    if (!document) throw new Error("No document is open");
    set({
      loading: true,
      isMutating: true,
      error: null,
      syncState: "checking",
      syncMessage: "模型正在规划并校验事务…",
    });
    try {
      const result = await api.generate(
        document.id,
        document.revision,
        prompt,
        context,
        provider,
      );
      set({
        document: result.document,
        documents: await api.listDocuments(),
        selectedElementIds: [],
        loading: false,
        isMutating: false,
        syncState: "synced",
        syncMessage: `已同步至 r${result.document.revision}`,
        pendingExternalRevision: null,
      });
      return result.plan.explanation;
    } catch (error) {
      set({
        error: messageFromError(error),
        loading: false,
        isMutating: false,
        syncState: "error",
        syncMessage: "Agent 请求失败，文档未写入",
      });
      throw error;
    }
  },
}));
