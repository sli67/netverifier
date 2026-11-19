import { useEffect, useState, type FormEvent } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, ApiError, type CheckResult, type Deployment, type LabPhase, type Run, type RunAttempt, type RunSummary, type SideEffect } from "./api";
import { useAuth } from "./auth";
import { formatDateTime, progressText } from "./format";

const POLL_ACTIVE_MS = 1500;
// A run waiting for a person changes only when someone acts, possibly someone else.
const POLL_WAITING_MS = 5000;
// Times the checks can be sent back to be rewritten; matches MAX_CHECK_REVISIONS in the worker.
const MAX_CHECK_REVISIONS = 3;

type Mode = Run["mode"];
const ALL: Mode[] = ["verify", "change", "audit"];

const STEPS: { key: string; label: string; working: string; modes: Mode[] }[] = [
  { key: "clarify", label: "Clarify", working: "Turning the intent into requirements", modes: ["verify", "change"] },
  { key: "infer", label: "Infer purpose", working: "Working out what it is meant to do", modes: ["audit"] },
  { key: "investigate", label: "Investigate", working: "Reading configs and querying Batfish", modes: ["verify", "change"] },
  { key: "plan_checks", label: "Plan checks", working: "Writing Batfish checks for the requirements", modes: ALL },
  { key: "baseline", label: "Baseline", working: "Running the checks on the current configs", modes: ALL },
  { key: "review_checks", label: "Confirm checks", working: "Taking your review of the checks", modes: ["change", "audit"] },
  { key: "propose", label: "Propose", working: "Drafting a configuration change", modes: ["change"] },
  { key: "validate", label: "Validate", working: "Checking the candidate on Batfish", modes: ["change"] },
  { key: "report", label: "Report", working: "Summing up", modes: ALL },
  { key: "approve", label: "Approve", working: "Waiting for an approver", modes: ["change"] },
];

const TITLES: Record<Mode, string> = { verify: "Check", change: "Change proposal", audit: "Audit" };

const STATUS: Record<Run["status"], { text: string; tone: string }> = {
  queued: { text: "Queued", tone: "pending" },
  running: { text: "Running", tone: "pending" },
  waiting_user: { text: "Needs your answer", tone: "warn" },
  waiting_approval: { text: "Awaiting approval", tone: "warn" },
  done: { text: "Finished", tone: "ok" },
  failed: { text: "Failed", tone: "bad" },
  cancelled: { text: "Cancelled", tone: "bad" },
};

const OUTCOME_TONE: Record<string, string> = {
  holds: "ok",
  approved: "ok",
  no_change_needed: "ok",
  violated: "bad",
  not_validated: "bad",
  rejected: "bad",
  inconclusive: "warn",
};

const RESULT: Record<string, { text: string; tone: string }> = {
  pass: { text: "Pass", tone: "ok" },
  fail: { text: "Fail", tone: "bad" },
  error: { text: "Undecided", tone: "warn" },
  review: { text: "Review", tone: "warn" },
};

const OUTCOME_TEXT: Record<string, string> = {
  holds: "Holds",
  violated: "Does not hold",
  inconclusive: "Inconclusive",
  no_change_needed: "No change needed",
  not_validated: "No change passed",
  approved: "Approved",
  rejected: "Rejected",
};

/** A short label for a run's status, using its outcome once it has finished. */
export function runLabel(status: Run["status"], outcome: string | null): { text: string; tone: string } {
  if (status === "done" && outcome) return { text: OUTCOME_TEXT[outcome] ?? outcome, tone: OUTCOME_TONE[outcome] ?? "warn" };
  if (status === "waiting_user") return { text: "Waiting for you", tone: "warn" };
  return STATUS[status];
}

const isActive = (status: Run["status"]) => status === "queued" || status === "running";
const isWaiting = (status: Run["status"]) => status === "waiting_user" || status === "waiting_approval";

/**
 * The staged workflow runs of one investigation: the latest by default, with
 * its steps, check results, proposed change and approval.
 *
 * `refresh` changes when a run was just started elsewhere on the page.
 */
