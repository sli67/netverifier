import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { api, ApiError, type Investigation, type ParsedFile } from "../api";
import { Chat } from "../Chat";
import { DeleteInvestigationButton } from "../DeleteInvestigationButton";
import { formatDateTime, formatDuration } from "../format";
import { InvestigateButton } from "../InvestigateButton";
import { RunPanel } from "../RunPanel";
import { StatusBadge } from "../StatusBadge";

const POLL_MS = 1500;
const SLOW_QUEUE_MS = 8000;
const MAX_INTENT_LENGTH = 4000;
// Longer issue lists start collapsed so they do not bury the rest of the page.
const ISSUES_OPEN_LIMIT = 5;

type Load = { status: "loading" } | { status: "error"; notFound: boolean } | { status: "ready"; data: Investigation };

const FILE_STATUS: Record<string, { text: string; tone: string }> = {
  PASSED: { text: "Parsed", tone: "ok" },
  PARTIALLY_UNRECOGNIZED: { text: "Partly recognised", tone: "warn" },
  UNKNOWN: { text: "Unknown format", tone: "bad" },
  FAILED: { text: "Failed to parse", tone: "bad" },
  EMPTY: { text: "Empty", tone: "bad" },
  UNSUPPORTED: { text: "Unsupported", tone: "bad" },
  WILL_NOT_COMMIT: { text: "Would not commit", tone: "bad" },
  ORPHANED: { text: "Orphaned", tone: "warn" },
};

function formatName(format: string): string {
  return format === "UNKNOWN" ? "Unknown" : format.replace(/_/g, " ");
}

/** Loads the investigation and keeps refreshing it until it reaches a final state. */
function useInvestigation(id: string): Load & { retry: () => void } {
  const [load, setLoad] = useState<Load>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let current = true;
    let timer: number | undefined;
    setLoad({ status: "loading" });

    async function refresh() {
      try {
        const { investigation } = await api<{ investigation: Investigation }>(`/investigations/${id}`);
        if (!current) return;
        setLoad({ status: "ready", data: investigation });
        if (investigation.status === "queued" || investigation.status === "parsing") {
          timer = window.setTimeout(() => void refresh(), POLL_MS);
        }
      } catch (err) {
        if (!current) return;
        const notFound = err instanceof ApiError && err.status === 404;
        // A failed refresh keeps showing the last good data and tries again.
        setLoad((prev) => (prev.status === "ready" && !notFound ? prev : { status: "error", notFound }));
        if (!notFound) timer = window.setTimeout(() => void refresh(), POLL_MS * 2);
      }
    }
    void refresh();

    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [id, attempt]);

  return { ...load, retry: () => setAttempt((n) => n + 1) };
}

