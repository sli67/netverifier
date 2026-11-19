import type { AgentProgress } from "./api";

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export function formatDateTime(iso: string): string {
  return dateTime.format(new Date(iso));
}

export function formatDuration(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** One short line saying where the agent is: "Step 7 · Batfish routes", or "Thinking · 6 tool calls so far". */
export function progressText(progress: AgentProgress | null | undefined): string | null {
  if (!progress) return null;
  if (progress.doing !== "thinking") return `Step ${progress.step + 1} · ${progress.doing}`;
  return progress.step === 0 ? "Thinking" : `Thinking · ${progress.step} tool ${progress.step === 1 ? "call" : "calls"} so far`;
}
