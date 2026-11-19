export type Role = "requester" | "approver" | "admin";

export interface User {
  id: string;
  email: string;
  role: Role;
}

export interface SnapshotFile {
  path: string;
  sha256: string;
  size: number;
}

export interface SnapshotSummary {
  id: string;
  name: string;
  contentHash: string;
  fileCount: number;
  totalBytes: number;
  createdAt: string;
  createdBy: { id: string; email: string };
  /** The investigation the signed-in user would resume, if they have one. */
  resumeInvestigationId: string | null;
}

export interface Snapshot extends SnapshotSummary {
  files: SnapshotFile[];
}

export type InvestigationStatus = "queued" | "parsing" | "ready" | "failed";

export interface InvestigationSummary {
  id: string;
  status: InvestigationStatus;
  snapshot: { id: string; name: string };
  createdBy: { id: string; email: string };
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  /** True when Batfish already held a parsed model of this content. */
  modelReused: boolean | null;
  batfishInstance: string | null;
  batfishVersion: string | null;
  parseMs: number | null;
  error: string | null;
  /** The user's description of the change they want, if they have given one. */
  intent: string | null;
  intentUpdatedAt: string | null;
  intentUpdatedBy: string | null;
  messageCount: number;
  /** The latest message, or when parsing finished, or when it was started. */
  lastActivityAt: string;
}

export interface ParsedFile {
  path: string;
  status: string;
  format: string;
  nodes: string[];
}

export interface ParseIssue {
  type: string;
  details: string;
  lineText: string | null;
  nodes: string[];
  sources: { path: string; lines: number[] }[];
}

export interface Investigation extends InvestigationSummary {
  result: {
    files: ParsedFile[];
    issues: ParseIssue[];
    /** Whether Batfish could compute routing and forwarding. Absent on older investigations. */
    dataplane?: { ok: boolean; error: string | null };
  } | null;
}

export type RunStatus = "queued" | "running" | "waiting_user" | "waiting_approval" | "done" | "failed" | "cancelled";

export interface CheckResult {
  index: number;
  requirement: string;
  description: string;
  kind: string;
  /** error means Batfish could not decide the check. */
  status: "pass" | "fail" | "error";
  evidence: string;
}

export interface SideEffect {
  name: string;
  /** review marks a change the requirements cover, listed for the approver to confirm. */
  status: "pass" | "fail" | "review";
  summary: string;
  items: string[];
}

export interface RunAttempt {
  round: number;
  summary: string;
  diff?: string;
  content_hash?: string;
  changed?: string[];
  validation?: CheckResult[];
  side_effects?: SideEffect[];
  passed: boolean;
  problem: string;
}

export interface RunState {
  qa?: { question: string; answer: string }[];
  pending_question?: string;
  requirements?: { id: string; text: string }[];
  assumptions?: string[];
  findings?: string;
  steps?: { tool: string; args: unknown; summary: string }[];
  checks?: { requirement: string; description: string }[];
  uncheckable?: { requirement: string; reason: string }[];
  baseline?: CheckResult[];
  /** Corrections the run's starter asked for when reviewing the checks. */
  check_notes?: { note: string; by: string | null }[];
  /** Checks the reviewer removed before a change was proposed. */
  dropped_checks?: { requirement: string; description: string; by: string | null }[];
  checks_confirmed_by?: string;
  attempts?: RunAttempt[];
  headline?: string;
  /** audit runs: what the structure appears meant to do, and problems Batfish found in it without a model. */
  purpose?: string;
  code_findings?: { description: string; evidence: string }[];
}

export interface RunSummary {
  id: string;
  investigationId: string;
  mode: "verify" | "change" | "audit";
  /** audit runs only: the name of the structure audited. */
  target: string | null;
  intent: string;
  status: RunStatus;
  /** The step the run is in or last finished. */
  stage: string | null;
  outcome: string | null;
  error: string | null;
  createdBy: { id: string; email: string };
  createdAt: string;
  updatedAt: string;
  finishedAt: string | null;
  /** The chat message whose suggestion started this run, if any. */
  messageId: string | null;
  approval: {
    decision: "approved" | "rejected";
    comment: string | null;
    candidateHash: string;
    decidedBy: string;
    decidedAt: string;
  } | null;
}