export function InvestigationDetail() {
  const navigate = useNavigate();
  const { id = "" } = useParams();
  const load = useInvestigation(id);
  // Counts runs started from this page, so the run panel knows to look again.
  const [runsStarted, setRunsStarted] = useState(0);
  // The intent a run started from the chat saved, so the intent card shows it.
  const [chatIntent, setChatIntent] = useState<SavedIntent | null>(null);
  // A run picked in the chat's trace, for the run panel to show.
  const [focusRun, setFocusRun] = useState<{ id: string; n: number } | null>(null);

  if (load.status === "loading") return <p className="muted">Loading…</p>;

  if (load.status === "error") {
    return (
      <div className="card">
        <h2>{load.notFound ? "Investigation not found" : "Couldn't load this investigation"}</h2>
        <p className="muted">
          {load.notFound ? "The link may be wrong." : "Check that the API is running. Retrying automatically."}
        </p>
        <div className="row">
          <Link className="button secondary" to="/">
            Back to snapshots
          </Link>
        </div>
      </div>
    );
  }

  const inv = load.data;
  const files = inv.result?.files ?? [];
  const issues = inv.result?.issues ?? [];

  return (
    <>
      <div className="page-head">
        <div>
          <p className="small">
            <Link to="/">Snapshots</Link> / <Link to={`/snapshots/${inv.snapshot.id}`}>{inv.snapshot.name}</Link>
          </p>
          <h1>{inv.snapshot.name}</h1>
          <p className="muted page-description">Explore your network with the investigation agent.</p>
        </div>
        <div className="row">
          <DeleteInvestigationButton investigation={inv} onDeleted={() => navigate(`/snapshots/${inv.snapshot.id}`)} />
          {(inv.status === "ready" || inv.status === "failed") && (
            <InvestigateButton snapshotId={inv.snapshot.id} snapshotName={inv.snapshot.name} secondary fresh />
          )}
        </div>
      </div>

      <Banner inv={inv} files={files} />

      <RunPanel key={`runs-${inv.id}`} investigationId={inv.id} refresh={runsStarted} focus={focusRun} />

      <div className="investigation-layout">
        <div className="conversation-column">
          {inv.status === "ready" ? (
            <Chat
              key={`chat-${inv.id}`}
              investigationId={inv.id}
              onRunStarted={(run) => {
                setRunsStarted((n) => n + 1);
                setChatIntent({ intent: run.intent, at: run.createdAt, by: run.createdBy.email });
              }}
              onOpenRun={(id) => setFocusRun((f) => ({ id, n: (f?.n ?? 0) + 1 }))}
            />
          ) : (
            <section className="card agent-waiting">
              <h2>{inv.status === "failed" ? "The model couldn't be prepared" : "Preparing your workspace"}</h2>
              <p className="muted">{inv.status === "failed" ? "Review the error above and start another investigation to try again." : "The agent will be available here when the configurations have been parsed. You can add your change intent while you wait."}</p>
            </section>
          )}
        </div>
        <aside className="investigation-context" aria-label="Investigation context">
          <div className="context-title">INVESTIGATION CONTEXT</div>
          <IntentCard
            key={inv.id}
            investigation={inv}
            replaced={chatIntent}
            onRunStarted={() => setRunsStarted((n) => n + 1)}
          />

      {inv.finishedAt && inv.batfishVersion && (
        <dl className="card meta">
          <div>
            <dt>Parsed model</dt>
            <dd>{inv.modelReused ? "Reused from an earlier run" : "Newly parsed"}</dd>
          </div>
          <div>
            <dt>Time in Batfish</dt>
            <dd>{inv.parseMs === null ? "Unknown" : formatDuration(inv.parseMs)}</dd>
          </div>
          <div>
            <dt>Batfish</dt>
            <dd>
              {inv.batfishVersion} on {inv.batfishInstance}
            </dd>
          </div>
          <div>
            <dt>Started</dt>
            <dd>
              {formatDateTime(inv.createdAt)} by {inv.createdBy.email}
            </dd>
          </div>
        </dl>
      )}

        </aside>
      </div>

      {files.length > 0 && (
        <section className="card table-wrap" aria-label="Files">
          <h2>Configuration files <span className="badge">{files.length}</span></h2>
          <table>
            <thead>
              <tr>
                <th>File</th>
                <th>Device</th>
                <th>Format</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => {
                const status = FILE_STATUS[f.status] ?? { text: f.status, tone: "warn" };
                return (
                  <tr key={f.path}>
                    <td>
                      <Link className="mono wrap" to={`/snapshots/${inv.snapshot.id}?file=${encodeURIComponent(f.path)}`}>
                        {f.path}
                      </Link>
                    </td>
                    <td>{f.nodes.length > 0 ? f.nodes.join(", ") : <span className="muted">None</span>}</td>
                    <td>{formatName(f.format)}</td>
                    <td>
                      <span className={`status ${status.tone}`}>{status.text}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      )}

      {issues.length > 0 && (
        <details className="card collapsible" open={issues.length <= ISSUES_OPEN_LIMIT}>
          <summary>
            <h2>Issues reported by Batfish</h2>
            <span className="muted">{issues.length}</span>
          </summary>
          <ul className="issues">
            {issues.map((issue, i) => (
              <li key={i}>
                <strong>
                  {issue.type}: {issue.details}
                </strong>
                <span className="muted small mono wrap">
                  {issue.sources
                    .map((s) => (s.lines.length > 0 ? `${s.path} line ${s.lines.join(", ")}` : s.path))
                    .join("; ")}
                </span>
                {issue.lineText && <pre>{issue.lineText}</pre>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </>
  );
}

/** Lets the user describe the change they want. Saved on the investigation. */
interface SavedIntent {
  intent: string;
  at: string | null;
  by: string | null;
}

/** `replaced` is an intent saved elsewhere on the page, which this card then shows. */
function IntentCard({
  investigation,
  replaced,
  onRunStarted,
}: {
  investigation: Investigation;
  replaced: SavedIntent | null;
  onRunStarted: () => void;
}) {
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  async function start(mode: "verify" | "change") {
    setStarting(true);
    setStartError(null);
    try {
      await api("/runs", { method: "POST", body: { investigationId: investigation.id, mode } });
      onRunStarted();
    } catch (err) {
      setStartError(
        err instanceof ApiError && err.code === "run_active"
          ? "A run is already under way on this investigation. Wait for it to finish, or cancel it."
          : "Couldn't start. Try again.",
      );
    } finally {
      setStarting(false);
    }
  }

  const [saved, setSaved] = useState<SavedIntent>({
    intent: investigation.intent ?? "",
    at: investigation.intentUpdatedAt,
    by: investigation.intentUpdatedBy,
  });
  const [draft, setDraft] = useState(saved.intent);
  useEffect(() => {
    if (!replaced) return;
    setSaved(replaced);
    setDraft(replaced.intent);
  }, [replaced]);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const dirty = draft.trim() !== saved.intent;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setFailed(false);
    try {
      const { investigation: updated } = await api<{ investigation: Investigation }>(
        `/investigations/${investigation.id}/intent`,
        { method: "PUT", body: { intent: draft } },
      );
      const next = { intent: updated.intent ?? "", at: updated.intentUpdatedAt, by: updated.intentUpdatedBy };
      setSaved(next);
      setDraft(next.intent);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={onSubmit}>
      <div className="field">
        <label htmlFor="intent">
          <span className="field-heading">Change intent</span>
        </label>
        <p className="muted small">Give the agent context: what should change, and what must stay the same?</p>
        <textarea
          id="intent"
          rows={4}
          maxLength={MAX_INTENT_LENGTH}
          placeholder="Allow 192.0.2.0/24 to be exported to ISP-A, but keep blocking all other customer prefixes."
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
      </div>
      <div className="row">
        <button type="submit" disabled={!dirty || busy}>
          {busy ? "Saving…" : "Save intent"}
        </button>
        {dirty && !busy && saved.intent !== "" && (
          <button type="button" className="secondary" onClick={() => setDraft(saved.intent)}>
            Discard changes
          </button>
        )}
        {failed && (
          <span className="error" role="alert">
            Couldn't save. Your text is still here; try again.
          </span>
        )}
        {!failed && !dirty && saved.at && (
          <span className="muted small" role="status">
            {saved.intent === "" ? "Cleared" : "Saved"} {formatDateTime(saved.at)}
            {saved.by ? ` by ${saved.by}` : ""}
          </span>
        )}
      </div>
      <div className="run-start">
        <p className="muted small">
          {investigation.status !== "ready"
            ? "Once the configurations are parsed, you can check this intent or have a change proposed."
            : saved.intent === "" || dirty
              ? "Save an intent to check it against the configs or have a change proposed."
              : "Check whether the configs meet this intent, or have a change proposed and validated on Batfish."}
        </p>
        <div className="row">
          {(["verify", "change"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              className="secondary"
              disabled={starting || dirty || saved.intent === "" || investigation.status !== "ready"}
              onClick={() => void start(mode)}
            >
              {mode === "verify" ? "Check this" : "Propose a change"}
            </button>
          ))}
        </div>
        {startError && (
          <p className="error" role="alert">
            {startError}
          </p>
        )}
        <AuditStart investigation={investigation} onRunStarted={onRunStarted} />
      </div>
    </form>
  );
}

/** Starts an audit of one named route-map, policy, list or ACL: no intent needed, it is inferred and then confirmed. */
function AuditStart({ investigation, onRunStarted }: { investigation: Investigation; onRunStarted: () => void }) {
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function audit() {
    setBusy(true);
    setError(null);
    try {
      await api("/runs", { method: "POST", body: { investigationId: investigation.id, mode: "audit", target } });
      setTarget("");
      onRunStarted();
    } catch (err) {
      setError(
        err instanceof ApiError && err.code === "run_active"
          ? "A run is already under way on this investigation. Wait for it to finish, or cancel it."
          : "Couldn't start the audit. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="field">
      <label htmlFor="audit-target" className="small">
        Or audit one route-map, policy, list or ACL by name
      </label>
      <div className="row">
        <input
          id="audit-target"
          value={target}
          maxLength={200}
          placeholder="ISP-A-OUT"
          onChange={(e) => setTarget(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              if (target.trim()) void audit();
            }
          }}
        />
        <button
          type="button"
          className="secondary"
          disabled={busy || target.trim() === "" || investigation.status !== "ready"}
          onClick={() => void audit()}
        >
          {busy ? "Starting…" : "Audit"}
        </button>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function Banner({ inv, files }: { inv: Investigation; files: ParsedFile[] }) {
  const slow = useSlowQueue(inv);
  const clean = files.filter((f) => f.status === "PASSED").length;
  const usable = files.filter((f) => f.nodes.length > 0).length;

  let detail: string;
  if (inv.status === "queued") {
    detail = slow
      ? "Still waiting for a worker. Check that one is running (pnpm worker)."
      : "Waiting for a worker to pick this up.";
  } else if (inv.status === "parsing") {
    detail = "Batfish is loading the configurations.";
  } else if (inv.status === "failed") {
    detail = inv.error ?? "The investigation failed.";
  } else if (clean === files.length) {
    detail = `Batfish parsed all ${files.length} ${files.length === 1 ? "file" : "files"} cleanly.`;
  } else {
    detail = `Batfish built a model from ${usable} of ${files.length} files; ${files.length - clean} need attention.`;
  }
  const dataplane = inv.status === "ready" ? inv.result?.dataplane : undefined;

  return (
    <div className="card banner" role="status">
      <div className="headline">
        <StatusBadge status={inv.status} />
        <span>{detail}</span>
      </div>
      {dataplane && !dataplane.ok && (
        <p className="small">
          <strong>Routing could not be computed.</strong> Batfish failed while simulating this network, so questions
          about routes, BGP sessions and reachability cannot be answered here. Policy and ACL analysis still works.
          <span className="muted"> Batfish reported: {dataplane.error}</span>
        </p>
      )}
    </div>
  );
}

/** True once an investigation has sat in the queue long enough to suggest no worker is running. */
function useSlowQueue(inv: Investigation): boolean {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (inv.status !== "queued") {
      setSlow(false);
      return;
    }
    const waited = Date.now() - new Date(inv.createdAt).getTime();
    const timer = window.setTimeout(() => setSlow(true), Math.max(0, SLOW_QUEUE_MS - waited));
    return () => window.clearTimeout(timer);
  }, [inv.status, inv.createdAt]);
  return slow;
}
