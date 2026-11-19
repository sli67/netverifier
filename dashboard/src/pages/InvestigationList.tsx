import { useState } from "react";
import { Link } from "react-router";
import { api, type InvestigationSummary } from "../api";
import { useAuth } from "../auth";
import { DeleteInvestigationButton } from "../DeleteInvestigationButton";
import { formatDateTime } from "../format";
import { Icon } from "../Icon";
import { StatusBadge } from "../StatusBadge";
import { useAsync } from "../useAsync";

/** Every past investigation, most recently active first. */
export function InvestigationList() {
  const { state } = useAuth();
  const userId = state.status === "signed-in" ? state.user.id : null;
  const [onlyMine, setOnlyMine] = useState(true);
  const result = useAsync(() => api<{ investigations: InvestigationSummary[] }>("/investigations"), []);

  const all = result.status === "ready" ? result.data.investigations : [];
  const shown = all
    .filter((i) => !onlyMine || i.createdBy.id === userId)
    .sort((a, b) => b.lastActivityAt.localeCompare(a.lastActivityAt));

  return (
    <>
      <div className="page-head">
        <div>
          <p className="eyebrow">NETWORK WORKSPACE</p>
          <h1>Investigations</h1>
          <p className="muted page-description">Pick up a past conversation with the agent where you left it.</p>
        </div>
      </div>

      {result.status === "loading" && <p className="muted">Loading…</p>}

      {result.status === "error" && (
        <div className="card">
          <h2>Couldn't load investigations</h2>
          <p className="muted">Check that the API is running, then try again.</p>
          <div>
            <button onClick={result.reload}>Try again</button>
          </div>
        </div>
      )}

      {result.status === "ready" && (
        <div className="card snapshot-table">
          <div className="snapshot-table-head">
            <h2>
              {onlyMine ? "Your investigations" : "All investigations"} <span className="badge">{shown.length}</span>
            </h2>
            <label className="filter-check">
              <input type="checkbox" checked={onlyMine} onChange={(e) => setOnlyMine(e.target.checked)} />
              Only mine
            </label>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Snapshot</th>
                  <th>Intent</th>
                  <th>Status</th>
                  <th className="num">Messages</th>
                  <th>Last activity</th>
                  {!onlyMine && <th>Started by</th>}
                  <th />
                </tr>
              </thead>
              <tbody>
                {shown.map((i) => (
                  <tr key={i.id}>
                    <td>
                      <Link className="snapshot-name" to={`/snapshots/${i.snapshot.id}`}>
                        <Icon name="layers" size={18} />
                        {i.snapshot.name}
                      </Link>
                    </td>
                    <td className="intent-cell">{i.intent ?? <span className="muted">None yet</span>}</td>
                    <td>
                      <StatusBadge status={i.status} />
                    </td>
                    <td className="num">{i.messageCount}</td>
                    <td>{formatDateTime(i.lastActivityAt)}</td>
                    {!onlyMine && <td>{i.createdBy.email}</td>}
                    <td className="num">
                      <div className="row end nowrap">
                        <Link to={`/investigations/${i.id}`}>Open</Link>
                        <DeleteInvestigationButton investigation={i} onDeleted={result.reload} />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {shown.length === 0 && (
              <p className="empty muted">
                {onlyMine ? "You haven't started an investigation yet. " : "No investigations yet. "}
                <Link to="/">Choose a snapshot</Link> and select Investigate.
              </p>
            )}
          </div>
        </div>
      )}
    </>
  );
}
