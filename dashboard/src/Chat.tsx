import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, ApiError, type AgentProgress, type ChatMessage, type Run } from "./api";
import { CodePreview } from "./CodePreview";
import { Icon } from "./Icon";
import { formatDateTime, formatDuration, progressText } from "./format";
import { runLabel } from "./RunPanel";

const MARKDOWN_COMPONENTS = { pre: CodePreview };

const POLL_MS = 1500;
// While a run started from the chat is unfinished, its status in the trace is refreshed this often.
const POLL_RUNS_MS = 5000;
const MAX_MESSAGE_LENGTH = 4000;

interface Conversation {
  messages: ChatMessage[];
  agentPending: boolean;
  agentProgress?: AgentProgress | null;
}

/**
 * The conversation with the agent about one investigation.
 *
 * `onRunStarted` is called when a run is started from a suggestion in the chat.
 */
export function Chat({
  investigationId,
  onRunStarted,
  onOpenRun,
}: {
  investigationId: string;
  onRunStarted: (run: Run) => void;
  onOpenRun: (runId: string) => void;
}) {
  const [tick, setTick] = useState(0);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const followRef = useRef(true);
  const [unread, setUnread] = useState(false);
  const pending = conversation?.agentPending ?? false;

  // Load the conversation, and keep refreshing while the agent is working.
  useEffect(() => {
    let current = true;
    let timer: number | undefined;

    async function refresh() {
      try {
        const data = await api<Conversation>(`/investigations/${investigationId}/messages`);
        if (!current) return;
        setConversation(data);
        setLoadFailed(false);
        const runsOpen = data.messages.some((m) =>
          m.runs.some((r) => r.status !== "done" && r.status !== "failed" && r.status !== "cancelled"),
        );
        if (data.agentPending) timer = window.setTimeout(() => void refresh(), POLL_MS);
        else if (runsOpen) timer = window.setTimeout(() => void refresh(), POLL_RUNS_MS);
      } catch {
        if (!current) return;
        setLoadFailed(true);
        timer = window.setTimeout(() => void refresh(), POLL_MS * 2);
      }
    }
    void refresh();

    return () => {
      current = false;
      window.clearTimeout(timer);
    };
    // Re-runs when a reply becomes pending or a run is started, which restarts the refresh loop.
  }, [investigationId, pending, tick]);

  const count = conversation?.messages.length ?? 0;
  useEffect(() => {
    const area = scrollRef.current;
    if (!area) return;
    if (followRef.current) {
      area.scrollTop = area.scrollHeight;
      setUnread(false);
    } else if (count > 0) {
      setUnread(true);
    }
  }, [count, pending]);

  useEffect(() => {
    const input = inputRef.current;
    if (input) {
      input.style.height = "auto";
      input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
    }
  }, [draft]);

  function useSuggestion(text: string) {
    setDraft(text);
    inputRef.current?.focus();
  }

  async function send() {
    const content = draft.trim();
    if (!content || sending || pending) return;
    setSending(true);
    setSendError(null);
    try {
      const { message } = await api<{ message: ChatMessage }>(`/investigations/${investigationId}/messages`, {
        method: "POST",
        body: { content },
      });
      followRef.current = true;
      setDraft("");
      inputRef.current?.focus();
      setConversation((c) => ({ messages: [...(c?.messages ?? []), message], agentPending: true }));
    } catch (err) {
      if (err instanceof ApiError && err.code === "agent_busy") {
        setSendError("The agent is still replying. Send your message when it has finished.");
        setConversation((c) => (c ? { ...c, agentPending: true } : c));
      } else {
        setSendError("Couldn't send your message. It is still in the box; try again.");
      }
    } finally {
      setSending(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void send();
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  }

  return (
    <section className="card chat" aria-label="Agent conversation">
      <header className="chat-header">
        <div className="row"><span className="agent-avatar"><Icon name="spark" size={19} /></span><div><h2>Investigation agent</h2><p className="muted small">Grounded in your network configuration</p></div></div>
        <span className="agent-state"><span className={`status-dot${pending || loadFailed ? " working" : ""}`} />{loadFailed ? "Reconnecting" : pending ? "Investigating" : conversation ? "Ready" : "Connecting"}</span>
      </header>

      <div className="chat-scroll" ref={scrollRef} onScroll={() => {
        const area = scrollRef.current;
        if (!area) return;
        followRef.current = area.scrollHeight - area.scrollTop - area.clientHeight < 64;
        if (followRef.current) setUnread(false);
      }}>
        {conversation === null && !loadFailed && <p className="muted chat-loading" role="status">Loading your conversation…</p>}
        {loadFailed && <p className="error chat-loading" role="alert">Couldn't refresh the conversation. Reconnecting automatically.</p>}

        {conversation && conversation.messages.length === 0 && (
          <div className="chat-welcome">
            <span className="welcome-mark"><Icon name="spark" size={30} /></span>
            <p className="eyebrow">YOUR NETWORK, UNDERSTOOD</p>
            <h2>What would you like to investigate?</h2>
            <p className="muted">Trace a route, explore a policy, or understand a configuration. Ask a question to get started.</p>
            <div className="suggestions">
              {[
                { title: "Understand this snapshot", text: "Summarize the network devices and routing configuration in this snapshot.", icon: "network" as const },
                { title: "Review routing policies", text: "Review the routing policies in this snapshot and explain any potential issues.", icon: "shield" as const },
                { title: "Find configuration issues", text: "Check this snapshot for configuration issues and explain their impact.", icon: "search" as const },
              ].map((item) => <button type="button" className="suggestion" key={item.title} onClick={() => useSuggestion(item.text)}><Icon name={item.icon} size={19} /><span>{item.title}</span><Icon name="chevron" size={15} /></button>)}
            </div>
          </div>
        )}

        {conversation && conversation.messages.length > 0 && (
          <ol className="messages" aria-label="Conversation messages">
            {conversation.messages.map((m) => (
              <li key={m.id} id={`message-${m.id}`} className={`message ${m.role}${m.failed ? " failed" : ""}`}>
                <div className="message-avatar" aria-hidden="true">{m.role === "agent" ? <Icon name="spark" size={17} /> : (m.createdBy ?? "You").charAt(0).toUpperCase()}</div>
                <div className="message-body">
                  <div className="message-head">
                    <strong>{m.role === "agent" ? "Verifier" : (m.createdBy ?? "You")}</strong>
                    <time className="muted small" dateTime={m.createdAt}>{formatDateTime(m.createdAt)}</time>
                  </div>
                  {m.role === "agent" && !m.failed ? (
                    <div className="markdown"><Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>{m.content}</Markdown></div>
                  ) : <p className="plain">{m.content}</p>}
                  {m.steps.length > 0 && (
                    <details className="steps">
                      <summary>{m.steps.length} investigation {m.steps.length === 1 ? "step" : "steps"}<span className="step-hint">View evidence</span></summary>
                      <ol>{m.steps.map((step, i) => (
                        <li key={i}>
                          <strong className="tool-name">{step.tool}</strong>
                          <pre>{JSON.stringify(step.args, null, 2)}</pre>
                          {step.summary && <pre>{step.summary}</pre>}
                        </li>
                      ))}</ol>
                    </details>
                  )}
                  {m.role === "agent" && <Trace message={m} onOpenRun={onOpenRun} />}
                  {m.suggestion && m.runs.length === 0 && m === conversation.messages.at(-1) && (
                    <SuggestedRun
                      investigationId={investigationId}
                      messageId={m.id}
                      suggestion={m.suggestion}
                      onStarted={(run) => {
                        onRunStarted(run);
                        setTick((n) => n + 1);
                      }}
                    />
                  )}
                  {m.role === "agent" && !m.failed && <CopyReply content={m.content} />}
                </div>
              </li>
            ))}
          </ol>
        )}
        {pending && (
          <div className="thinking" role="status">
            <span className="thinking-dots"><i /><i /><i /></span>
            {progressText(conversation?.agentProgress) ?? "Waiting for the agent…"}
          </div>
        )}
      </div>

      {unread && <button className="new-message secondary" type="button" onClick={() => {
        followRef.current = true;
        if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        setUnread(false);
      }}>Jump to latest ↓</button>}
      <div className="composer-area">
        <form className="composer" onSubmit={onSubmit}>
          <label htmlFor="chat-input" className="visually-hidden">Message to the agent</label>
          <textarea ref={inputRef} id="chat-input" rows={2} maxLength={MAX_MESSAGE_LENGTH}
            placeholder="Ask anything about this snapshot…" value={draft}
            onChange={(e) => setDraft(e.target.value)} onKeyDown={onKeyDown}
            aria-describedby="composer-help" />
          <div className="composer-toolbar"><span className="composer-context"><Icon name="layers" size={14} />Current snapshot</span>
            <div className="row"><span className="muted small">{draft.length > 3500 ? `${draft.length}/${MAX_MESSAGE_LENGTH}` : ""}</span>
              <button type="submit" className="send-button" title="Send message" aria-label={sending ? "Sending message" : "Send message"} disabled={draft.trim() === "" || sending || pending || conversation === null}><Icon name="arrow" size={19} /></button>
            </div>
          </div>
        </form>
        <p className="composer-help" id="composer-help">{pending ? "You can draft your next question while the agent works." : "Enter to send · Shift+Enter for a new line"}<span>Read-only analysis</span></p>
        {sendError && <p className="error" role="alert">{sendError}</p>}
      </div>
    </section>
  );
}

/**
 * Offers to run the staged workflow on what the user just asked for. Nothing
 * starts until they click; starting saves the text as the change intent.
 */
const MAX_TOOL_KINDS = 6;

/** The tools a reply used, in order of first use with counts: "read_config ×3 · routes · search_configs ×2". */
function toolSummary(steps: ChatMessage["steps"]): string {
  const counts = new Map<string, number>();
  for (const step of steps) {
    const args = step.args as { question?: unknown } | null;
    const name = step.tool === "batfish_question" && typeof args?.question === "string" ? args.question : step.tool;
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  const parts = [...counts].map(([name, n]) => (n > 1 ? `${name} ×${n}` : name));
  const more = parts.length - MAX_TOOL_KINDS;
  return parts.slice(0, MAX_TOOL_KINDS).join(" · ") + (more > 0 ? ` · ${more} more` : "");
}

/** " in 6 rounds" when calls were made together, so the trace shows how much ran in parallel. */
function roundsText(steps: ChatMessage["steps"]): string {
  const rounds = new Set(steps.map((s) => s.round).filter((r) => r !== undefined)).size;
  return rounds > 0 && rounds < steps.length ? ` in ${rounds} ${rounds === 1 ? "round" : "rounds"}` : "";
}

const ROUTE_TEXT = { question: "a question", check: "something to check", change: "a change request", audit: "a request for an audit" };

/**
 * What ran for one agent reply: the agent loop, how the message was sorted,
 * and any workflow run started from it, each run linking to the run panel.
 */
function Trace({ message: m, onOpenRun }: { message: ChatMessage; onOpenRun: (runId: string) => void }) {
  const parts = [
    "Investigation agent",
    `${m.steps.length} tool ${m.steps.length === 1 ? "call" : "calls"}` + roundsText(m.steps),
    m.durationMs !== null ? formatDuration(m.durationMs) : null,
    m.model,
    m.route ? `sorted as ${ROUTE_TEXT[m.route]}` : null,
  ].filter(Boolean);
  return (
    <div className="message-trace" aria-label="What ran for this reply">
      <span>{parts.join(" · ")}</span>
      {m.steps.length > 0 && <span className="trace-tools">{toolSummary(m.steps)}</span>}
      {m.runs.map((r) => {
        const label = runLabel(r.status, r.outcome);
        return (
          <button key={r.id} type="button" className="trace-run" onClick={() => onOpenRun(r.id)}>
            → {r.mode === "verify" ? "Check" : "Change proposal"} run
            <span className={`status ${label.tone}`}>{label.text}</span>
          </button>
        );
      })}
    </div>
  );
}

function SuggestedRun({
  investigationId,
  messageId,
  suggestion,
  onStarted,
}: {
  investigationId: string;
  messageId: string;
  suggestion: NonNullable<ChatMessage["suggestion"]>;
  onStarted: (run: Run) => void;
}) {
  const [state, setState] = useState<"idle" | "starting" | "started">("idle");
  const [error, setError] = useState<string | null>(null);
  const change = suggestion.mode === "change";
  const audit = suggestion.mode === "audit";

  async function start() {
    setState("starting");
    setError(null);
    try {
      const { run } = await api<{ run: Run }>("/runs", {
        method: "POST",
        body: audit
          ? { investigationId, mode: "audit", target: suggestion.target, messageId }
          : { investigationId, mode: suggestion.mode, intent: suggestion.intent, messageId },
      });
      setState("started");
      onStarted(run);
    } catch (err) {
      setState("idle");
      setError(
        err instanceof ApiError && err.code === "run_active"
          ? "A run is already under way on this investigation. Wait for it to finish, or cancel it."
          : "Couldn't start. Try again.",
      );
    }
  }

  if (state === "started") {
    return <p className="run-suggestion muted small" role="status">Started. Follow it in the panel above.</p>;
  }
  return (
    <div className="run-suggestion">
      <p className="small">
        <strong>
          {audit
            ? `This asks for a review of ${suggestion.target}.`
            : change
              ? "This reads like a change request."
              : "This reads like something to check."}
        </strong>{" "}
        {audit
          ? "An audit works out what it is meant to do, asks you to confirm that, and checks it on Batfish."
          : change
            ? "The workflow can propose a change, validate it on Batfish and send it for approval."
            : "The workflow can check it on Batfish and give a verdict."}
      </p>
      {!audit && <blockquote className="small">{suggestion.intent}</blockquote>}
      <div className="row">
        <button type="button" disabled={state === "starting"} onClick={() => void start()}>
          {state === "starting"
            ? "Starting…"
            : audit
              ? `Audit ${suggestion.target}`
              : change
                ? "Propose a change for this"
                : "Run this as a check"}
        </button>
        {!audit && <span className="muted small">Starting saves this as the change intent.</span>}
      </div>
      {error && (
        <p className="error small" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function CopyReply({ content }: { content: string }) {
  const [status, setStatus] = useState<"idle" | "copied" | "error">("idle");
  useEffect(() => {
    if (status === "idle") return;
    const timer = window.setTimeout(() => setStatus("idle"), 2500);
    return () => window.clearTimeout(timer);
  }, [status]);
  return <button type="button" className="copy-reply" onClick={() => {
    void (async () => {
      try {
        await navigator.clipboard.writeText(content);
        setStatus("copied");
      } catch {
        setStatus("error");
      }
    })();
  }}><Icon name={status === "copied" ? "check" : "copy"} size={13} /><span role="status">{status === "copied" ? "Copied" : status === "error" ? "Couldn't copy. Try again." : "Copy response"}</span></button>;
}
