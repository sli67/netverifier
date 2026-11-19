import { useState } from "react";
import { Icon } from "../Icon";
import { Link } from "react-router";
import { api, type SnapshotSummary } from "../api";
import { DeleteSnapshotButton } from "../DeleteSnapshotButton";
import { formatBytes, formatDateTime } from "../format";
import { InvestigateButton } from "../InvestigateButton";
import { useAsync } from "../useAsync";

export function SnapshotList() {
  const [query, setQuery] = useState("");
  const result = useAsync(() => api<{ snapshots: SnapshotSummary[] }>("/snapshots"), []);

  const snapshots = result.status === "ready" ? result.data.snapshots : [];
  const filtered = snapshots.filter((s) => s.name.toLowerCase().includes(query.trim().toLowerCase()));

  return (
    <>
      <div className="page-head">
        <div><p className="eyebrow">NETWORK WORKSPACE</p><h1>Your snapshots</h1><p className="muted page-description">A clear view of your configurations. A conversation away from answers.</p></div>
        <Link className="button" to="/snapshots/new">
          <Icon name="plus" size={17} /> Upload snapshot
        </Link>
      </div>

      {result.status === "ready" && <div className="snapshot-overview">
        <div><span className="overview-icon"><Icon name="layers" /></span><div><strong>{snapshots.length}</strong><span>Snapshots in your workspace</span></div></div>
        <div><span className="overview-icon"><Icon name="file" /></span><div><strong>{snapshots.reduce((total, s) => total + s.fileCount, 0)}</strong><span>Configuration files</span></div></div>
        <div className="overview-note"><Icon name="spark" size={22} /><p>Choose a snapshot and select <strong>Investigate</strong> to start a conversation with your agent.</p></div>
      </div>}

      {result.status === "loading" && <p className="muted">Loading…</p>}

      {result.status === "error" && (
        <div className="card">
          <h2>Couldn't load snapshots</h2>
          <p className="muted">Check that the API is running, then try again.</p>
          <div>
            <button onClick={result.reload}>Try again</button>
          </div>
        </div>
      )}

      {result.status === "ready" && result.data.snapshots.length === 0 && (
        <div className="card empty">
          <span className="empty-icon"><Icon name="layers" size={30} /></span><h2>Your first investigation starts here</h2>
          <p className="muted">Upload your router configurations, then ask the agent to help you understand them.</p><div><Link className="button" to="/snapshots/new">Upload your first snapshot</Link></div>
        </div>
      )}

      {result.status === "ready" && result.data.snapshots.length > 0 && (
        <div className="card snapshot-table">
          <div className="snapshot-table-head"><h2>All snapshots <span className="badge">{snapshots.length}</span></h2><label className="search-field"><Icon name="search" size={17} /><input type="search" aria-label="Search snapshots" placeholder="Search snapshots…" value={query} onChange={(e) => setQuery(e.target.value)} /></label></div>
          <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th className="num">Files</th>
                <th className="num">Size</th>
                <th>Uploaded by</th>
                <th>Uploaded</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {filtered.map((s) => (
                <tr key={s.id}>
                  <td>
                    <Link className="snapshot-name" to={`/snapshots/${s.id}`}><Icon name="layers" size={18} />{s.name}</Link>
                  </td>
                  <td className="num">{s.fileCount}</td>
                  <td className="num">{formatBytes(s.totalBytes)}</td>
                  <td>{s.createdBy.email}</td>
                  <td>{formatDateTime(s.createdAt)}</td>
                  <td>
                    <div className="row end nowrap">
                      <InvestigateButton snapshotId={s.id} snapshotName={s.name} resume={s.resumeInvestigationId !== null} secondary />
                      <DeleteSnapshotButton snapshot={s} onDeleted={result.reload} />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {filtered.length === 0 && <p className="empty muted">No snapshots match “{query}”.</p>}
          </div>
          <div className="table-footer">{filtered.length} of {snapshots.length} snapshots</div>
        </div>
      )}
    </>
  );
}
