import { useCallback, useEffect, useState } from "react";

import {
  downloadCableExport,
  fetchCableDetail,
  fetchCableDocuments,
  type CableDetail,
} from "../api";

import "./CablePanel.css";

export function CablePanel() {
  const [documents, setDocuments] = useState<
    Awaited<ReturnType<typeof fetchCableDocuments>>
  >([]);
  const [detail, setDetail] = useState<CableDetail | null>(null);
  const [error, setError] = useState("");
  const [downloading, setDownloading] = useState(false);

  const refresh = useCallback(async () => {
    setDocuments(await fetchCableDocuments());
  }, []);

  useEffect(() => {
    refresh().catch((exc) => setError(String(exc)));
  }, [refresh]);

  const open = async (documentId: string) => {
    setError("");
    try {
      setDetail(await fetchCableDetail(documentId));
    } catch (exc) {
      setError(String(exc));
    }
  };

  const exportDocument = async () => {
    if (!detail) return;
    setDownloading(true);
    setError("");
    try {
      await downloadCableExport(detail.document_id, detail.revision);
    } catch (exc) {
      setError(String(exc));
    } finally {
      setDownloading(false);
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
            {entry.name}（r{entry.revision} · {entry.readiness_state}）
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
            <button type="button" disabled={downloading} onClick={() => exportDocument()}>
              导出确定性包
            </button>
          </div>
        </section>
      ) : null}
    </div>
  );
}