/** What the agent is doing right now: tool calls finished, and the current action. */
export interface AgentProgress {
  step: number;
  doing: string;
}

/** One device's live or predicted BGP state. */
export interface LabState {
  established: string[];
  bgp_best: string[];
  error?: string;
}

export interface LabPhase {
  predicted: Record<string, LabState>;
  live: Record<string, LabState>;
  comparison: {
    ok: boolean;
    nodes: Record<string, Partial<Record<"sessions_missing" | "sessions_unexpected" | "routes_missing" | "routes_unexpected" | "error", string[] | string>>>;
  };
}

export type DeploymentStatus = "queued" | "deploying" | "verifying" | "completed" | "rolled_back" | "failed";

export interface Deployment {
  id: string;
  status: DeploymentStatus;
  stage: string | null;
  labName: string | null;
  candidateHash: string;
  log: { at: string; step: string; message: string }[];
  result: { lab: string; interfaces: Record<string, Record<string, string>>; before?: LabPhase; after?: LabPhase } | null;
  error: string | null;
  createdBy: string;
  createdAt: string;
  finishedAt: string | null;
}

export interface Run extends RunSummary {
  state: RunState;
  /** Deployments of this run's approved candidate to the lab, newest first. */
  deployments: Deployment[];
  /** Live progress within the current step, while a worker is on the run. */
  progress: AgentProgress | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "agent";
  content: string;
  /** Tool calls the agent made while producing this reply. */
  /** round: the model turn the call was made in; calls made together share one. Absent on older messages. */
  steps: { tool: string; args: unknown; summary: string; round?: number }[];
  /** True for an agent message reporting that it could not reply. */
  failed: boolean;
  /** Agent messages only: a workflow run to offer when the user's message reads as one. */
  suggestion: { mode: "verify" | "change" | "audit"; intent: string; target?: string } | null;
  /** How the user's message was sorted after the reply. Null when unknown, including older messages. */
  route: "question" | "check" | "change" | "audit" | null;
  /** Time the worker spent on the reply, and the model that wrote it. Null on older messages. */
  durationMs: number | null;
  model: string | null;
  /** Workflow runs started from this message's suggestion. */
  runs: { id: string; mode: "verify" | "change" | "audit"; status: RunStatus; outcome: string | null; createdAt: string }[];
  createdAt: string;
  createdBy: string | null;
}

export interface ApiIssue {
  path: (string | number)[];
  message: string;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly issues: ApiIssue[] = [],
  ) {
    super(code);
  }
}

/** Fired when a request other than the session check itself comes back 401. */
export const SESSION_EXPIRED_EVENT = "verifier:session-expired";

async function send(path: string, init: { method?: string; body?: unknown }): Promise<string> {
  const res = await fetch(`/api${path}`, {
    method: init.method ?? "GET",
    credentials: "same-origin",
    headers: init.body === undefined ? undefined : { "Content-Type": "application/json" },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  const text = await res.text();

  if (!res.ok) {
    let data: { error?: unknown; issues?: unknown } = {};
    try {
      data = JSON.parse(text) as typeof data;
    } catch {
      // Non-JSON error body, e.g. from the dev proxy when the API is down.
    }
    if (res.status === 401 && !path.startsWith("/auth/")) {
      window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
    }
    throw new ApiError(
      res.status,
      typeof data.error === "string" ? data.error : "unknown_error",
      Array.isArray(data.issues) ? (data.issues as ApiIssue[]) : [],
    );
  }
  return text;
}

/** Calls the API through the same-origin /api prefix. Throws ApiError on non-2xx. */
export async function api<T = void>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const text = await send(path, init);
  return (text ? JSON.parse(text) : undefined) as T;
}

/** Like api(), for endpoints that return plain text. */
export function apiText(path: string): Promise<string> {
  return send(path, {});
}
