import { useEffect, useRef, useState } from "react";
import { api, ApiError, type ProviderConfig } from "../api";
import { useWorkspace } from "../store";
import type { AgentImagePayload } from "./visionImageTypes";
import type { SemanticAgentPlanResult, SemanticOperation } from "../types";
import {
  automaticAgentApplyResponseError,
  automaticAgentRunContextError,
  type AutomaticAgentRunOrigin,
} from "./automaticAgentRunGuard";
import { AgentStreamingViewer } from "./AgentStreamingViewer";

const MAX_REPLANS = 5;
const HIGH_RISK_OPERATIONS = new Set(["delete_element", "delete_layer", "delete_system", "clear_document"]);

type TraceEntry = {
  attempt: number;
  planId: string;
  valid: boolean;
  issueCodes: string[];
};

type Props = {
  prompt: string;
  context: string;
  provider: ProviderConfig;
  images?: AgentImagePayload[];
  requireVisibleOutput?: boolean;
  disabled?: boolean;
  onApplied?: () => void;
  onRunningChange?: (running: boolean) => void;
};

type PendingApproval = {
  result: SemanticAgentPlanResult;
  origin: AutomaticAgentRunOrigin;
};

function issueSignature(result: SemanticAgentPlanResult): string {
  return result.assessment.issues
    .map((issue) => `${issue.code}:${issue.field_path}`)
    .sort()
    .join("|");
}

function containsHighRiskOperation(operations: SemanticOperation[]): boolean {
  return operations.some((operation) => HIGH_RISK_OPERATIONS.has(operation.op));
}

function traceEntry(result: SemanticAgentPlanResult): TraceEntry {
  return {
    attempt: result.attempt,
    planId: result.plan.plan_id,
    valid: result.assessment.valid,
    issueCodes: result.assessment.issues.map((issue) => issue.code),
  };
}

