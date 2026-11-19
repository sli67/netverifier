import type { InvestigationStatus } from "./api";

const LABELS: Record<InvestigationStatus, { text: string; tone: string }> = {
  queued: { text: "Queued", tone: "pending" },
  parsing: { text: "Parsing", tone: "pending" },
  ready: { text: "Ready", tone: "ok" },
  failed: { text: "Failed", tone: "bad" },
};

export function StatusBadge({ status }: { status: InvestigationStatus }) {
  const { text, tone } = LABELS[status];
  return <span className={`status ${tone}`}>{text}</span>;
}
