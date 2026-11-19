import { useState } from "react";
import { useNavigate } from "react-router";
import { api, type Investigation } from "./api";

/**
 * Opens the user's most recent investigation of a snapshot, starting one if they
 * have none. Pass `resume` when they have one, so the button says so. With
 * `fresh`, always starts a new one.
 */
export function InvestigateButton({
  snapshotId,
  snapshotName,
  secondary = false,
  fresh = false,
  resume = false,
}: {
  snapshotId: string;
  snapshotName: string;
  secondary?: boolean;
  fresh?: boolean;
  resume?: boolean;
}) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function start() {
    setBusy(true);
    setFailed(false);
    try {
      const { investigation } = await api<{ investigation: Investigation }>("/investigations", {
        method: "POST",
        body: fresh ? { snapshotId, fresh: true } : { snapshotId },
      });
      navigate(`/investigations/${investigation.id}`);
    } catch {
      setBusy(false);
      setFailed(true);
    }
  }

  return (
    <span className="inline-error">
      <button
        type="button"
        className={secondary ? "secondary" : undefined}
        disabled={busy}
        aria-label={
          fresh
            ? `Start a new investigation of ${snapshotName}`
            : resume
              ? `Resume investigation of ${snapshotName}`
              : `Investigate ${snapshotName}`
        }
        onClick={() => void start()}
      >
        {busy ? "Opening…" : fresh ? "New investigation" : resume ? "Resume" : "Investigate"}
      </button>
      {failed && (
        <span className="error" role="alert">
          Couldn't open. Try again.
        </span>
      )}
    </span>
  );
}