export function AutomaticAgentRunner({
  prompt,
  context,
  provider,
  images = [],
  requireVisibleOutput = false,
  disabled,
  onApplied,
  onRunningChange,
}: Props) {
  const document = useWorkspace((state) => state.document);
  const [running, setRunning] = useState(false);
  const [phase, setPhase] = useState("");
  const [message, setMessage] = useState("");
  const [trace, setTrace] = useState<TraceEntry[]>([]);
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  const [streamingThinking, setStreamingThinking] = useState("");
  const [streamingContent, setStreamingContent] = useState("");
  const cancelRequested = useRef(false);
  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    onRunningChange?.(false);
  }, [onRunningChange]);

  useEffect(() => {
    if (!pendingApproval) return;
    const contextError = automaticAgentRunContextError(
      pendingApproval.origin,
      document,
      pendingApproval.result,
      true,
    );
    if (!contextError) return;
    setPendingApproval(null);
    setMessage(`应用失败：${contextError}`);
  }, [document?.id, document?.revision, pendingApproval]);

  const updateRunning = (next: boolean) => {
    setRunning(next);
    onRunningChange?.(next);
  };

  const stopRun = () => {
    cancelRequested.current = true;
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setPhase("");
    setMessage("已手动停止自动执行");
    updateRunning(false);
  };

  const assertRunContext = (
    origin: AutomaticAgentRunOrigin,
    result: SemanticAgentPlanResult,
    requireCompiledRevision = false,
  ) => {
    const contextError = automaticAgentRunContextError(
      origin,
      useWorkspace.getState().document,
      result,
      requireCompiledRevision,
    );
    if (contextError) throw new Error(contextError);
  };

  const applyResult = async (
    result: SemanticAgentPlanResult,
    origin: AutomaticAgentRunOrigin,
  ) => {
    const current = useWorkspace.getState().document;
    const compiled = result.compiled_plan;
    if (!current || !compiled || !result.assessment.valid) {
      throw new Error("自动执行没有得到可应用的有效事务");
    }
    assertRunContext(origin, result, true);
    setPhase("正在应用有效事务…");
    const sessionId = result.session_id
      ?? (await api.createAgentSession(origin.documentId, {
        actor: "web-user",
        provider: provider.base_url ?? "",
        model: provider.model ?? "",
        metadata: { surface: "web", workflow: "automatic-agent-confirmed-apply" },
      })).id;
    const requestedApproval = await api.requestToolApproval(
      sessionId,
      "apply_compiled_agent_transaction",
      origin.documentId,
      { transaction: compiled.transaction },
      "自动规划已完成，等待用户显式确认后应用。",
    );
    const approval = await api.resolveToolApproval(
      requestedApproval.id,
      true,
      "用户点击确认按钮批准本次工程变更。",
    );
    const applied = await api.applySemanticAgentPlan(
      origin.documentId,
      sessionId,
      approval.id,
      result.plan.plan_id,
      result.parent_plan_id,
      result.attempt,
      compiled.transaction,
    );
    const documents = await api.listDocuments();
    const afterApply = useWorkspace.getState().document;
    const responseError = automaticAgentApplyResponseError(
      origin,
      afterApply,
      applied.document,
      documents,
    );
    if (responseError) {
      const originalStillExists = documents.some(
        (item) => item.id === origin.documentId,
      );
      if (!originalStillExists && afterApply?.id === origin.documentId) {
        useWorkspace.setState({
          documents,
          document: null,
          selectedElementIds: [],
          error: null,
          syncState: "synced",
          syncMessage: "原文档已删除",
          pendingExternalRevision: null,
        });
      } else {
        useWorkspace.setState({ documents });
      }
      setPendingApproval(null);
      setMessage(`应用完成但未合并到当前画布：${responseError}`);
      setPhase("");
      onApplied?.();
      return;
    }
    const existing = new Set(applied.document.elements.map((element) => element.id));
    useWorkspace.setState({
      document: applied.document,
      documents,
      selectedElementIds: result.assessment.affected_element_ids.filter((id) => existing.has(id)),
      error: null,
      syncState: "synced",
      syncMessage: `已同步至 r${applied.document.revision}`,
      pendingExternalRevision: null,
    });
    setPendingApproval(null);
    setMessage(`生成成功，已自动应用到 revision ${applied.document.revision}`);
    setPhase("");
    onApplied?.();
  };

  const run = async () => {
    const initial = useWorkspace.getState().document;
    if (!initial || !prompt.trim() || running) return;
    const origin: AutomaticAgentRunOrigin = {
      documentId: initial.id,
      revision: initial.revision,
    };
    cancelRequested.current = false;
    updateRunning(true);
    setPendingApproval(null);
    setMessage("");
    setTrace([]);
    setStreamingThinking("");
    setStreamingContent("");
    const seenFailures = new Set<string>();
    try {
      setPhase("正在规划并编译…");
      const firstController = new AbortController();
      abortControllerRef.current = firstController;
      let result = await api.planSemanticAgentStream(
        initial.id,
        initial.revision,
        prompt.trim(),
        context,
        {
          onThinking: (delta) => setStreamingThinking((prev) => prev + delta),
          onContent: (delta) => setStreamingContent((prev) => prev + delta),
        },
        provider,
        images,
        requireVisibleOutput,
        firstController.signal,
      );
      assertRunContext(origin, result);
      const entries: TraceEntry[] = [traceEntry(result)];
      setTrace(entries);

      while (!result.assessment.valid) {
        assertRunContext(origin, result);
        if (cancelRequested.current) throw new Error("自动执行已停止");
        const signature = issueSignature(result);
        if (signature && seenFailures.has(signature)) {
          throw new Error(`检测到重复失败循环：${signature}`);
        }
        if (signature) seenFailures.add(signature);
        if (result.attempt >= MAX_REPLANS) {
          throw new Error(`达到最大重规划次数 ${MAX_REPLANS}`);
        }
        const nextAttempt = result.attempt + 1;
        setPhase(`正在按结构化错误自动重规划（${nextAttempt}/${MAX_REPLANS}）…`);
        const replanController = new AbortController();
        abortControllerRef.current = replanController;
        result = await api.replanSemanticAgent(
          origin.documentId,
          origin.revision,
          prompt.trim(),
          context,
          result.session_id,
          result.plan,
          nextAttempt,
          provider,
          images,
          requireVisibleOutput,
          replanController.signal,
        );
        assertRunContext(origin, result);
        entries.push(traceEntry(result));
        setTrace([...entries]);
      }

      assertRunContext(origin, result, true);
      if (cancelRequested.current) throw new Error("自动执行已停止");
      if (!result.compiled_plan) throw new Error("有效计划缺少编译事务");
      setPendingApproval({ result, origin });
      setMessage(
        containsHighRiskOperation(result.plan.transaction.operations)
          ? "计划已通过校验，包含删除或清空操作；Harness 要求人工确认后应用"
          : "计划已通过校验；工程变更需要人工确认后应用",
      );
      setPhase("");
      return;
    } catch (error) {
      if (cancelRequested.current) {
        setMessage("自动执行已手动停止。");
      } else {
        const text = error instanceof ApiError ? error.message : String(error instanceof Error ? error.message : error);
        setMessage(`生成失败：${text}`);
      }
      setPhase("");
    } finally {
      abortControllerRef.current = null;
      updateRunning(false);
    }
  };

  const confirmHighRisk = async () => {
    if (!pendingApproval || running) return;
    updateRunning(true);
    setMessage("");
    try {
      await applyResult(pendingApproval.result, pendingApproval.origin);
    } catch (error) {
      const text = error instanceof ApiError ? error.message : String(error instanceof Error ? error.message : error);
      setMessage(`应用失败：${text}`);
    } finally {
      updateRunning(false);
    }
  };

  return (
    <section className="automatic-agent-runner">
      <div className="automatic-agent-heading">
        <div><strong>自动完成</strong><span>自动规划、修复并应用安全事务</span></div>
        {running ? (
          <button
            type="button"
            className="danger"
            onClick={stopRun}
          >🛑 停止自动执行</button>
        ) : (
          <button
            type="button"
            className="primary"
            disabled={disabled || !prompt.trim() || !document}
            onClick={() => void run()}
          >自动生成并应用</button>
        )}
      </div>
      {running ? (
        <div className="automatic-agent-progress">
          <span>{phase}</span>
          <button type="button" className="danger-inline" onClick={stopRun}>🛑 叫停</button>
        </div>
      ) : null}
      <AgentStreamingViewer
        thinking={streamingThinking}
        content={streamingContent}
        isStreaming={running}
        onStop={stopRun}
      />
      {message ? <div className={`automatic-agent-result ${message.startsWith("生成成功") ? "success" : message.includes("需要确认") ? "warning" : "error"}`}>{message}</div> : null}
      {pendingApproval ? <div className="automatic-agent-approval"><button type="button" className="confirm" disabled={running} onClick={() => void confirmHighRisk()}>确认并批准工程变更</button><button type="button" disabled={running} onClick={() => setPendingApproval(null)}>放弃</button></div> : null}
      {trace.length ? <details className="automatic-agent-trace"><summary>执行轨迹 · {trace.length} 次规划</summary><ol>{trace.map((entry) => <li key={entry.planId}><code>attempt {entry.attempt}</code><span>{entry.valid ? "通过" : entry.issueCodes.join(", ") || "未通过"}</span></li>)}</ol></details> : null}
      <p className="group-hint">相同结构化错误再次出现时会提前停止，避免模型在两个错误之间循环。所有工程变更均需通过 Harness Approval Gate；删除、清空等高风险操作会额外标记风险。</p>
    </section>
  );
}