export function RunPanel({
  investigationId,
  refresh,
  focus,
}: {
  investigationId: string;
  refresh: number;
  /** A run to show, chosen elsewhere on the page; n changes on every request. */
  focus: { id: string; n: number } | null;
}) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [tick, setTick] = useState(0);
  // Bumped when a run is started from inside the panel (a fix proposed after an audit).
  const [listTick, setListTick] = useState(0);

  // The list of runs. A newly started run becomes the one shown.
  useEffect(() => {
    let current = true;
    void api<{ runs: RunSummary[] }>(`/runs?investigationId=${investigationId}`)
      .then(({ runs }) => {
        if (!current) return;
        setRuns(runs);
        setSelected(runs[0]?.id ?? null);
      })
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, [investigationId, refresh, listTick]);

  // A run picked from the chat: show it and bring the panel into view.
  useEffect(() => {
    if (!focus) return;
    setSelected(focus.id);
    document.querySelector(".run-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [focus]);

  // The selected run, refreshed while it can still change.
  useEffect(() => {
    if (!selected) {
      setRun(null);
      return;
    }
    let current = true;
    let timer: number | undefined;
    async function load() {
      let next = POLL_WAITING_MS;
      try {
        const { run } = await api<{ run: Run }>(`/runs/${selected}`);
        if (!current) return;
        setRun(run);
        if (isActive(run.status) || deploying(run)) next = POLL_ACTIVE_MS;
        else if (!isWaiting(run.status)) return;
      } catch (err) {
        if (!current || (err instanceof ApiError && err.status === 404)) return;
      }
      timer = window.setTimeout(() => void load(), next);
    }
    void load();
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [selected, tick]);

  if (!run) return null;
  const reload = () => setTick((n) => n + 1);
  const state = run.state;
  const attempts = state.attempts ?? [];
  const validated = attempts.at(-1)?.passed ? attempts.at(-1)! : null;
  const reviewing = run.status === "waiting_user" && run.stage === "review_checks";
  const status = reviewing ? { text: "Confirm the checks", tone: "warn" } : STATUS[run.status];

  return (
    <section className="card run-panel" aria-label="Workflow run">
      <div className="row between">
        <div className="row">
          <h2>
            {TITLES[run.mode]}
            {run.target ? ` of ${run.target}` : ""}
          </h2>
          <span className={`status ${status.tone}`}>{status.text}</span>
          <span className="muted small">
            Started {formatDateTime(run.createdAt)} by {run.createdBy.email}
            {run.messageId && (
              <>
                {" from a "}
                <button type="button" className="link-button" onClick={() => showMessage(run.messageId!)}>
                  chat message
                </button>
              </>
            )}
          </span>
        </div>
        <div className="row">
          {runs.length > 1 && (
            <select aria-label="Choose a run" value={run.id} onChange={(e) => setSelected(e.target.value)}>
              {runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {formatDateTime(r.createdAt)} · {r.mode === "verify" ? "Check" : "Change"}
                  {r.messageId ? " · from chat" : ""}
                </option>
              ))}
            </select>
          )}
          {(isActive(run.status) || isWaiting(run.status)) && <CancelButton run={run} onDone={reload} />}
        </div>
      </div>

      <Timeline run={run} />

      {run.status === "queued" && <p className="muted">Waiting for a worker to pick this up.</p>}
      {run.error && (
        <p className="error" role="alert">
          {run.error}
        </p>
      )}
      {state.headline && !isActive(run.status) && (
        <p className={`run-headline ${OUTCOME_TONE[run.outcome ?? ""] ?? "warn"}`} role="status">
          {state.headline}
        </p>
      )}

      {run.status === "waiting_user" && !reviewing && state.pending_question && (
        <AnswerForm run={run} question={state.pending_question} onDone={reload} />
      )}

      <details className="run-section">
        <summary>Intent this run used</summary>
        <p className="plain">{run.intent}</p>
      </details>

      {state.purpose && (
        <div className="run-section">
          <h3>What {run.target} appears meant to do</h3>
          <p>{state.purpose}</p>
        </div>
      )}

      {run.mode === "audit" && run.status === "done" && run.outcome === "violated" && (
        <ProposeFix run={run} onStarted={() => setListTick((n) => n + 1)} />
      )}

      {(state.code_findings?.length ?? 0) > 0 && (
        <div className="run-section">
          <h3>Found by Batfish directly</h3>
          <ul className="run-checks">
            {state.code_findings!.map((f, i) => (
              <li key={i}>
                <span className="status bad">Problem</span>
                <div>
                  <p>{f.description}</p>
                  <p className="muted small wrap">{f.evidence}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {state.requirements && (
        <div className="run-section">
          <h3>{run.mode === "audit" ? "Inferred requirements" : "Requirements"}</h3>
          <ul className="run-list">
            {state.requirements.map((r) => (
              <li key={r.id}>
                <strong>{r.id}</strong> {r.text}
              </li>
            ))}
          </ul>
          {(state.assumptions?.length ?? 0) > 0 && (
            <p className="muted small">Assumed without asking: {state.assumptions!.join(" ")}</p>
          )}
          {(state.qa?.length ?? 0) > 0 && (
            <ul className="run-list muted small">
              {state.qa!.map((e, i) => (
                <li key={i}>
                  Asked: {e.question} Answered: {e.answer}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {state.baseline && (
        <div className="run-section">
          <h3>Checks on the current configs</h3>
          {reviewing ? (
            <ReviewChecks run={run} results={state.baseline} onDone={reload} />
          ) : (
            <Checks results={state.baseline} />
          )}
          {state.checks_confirmed_by && (
            <p className="muted small">Checks confirmed by {state.checks_confirmed_by} before any change was proposed.</p>
          )}
          {(state.dropped_checks?.length ?? 0) > 0 && (
            <>
              <p className="small">
                <strong>Removed by the reviewer.</strong> These checks were judged wrong and are not part of the verdict:
              </p>
              <ul className="run-list small">
                {state.dropped_checks!.map((c, i) => (
                  <li key={i}>
                    <strong>{c.requirement}</strong> {c.description}
                  </li>
                ))}
              </ul>
            </>
          )}
          {(state.check_notes?.length ?? 0) > 0 && (
            <ul className="run-list muted small">
              {state.check_notes!.map((n, i) => (
                <li key={i}>
                  Checks rewritten at {n.by ?? "the reviewer"}'s request: {n.note}
                </li>
              ))}
            </ul>
          )}
          {(state.uncheckable?.length ?? 0) > 0 && (
            <>
              <p className="small">
                <strong>Not checked.</strong> Batfish could not decide these, so they are not part of the verdict:
              </p>
              <ul className="run-list small">
                {state.uncheckable!.map((u, i) => (
                  <li key={i}>
                    <strong>{u.requirement}</strong> {u.reason}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {attempts.map((attempt) => (
        <Attempt key={attempt.round} attempt={attempt} open={attempt === attempts.at(-1)} />
      ))}

      {run.status === "waiting_approval" && validated?.content_hash && (
        <Decision run={run} candidateHash={validated.content_hash} onDone={reload} />
      )}
      {run.approval && (
        <p className="small">
          <strong>{run.approval.decision === "approved" ? "Approved" : "Rejected"}</strong> by {run.approval.decidedBy}{" "}
          on {formatDateTime(run.approval.decidedAt)}, for candidate{" "}
          <span className="mono">{run.approval.candidateHash.slice(0, 12)}</span>.
          {run.approval.comment ? ` "${run.approval.comment}"` : ""}
        </p>
      )}
      {run.approval?.decision === "approved" && <Deployments run={run} onDone={reload} />}

      {state.findings && (
        // With nothing Batfish could check, the findings are the run's answer, so they start open.
        <details className="run-section" open={run.status === "done" && (state.checks?.length ?? 0) === 0}>
          <summary>Investigation findings{state.steps ? ` (${state.steps.length} tool calls)` : ""}</summary>
          <div className="markdown">
            <Markdown remarkPlugins={[remarkGfm]}>{state.findings}</Markdown>
          </div>
        </details>
      )}
    </section>
  );
}

/** Scrolls the conversation to a message and marks it for a moment. */
function showMessage(id: string) {
  const element = document.getElementById(`message-${id}`);
  if (!element) return;
  element.scrollIntoView({ behavior: "smooth", block: "center" });
  element.classList.add("highlight");
  window.setTimeout(() => element.classList.remove("highlight"), 2000);
}

function Timeline({ run }: { run: Run }) {
  const steps = STEPS.filter((s) => s.modes.includes(run.mode));
  // The approve step does not report progress of its own: it is where the run waits.
  const stage = run.status === "waiting_approval" || run.approval ? "approve" : run.stage;
  const at = steps.findIndex((s) => s.key === stage);
  const ended = run.status === "done" || run.status === "failed" || run.status === "cancelled";
  const working = isActive(run.status) && at >= 0 ? steps[at]!.working : null;
  return (
    <>
      <ol className="run-steps" aria-label="Steps">
        {steps.map((step, i) => {
          // A run that ended early (for example nothing to change) never reached the later steps.
          const state = i < at || (i === at && ended && run.status === "done") ? "done" : i === at ? "current" : "todo";
          return (
            <li key={step.key} className={state} aria-current={state === "current" ? "step" : undefined}>
              {step.label}
            </li>
          );
        })}
      </ol>
      {working && (
        <p className="muted small" role="status">
          {working}…
          {/* Only the investigate step runs the agent loop, so only it has finer progress. */}
          {stage === "investigate" && run.progress ? ` ${progressText(run.progress)}` : ""}
        </p>
      )}
    </>
  );
}

function Checks({ results }: { results: CheckResult[] }) {
  if (results.length === 0) return <p className="muted small">No check could be written for these requirements.</p>;
  return (
    <ul className="run-checks">
      {results.map((r) => (
        <li key={r.index}>
          <span className={`status ${RESULT[r.status]!.tone}`}>{RESULT[r.status]!.text}</span>
          <div>
            <p>
              <strong>{r.requirement}</strong> {r.description}
            </p>
            <p className="muted small wrap">{r.evidence}</p>
          </div>
        </li>
      ))}
    </ul>
  );
}

/**
 * The checks a change run will be held to, shown before anything is proposed.
 * A wrong check leads to a wrong change that still passes validation, so the
 * run's starter confirms them here: untick the wrong ones, or say what is wrong
 * and have them rewritten.
 */
function ReviewChecks({ run, results, onDone }: { run: Run; results: CheckResult[]; onDone: () => void }) {
  const { state: auth } = useAuth();
  const user = auth.status === "signed-in" ? auth.user : null;
  const [dropped, setDropped] = useState<Set<number>>(new Set());
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canReview = user && (user.id === run.createdBy.id || user.role === "admin");
  if (!canReview) {
    return (
      <>
        <Checks results={results} />
        <p className="run-question">Waiting for {run.createdBy.email} to confirm these checks.</p>
      </>
    );
  }
  const revisionsLeft = MAX_CHECK_REVISIONS - (run.state.check_notes?.length ?? 0);
  const keptFailing = results.some((r) => r.status === "fail" && !dropped.has(r.index));

  async function send(body: { drop: number[] } | { note: string }) {
    setBusy(true);
    setError(null);
    try {
      await api(`/runs/${run.id}/checks`, { method: "POST", body });
      onDone();
    } catch {
      setBusy(false);
      setError("Couldn't send your review. Try again.");
    }
  }

  function toggle(index: number) {
    setDropped((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  return (
    <div className="run-question">
      {run.mode === "audit" ? (
        <>
          <strong>Is this what {run.target} is meant to do?</strong>
          <p className="small">
            These checks come from what it appears meant to do, so they are a guess until you confirm them. Untick any
            that are wrong, or say what is wrong and they are rewritten.
          </p>
        </>
      ) : (
        <>
          <strong>Confirm these checks before a change is proposed</strong>
          <p className="small">
            The proposed change will be made to pass exactly these checks. Untick any that do not match what you meant.
          </p>
        </>
      )}
      <ul className="run-checks selectable">
        {results.map((r) => (
          <li key={r.index}>
            <input
              type="checkbox"
              aria-label={`Keep check: ${r.description}`}
              checked={!dropped.has(r.index)}
              onChange={() => toggle(r.index)}
              disabled={busy}
            />
            <span className={`status ${RESULT[r.status]!.tone}`}>{RESULT[r.status]!.text}</span>
            <div>
              <p>
                <strong>{r.requirement}</strong> {r.description}
              </p>
              <p className="muted small wrap">{r.evidence}</p>
            </div>
          </li>
        ))}
      </ul>
      {revisionsLeft > 0 && (
        <>
          <label htmlFor="run-check-note" className="small">
            Or say what is wrong, and the checks are rewritten ({revisionsLeft} left)
          </label>
          <textarea
            id="run-check-note"
            rows={2}
            maxLength={4000}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="For example: R4 should only cover routes tagged with the customer community."
          />
        </>
      )}
      <div className="row">
        <button type="button" disabled={busy} onClick={() => void send({ drop: [...dropped] })}>
          {run.mode === "audit" ? "Confirm and report" : keptFailing ? "Propose a change with these checks" : "Finish without a change"}
        </button>
        {revisionsLeft > 0 && (
          <button
            type="button"
            className="secondary"
            disabled={busy || note.trim() === ""}
            onClick={() => void send({ note })}
          >
            Rewrite the checks
          </button>
        )}
        {error && (
          <span className="error" role="alert">
            {error}
          </span>
        )}
      </div>
    </div>
  );
}

function Effects({ effects }: { effects: SideEffect[] }) {
  return (
    <ul className="run-checks">
      {effects.map((e) => (
        <li key={e.name}>
          <span className={`status ${RESULT[e.status]!.tone}`}>{RESULT[e.status]!.text}</span>
          <div>
            <p>
              <strong>{e.name}</strong> {e.summary}
            </p>
            {e.items.map((item, i) => (
              <p key={i} className="muted small wrap">
                {item}
              </p>
            ))}
          </div>
        </li>
      ))}
    </ul>
  );
}

function Attempt({ attempt, open }: { attempt: RunAttempt; open: boolean }) {
  return (
    <details className="run-section run-attempt" open={open}>
      <summary>
        Proposed change, attempt {attempt.round}{" "}
        <span className={`status ${attempt.passed ? "ok" : "bad"}`}>
          {attempt.passed ? "Passed validation" : "Did not pass"}
        </span>
      </summary>
      {attempt.summary && <p>{attempt.summary}</p>}
      {attempt.diff && <Diff text={attempt.diff} />}
      {attempt.content_hash && (
        <p className="muted small">
          Candidate content hash <span className="mono wrap">{attempt.content_hash}</span>
        </p>
      )}
      {attempt.validation && (
        <>
          <h3>Checks on the candidate</h3>
          <Checks results={attempt.validation} />
        </>
      )}
      {attempt.side_effects && (
        <>
          <h3>What else changes</h3>
          <Effects effects={attempt.side_effects} />
        </>
      )}
      {!attempt.validation && attempt.problem && <p className="error">{attempt.problem}</p>}
    </details>
  );
}

function Diff({ text }: { text: string }) {
  return (
    <pre className="diff" aria-label="Configuration diff">
      {text.split("\n").map((line, i) => {
        const kind =
          line.startsWith("+++") || line.startsWith("---")
            ? "diff-file"
            : line.startsWith("@@")
              ? "hunk"
              : line.startsWith("+")
                ? "add"
                : line.startsWith("-")
                  ? "del"
                  : "";
        return (
          <span key={i} className={kind}>
            {line + "\n"}
          </span>
        );
      })}
    </pre>
  );
}

function AnswerForm({ run, question, onDone }: { run: Run; question: string; onDone: () => void }) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(`/runs/${run.id}/answer`, { method: "POST", body: { answer } });
      onDone();
    } catch (err) {
      setBusy(false);
      setError(
        err instanceof ApiError && err.status === 403
          ? "Only the person who started this run can answer."
          : "Couldn't send your answer. Try again.",
      );
    }
  }

  return (
    <form className="run-question" onSubmit={onSubmit}>
      <label htmlFor="run-answer">
        <strong>The workflow needs an answer before it can continue</strong>
      </label>
      <p>{question}</p>
      <textarea id="run-answer" rows={2} maxLength={4000} value={answer} onChange={(e) => setAnswer(e.target.value)} />
      <div className="row">
        <button type="submit" disabled={busy || answer.trim() === ""}>
          {busy ? "Sending…" : "Answer and continue"}
        </button>
        {error && (
          <span className="error" role="alert">
            {error}
          </span>
        )}
      </div>
    </form>
  );
}

function Decision({ run, candidateHash, onDone }: { run: Run; candidateHash: string; onDone: () => void }) {
  const { state } = useAuth();
  const user = state.status === "signed-in" ? state.user : null;
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canReview = user?.role === "admin" || (user?.role === "approver" && user.id !== run.createdBy.id);
  if (!canReview) {
    return (
      <p className="run-question">
        {user?.role === "approver"
          ? "This is your own run. Another approver or an admin needs to review it."
          : "Waiting for an approver or admin to review this change."}
      </p>
    );
  }

  async function decide(decision: "approved" | "rejected") {
    setBusy(true);
    setError(null);
    try {
      await api(`/runs/${run.id}/decision`, { method: "POST", body: { decision, candidateHash, comment } });
      onDone();
    } catch (err) {
      setBusy(false);
      setError(
        err instanceof ApiError && err.code === "candidate_mismatch"
          ? "The candidate changed since this page loaded. Reload and review it again."
          : "Couldn't record your decision. Try again.",
      );
    }
  }

  return (
    <div className="run-question">
      <strong>Review this change</strong>
      <p className="small">
        Your decision applies to exactly the candidate shown above, with content hash{" "}
        <span className="mono">{candidateHash.slice(0, 12)}</span>. Nothing is deployed by approving.
      </p>
      <label htmlFor="run-comment" className="small">
        Comment (optional)
      </label>
      <textarea id="run-comment" rows={2} maxLength={2000} value={comment} onChange={(e) => setComment(e.target.value)} />
      <div className="row">
        <button type="button" disabled={busy} onClick={() => void decide("approved")}>
          Approve
        </button>
        <button type="button" className="secondary danger" disabled={busy} onClick={() => void decide("rejected")}>
          Reject
        </button>
        {error && (
          <span className="error" role="alert">
            {error}
          </span>
        )}
      </div>
    </div>
  );
}

const DEPLOY_STATUS: Record<Deployment["status"], { text: string; tone: string }> = {
  queued: { text: "Queued", tone: "pending" },
  deploying: { text: "Deploying", tone: "pending" },
  verifying: { text: "Verifying", tone: "pending" },
  completed: { text: "Verified in the lab", tone: "ok" },
  rolled_back: { text: "Rolled back", tone: "bad" },
  failed: { text: "Failed", tone: "bad" },
};

const deploymentActive = (d: Deployment) => d.status === "queued" || d.status === "deploying" || d.status === "verifying";
const deploying = (run: Run) => (run.deployments ?? []).some(deploymentActive);

/** What changed on each device between the live lab before and after: routes and sessions gained or lost. */
function liveChanges(before: LabPhase["live"], after: LabPhase["live"]): string[] {
  const lines: string[] = [];
  for (const node of [...new Set([...Object.keys(before), ...Object.keys(after)])].sort()) {
    const b = before[node] ?? { established: [], bgp_best: [] };
    const a = after[node] ?? { established: [], bgp_best: [] };
    const gained = a.bgp_best.filter((r) => !b.bgp_best.includes(r));
    const lost = b.bgp_best.filter((r) => !a.bgp_best.includes(r));
    const up = a.established.filter((s) => !b.established.includes(s));
    const down = b.established.filter((s) => !a.established.includes(s));
    const parts = [
      gained.length ? `gained ${gained.join(", ")}` : "",
      lost.length ? `lost ${lost.join(", ")}` : "",
      up.length ? `sessions up ${up.join(", ")}` : "",
      down.length ? `sessions down ${down.join(", ")}` : "",
    ].filter(Boolean);
    if (parts.length) lines.push(`${node}: ${parts.join("; ")}`);
  }
  return lines;
}

function mismatches(phase: LabPhase | undefined): string[] {
  if (!phase) return [];
  return Object.entries(phase.comparison.nodes).flatMap(([node, diff]) =>
    Object.entries(diff)
      .filter(([, v]) => (Array.isArray(v) ? v.length : v))
      .map(([k, v]) => `${node}: ${k.replace("_", " ")} ${Array.isArray(v) ? v.join(", ") : v}`),
  );
}

/**
 * Deploying the approved candidate to the lab, and what the live routers showed.
 * Only approvers and admins can start a deployment; it runs on the deploy worker.
 */
function Deployments({ run, onDone }: { run: Run; onDone: () => void }) {
  const { state } = useAuth();
  const user = state.status === "signed-in" ? state.user : null;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const latest = run.deployments?.[0];
  const canDeploy = (user?.role === "approver" || user?.role === "admin") && !(latest && deploymentActive(latest));

  async function start() {
    setBusy(true);
    setError(null);
    try {
      await api(`/runs/${run.id}/deploy`, { method: "POST" });
      onDone();
    } catch (err) {
      setError(
        err instanceof ApiError && err.code === "deployment_active"
          ? "Another deployment is using this snapshot's lab. Wait for it to finish."
          : "Couldn't start the deployment. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  const before = latest?.result?.before;
  const after = latest?.result?.after;
  const changes = before && after ? liveChanges(before.live, after.live) : [];
  return (
    <div className="run-section">
      <div className="row between">
        <h3>Lab deployment</h3>
        {canDeploy && (
          <button type="button" className={latest ? "secondary" : undefined} disabled={busy} onClick={() => void start()}>
            {busy ? "Starting…" : latest ? "Deploy again" : "Deploy to lab"}
          </button>
        )}
      </div>
      {!latest && (
        <p className="muted small">
          Boots the lab on the current configs, then on this candidate, and checks the live routers against Batfish's
          prediction. Nothing outside the lab is changed.
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {latest && (
        <>
          <p className="small">
            <span className={`status ${DEPLOY_STATUS[latest.status].tone}`}>{DEPLOY_STATUS[latest.status].text}</span>{" "}
            Started {formatDateTime(latest.createdAt)} by {latest.createdBy}
            {latest.labName ? ` in lab ${latest.labName}` : ""}.
          </p>
          {latest.error && <p className="error">{latest.error}</p>}
          {changes.length > 0 && (
            <>
              <p className="small">
                <strong>Seen live after the change:</strong>
              </p>
              <ul className="run-list small">
                {changes.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </>
          )}
          {after && !after.comparison.ok && (
            <>
              <p className="small">
                <strong>Where the live lab differed from Batfish:</strong>
              </p>
              <ul className="run-list small">
                {mismatches(after).map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </>
          )}
          {before && !before.comparison.ok && (
            <p className="muted small">
              Even before the change, the live lab differed from Batfish: {mismatches(before).join("; ")}.
            </p>
          )}
          <details>
            <summary className="small">Steps ({latest.log.length})</summary>
            <ul className="run-list small">
              {latest.log.map((entry, i) => (
                <li key={i}>
                  <span className="muted">{formatDateTime(entry.at)}</span> {entry.message}
                </li>
              ))}
            </ul>
          </details>
        </>
      )}
    </div>
  );
}

/**
 * After an audit finds problems: start a change run whose intent is what the audit confirmed the
 * structure is meant to do, so a fix is proposed, validated and sent for approval.
 */
function ProposeFix({ run, onStarted }: { run: Run; onStarted: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requirements = (run.state.requirements ?? []).map((r) => r.text).join(" ");
  const intent = `Make ${run.target} do what an audit confirmed it is meant to do. ${run.state.purpose ?? ""} ${requirements}`
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 4000);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      await api("/runs", { method: "POST", body: { investigationId: run.investigationId, mode: "change", intent } });
      onStarted();
    } catch (err) {
      setBusy(false);
      setError(
        err instanceof ApiError && err.code === "run_active"
          ? "Another run is under way on this investigation."
          : "Couldn't start. Try again.",
      );
    }
  }

  return (
    <div className="row">
      <button type="button" disabled={busy} onClick={() => void start()}>
        {busy ? "Starting…" : "Propose a fix"}
      </button>
      <span className="muted small">Starts a change run on what the audit confirmed, and saves it as the change intent.</span>
      {error && (
        <span className="error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}

function CancelButton({ run, onDone }: { run: Run; onDone: () => void }) {
  const { state } = useAuth();
  const user = state.status === "signed-in" ? state.user : null;
  const [busy, setBusy] = useState(false);
  if (!user || (user.id !== run.createdBy.id && user.role !== "admin")) return null;
  return (
    <button
      type="button"
      className="secondary"
      disabled={busy}
      onClick={() => {
        setBusy(true);
        void api(`/runs/${run.id}/cancel`, { method: "POST" })
          .catch(() => undefined)
          .finally(() => {
            setBusy(false);
            onDone();
          });
      }}
    >
      {busy ? "Cancelling…" : "Cancel run"}
    </button>
  );
}
