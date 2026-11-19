import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { api, ApiError, apiText, type InvestigationSummary, type Snapshot } from "../api";
import { CodePreview } from "../CodePreview";
import { DeleteInvestigationButton } from "../DeleteInvestigationButton";
import { DeleteSnapshotButton } from "../DeleteSnapshotButton";
import { InvestigateButton } from "../InvestigateButton";
import { StatusBadge } from "../StatusBadge";
import { formatBytes, formatDateTime } from "../format";
import { useAsync } from "../useAsync";

export function SnapshotDetail() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const result = useAsync(() => api<{ snapshot: Snapshot }>(`/snapshots/${id}`), [id]);

  if (result.status === "loading") return <p className="muted">Loading…</p>;

  if (result.status === "error") {
    const notFound = result.error instanceof ApiError && result.error.status === 404;
    return (
      <div className="card">
        <h2>{notFound ? "Snapshot not found" : "Couldn't load this snapshot"}</h2>
        <p className="muted">
          {notFound ? "It may have been removed, or the link is wrong." : "Check that the API is running, then try again."}
        </p>
        <div className="row">
          {!notFound && <button onClick={result.reload}>Try again</button>}
          <Link className="button secondary" to="/">
            Back to snapshots
          </Link>
        </div>
      </div>
    );
  }

  const { snapshot } = result.data;
  const selected = snapshot.files.find((f) => f.path === params.get("file")) ?? snapshot.files[0];

  return (
    <>
      <div className="page-head">
        <div>
          <p className="small">
            <Link to="/">Snapshots</Link>
          </p>
          <h1>{snapshot.name}</h1>
        </div>
        <div className="row">
          <DeleteSnapshotButton snapshot={snapshot} onDeleted={() => navigate("/")} />
          <InvestigateButton snapshotId={snapshot.id} snapshotName={snapshot.name} resume={snapshot.resumeInvestigationId !== null} />
        </div>
      </div>

      <dl className="card meta">
        <div>
          <dt>Uploaded by</dt>
          <dd>{snapshot.createdBy.email}</dd>
        </div>
        <div>
          <dt>Uploaded</dt>
          <dd>{formatDateTime(snapshot.createdAt)}</dd>
        </div>
        <div>
          <dt>Contents</dt>
          <dd>
            {snapshot.fileCount} {snapshot.fileCount === 1 ? "file" : "files"}, {formatBytes(snapshot.totalBytes)}
          </dd>
        </div>
        <div className="wide">
          <dt>Content hash (SHA-256)</dt>
          <dd className="mono wrap">{snapshot.contentHash}</dd>
        </div>
      </dl>

      <InvestigationHistory snapshotId={snapshot.id} snapshotName={snapshot.name} onChanged={result.reload} />

      <div className="viewer">
        <nav className="card file-list" aria-label="Files">
          {snapshot.files.map((f) => (
            <button
              key={f.path}
              type="button"
              className={f.path === selected?.path ? "file active" : "file"}
              aria-current={f.path === selected?.path ? "true" : undefined}
              onClick={() => setParams({ file: f.path }, { replace: true })}
            >
              <span className="mono wrap">{f.path}</span>
              <span className="muted small">{formatBytes(f.size)}</span>
            </button>
          ))}
        </nav>
        {selected && <FileContent key={`${snapshot.id}/${selected.path}`} snapshotId={snapshot.id} path={selected.path} />}
      </div>
    </>
  );
}

function InvestigationHistory({
  snapshotId,
  snapshotName,
  onChanged,
}: {
  snapshotId: string;
  snapshotName: string;
  onChanged: () => void;
}) {
  const result = useAsync(
    () => api<{ investigations: InvestigationSummary[] }>(`/investigations?snapshotId=${snapshotId}`),
    [snapshotId],
  );
  // The page works without this list, so a failure to load it is not shown as an error.
  if (result.status !== "ready" || result.data.investigations.length === 0) return null;

  return (
    <section className="card table-wrap" aria-label="Investigations">
      <div className="row between">
        <h2>Investigations</h2>
        <InvestigateButton snapshotId={snapshotId} snapshotName={snapshotName} secondary fresh />
      </div>
      <table>
        <thead>
          <tr>
            <th>Status</th>
            <th>Intent</th>
            <th className="num">Messages</th>
            <th>Last activity</th>
            <th>Started by</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {result.data.investigations.map((i) => (
            <tr key={i.id}>
              <td>
                <StatusBadge status={i.status} />
              </td>
              <td className="intent-cell">{i.intent ?? <span className="muted">None yet</span>}</td>
              <td className="num">{i.messageCount}</td>
              <td>{formatDateTime(i.lastActivityAt)}</td>
              <td>{i.createdBy.email}</td>
              <td className="num">
                <div className="row end nowrap">
                  <Link to={`/investigations/${i.id}`}>Open</Link>
                  <DeleteInvestigationButton investigation={i} onDeleted={() => { result.reload(); onChanged(); }} />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function FileContent({ snapshotId, path }: { snapshotId: string; path: string }) {
  const result = useAsync(
    () => apiText(`/snapshots/${snapshotId}/file?path=${encodeURIComponent(path)}`),
    [snapshotId, path],
  );

  return (
    <section className="card file-content" aria-label={path}>
      <h2 className="mono wrap">{path}</h2>
      {result.status === "loading" && <p className="muted">Loading…</p>}
      {result.status === "error" && (
        <>
          <p className="error">Couldn't load this file. It may be missing or changed in storage.</p>
          <div>
            <button onClick={result.reload}>Try again</button>
          </div>
        </>
      )}
      {result.status === "ready" && <CodePreview>{result.data}</CodePreview>}
    </section>
  );
}
