import { useCallback, useEffect, useState } from "react";

import {
  downloadProjectPackage,
  fetchProjectLinks,
  fetchProjectReadiness,
  fetchProjectSummary,
  type ProjectReadiness,
} from "../api";
import "./ProjectPanel.css";

// The M12 migration seeds exactly one default project; the inspection surface
// is scoped to it (multi-project UI is future scope).
const DEFAULT_PROJECT_ID = "proj_m12default";

function defaultAsOf(): string {
  return new Date().toISOString();
}

export function ProjectPanel() {
  const [summary, setSummary] = useState<Awaited<ReturnType<typeof fetchProjectSummary>> | null>(null);
  const [links, setLinks] = useState<Awaited<ReturnType<typeof fetchProjectLinks>> | null>(null);
  const [readiness, setReadiness] = useState<ProjectReadiness | null>(null);
  const [asOf, setAsOf] = useState(defaultAsOf);
  const [error, setError] = useState("");

  const downloadPackage = useCallback(async () => {
    setError("");
    try {
      await downloadProjectPackage(DEFAULT_PROJECT_ID, asOf);
    } catch (downloadError) {
      setError(downloadError instanceof Error ? downloadError.message : String(downloadError));
    }
  }, [asOf]);

  const refresh = useCallback(async () => {
    setError("");
    try {
      const [nextSummary, nextLinks, nextReadiness] = await Promise.all([
        fetchProjectSummary(DEFAULT_PROJECT_ID),
        fetchProjectLinks(DEFAULT_PROJECT_ID),
        fetchProjectReadiness(DEFAULT_PROJECT_ID, asOf),
      ]);
      setSummary(nextSummary);
      setLinks(nextLinks);
      setReadiness(nextReadiness);
    } catch (refreshError) {
      setError(refreshError instanceof Error ? refreshError.message : String(refreshError));
    }
  }, [asOf]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="project-panel" data-testid="project-panel">
      <header className="project-panel-header">
        <h2>项目图谱（只读）</h2>
        <label>
          evaluation_as_of
          <input
            data-testid="project-as-of"
            value={asOf}
            onChange={(event) => setAsOf(event.target.value)}
          />
        </label>
        <button type="button" onClick={() => void refresh()}>重新评估</button>
        <button type="button" onClick={() => void downloadPackage()}>
          下载项目交付包
        </button>
      </header>
      {error ? <p className="project-panel-error" role="alert">{error}</p> : null}
      {readiness ? (
        <section className="project-readiness" data-testid="project-readiness">
          <strong>project readiness: {readiness.state}</strong>
          <span>result_hash {readiness.result_hash.slice(0, 16)}…</span>
          <span>
            profile {readiness.profile_id} v{readiness.profile_version}
          </span>
          {readiness.issues.length ? (
            <ul>
              {readiness.issues.map((issue) => (
                <li key={`${issue.link_id}-${issue.code}`}>
                  [{issue.severity}] {issue.code} · {issue.link_id}
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}
      {summary ? (
        <section>
          <h3>成员文档</h3>
          <table className="project-table">
            <thead>
              <tr><th>document_id</th><th>domain</th><th>revision</th></tr>
            </thead>
            <tbody>
              {summary.members.map((member) => (
                <tr key={member.document_id}>
                  <td>{member.document_id}</td>
                  <td>{member.domain}</td>
                  <td>{member.revision ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}
      {links ? (
        <section>
          <h3>跨域工程链接（active）</h3>
          {links.links.length === 0 ? <p>无 active link。</p> : (
            <table className="project-table">
              <thead>
                <tr>
                  <th>link_id</th><th>关系</th><th>source</th><th>端点</th>
                  <th>target</th><th>pins</th>
                </tr>
              </thead>
              <tbody>
                {links.links.map((link) => (
                  <tr key={link.link_id}>
                    <td>{link.link_id}</td>
                    <td>{link.relation_type}</td>
                    <td>{link.source_document_id} · {link.source_object_ref}</td>
                    <td>{link.source_endpoint}</td>
                    <td>{link.target_document_id} · {link.target_object_ref}</td>
                    <td>
                      r{link.pinned_source_revision} / r{link.pinned_target_revision}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      ) : null}
    </div>
  );
}
