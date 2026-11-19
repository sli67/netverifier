import { useRef, useState } from "react";
import { api, ApiError } from "./api";
import { useAuth } from "./auth";

/**
 * Deletes an investigation and its conversation after confirmation. Renders
 * nothing for users who are not allowed to (only its starter and admins are).
 */
export function DeleteInvestigationButton({
  investigation,
  onDeleted,
}: {
  investigation: { id: string; snapshot: { name: string }; createdBy: { id: string } };
  onDeleted: () => void;
}) {
  const { state } = useAuth();
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const user = state.status === "signed-in" ? state.user : null;
  if (!user || (user.id !== investigation.createdBy.id && user.role !== "admin")) return null;

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await api(`/investigations/${investigation.id}`, { method: "DELETE" });
      dialog.current?.close();
      onDeleted();
    } catch (err) {
      setBusy(false);
      if (err instanceof ApiError && err.status === 404) {
        // Someone else already deleted it: the outcome the user wanted.
        dialog.current?.close();
        onDeleted();
      } else if (err instanceof ApiError && err.status === 403) {
        setError("You don't have permission to delete this investigation.");
      } else {
        setError("Couldn't delete the investigation. Try again.");
      }
    }
  }

  return (
    <>
      <button
        type="button"
        className="secondary danger"
        aria-label={`Delete investigation of ${investigation.snapshot.name}`}
        onClick={() => {
          setError(null);
          setBusy(false);
          dialog.current?.showModal();
        }}
      >
        Delete
      </button>
      <dialog ref={dialog} aria-labelledby={`delete-inv-${investigation.id}`} onCancel={(e) => busy && e.preventDefault()}>
        <h2 id={`delete-inv-${investigation.id}`}>Delete this investigation?</h2>
        <p>
          This permanently removes the investigation of "{investigation.snapshot.name}", its intent and its agent
          conversation. The snapshot is kept. It cannot be undone.
        </p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="row end">
          <button type="button" className="secondary" disabled={busy} onClick={() => dialog.current?.close()}>
            Cancel
          </button>
          <button type="button" className="danger" disabled={busy} onClick={() => void confirm()}>
            {busy ? "Deleting…" : "Delete investigation"}
          </button>
        </div>
      </dialog>
    </>
  );
}
