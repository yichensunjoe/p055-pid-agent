import { useCallback, useEffect, useState } from "react";

type CableListEntry = { document_id: string; revision: number; name: string };
type CableReadiness = {
  state: "eligible" | "not_eligible";
  counts: Record<string, number>;
  reasons: string[];
  result_hash: string;
  profile_id: string;
  profile_version: number;
  profile_fingerprint: string;
};
type CableDetail = {
  document_id: string;
  revision: number;
  schema: string;
  name: string;
  segments: Array<{ id: string; from_node: string; to_node: string; gauge: string }>;
  readiness: CableReadiness;
};

async function cableFetch<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as {
      detail?: { code?: string; message?: string } | string;
    };
    const detail = body.detail;
    const message =
      typeof detail === "string" ? detail : (detail?.message ?? `HTTP ${response.status}`);
    throw new Error(message);
  }
  return (await response.json()) as T;
}

export function CablePanel() {
  const [documents, setDocuments] = useState<CableListEntry[]>([]);
  const [detail, setDetail] = useState<CableDetail | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    const listing = await cableFetch<{ documents: CableListEntry[] }>(
      "/api/v2/cable/documents",
    );
    setDocuments(listing.documents);
  }, []);

  useEffect(() => {
    refresh().catch((exc) => setError(String(exc)));
  }, [refresh]);

  const open = async (documentId: string) => {
    setError("");
    try {
      setDetail(await cableFetch<CableDetail>(`/api/v2/cable/documents/${documentId}`));
    } catch (exc) {
      setError(String(exc));
    }
  };

  return (
    <div className="cable-panel" data-testid="cable-panel">
      {error ? (
        <p className="review-error" role="alert">
          {error}
        </p>
      ) : null}
      <section className="cable-list">
        <h3>线缆文档</h3>
        {documents.length === 0 ? <p className="review-empty">暂无线缆文档。</p> : null}
        {documents.map((entry) => (
          <button
            key={entry.document_id}
            type="button"
            className="cable-list-item"
            onClick={() => open(entry.document_id)}
          >
            {entry.name}（r{entry.revision}）
          </button>
        ))}
      </section>
      {detail ? (
        <section className="cable-detail" data-testid="cable-detail">
          <header>
            <strong>{detail.name}</strong>
            <span
              className={`review-badge review-badge--${
                detail.readiness.state === "eligible" ? "released" : "stale"
              }`}
            >
              {detail.readiness.state}
            </span>
          </header>
          <p className="review-meta">
            {detail.document_id} · schema {detail.schema} · profile {detail.readiness.profile_id}/
            {detail.readiness.profile_version}
          </p>
          <table className="cable-segments">
            <thead>
              <tr>
                <th>段 ID</th>
                <th>起点</th>
                <th>终点</th>
                <th>线规</th>
              </tr>
            </thead>
            <tbody>
              {detail.segments.map((segment) => (
                <tr key={segment.id}>
                  <td>{segment.id}</td>
                  <td>{segment.from_node}</td>
                  <td>{segment.to_node}</td>
                  <td>{segment.gauge}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {detail.segments.length === 0 ? (
            <p className="review-empty">文档暂无线段。</p>
          ) : null}
          <div className="review-actions">
            <a
              className="review-download"
              href={`/api/v2/cable/documents/${detail.document_id}/export.zip?expected_revision=${detail.revision}`}
              download
            >
              导出确定性包
            </a>
          </div>
        </section>
      ) : null}
    </div>
  );
}
