import { useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { api, ApiError, type Snapshot } from "../api";
import { formatBytes } from "../format";

// These mirror the limits the API enforces, so problems show before uploading.
const MAX_FILES = 200;
const MAX_FILE_BYTES = 512 * 1024;
const MAX_TOTAL_BYTES = 5 * 1024 * 1024;
const MAX_PATH_DEPTH = 4;
const PATH_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

interface Draft {
  key: number;
  path: string;
  file: File;
}

function problemWith(draft: Draft, all: Draft[]): string | null {
  const segments = draft.path.split("/");
  if (!draft.path) return "Enter a path.";
  if (segments.length > MAX_PATH_DEPTH) return `Use at most ${MAX_PATH_DEPTH} path levels.`;
  if (!segments.every((s) => PATH_SEGMENT.test(s))) {
    return "Use letters, digits, '.', '_' and '-', separated by '/'.";
  }
  const lower = draft.path.toLowerCase();
  if (all.some((d) => d.key !== draft.key && d.path.toLowerCase() === lower)) return "Another file has this path.";
  if (draft.file.size === 0) return "This file is empty.";
  if (draft.file.size > MAX_FILE_BYTES) return `Larger than ${formatBytes(MAX_FILE_BYTES)}.`;
  return null;
}

export function SnapshotNew() {
  const navigate = useNavigate();
  const nextKey = useRef(0);
  const [name, setName] = useState("");
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [skipped, setSkipped] = useState(0);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  function onPick(e: ChangeEvent<HTMLInputElement>, fromFolder: boolean) {
    const picked = Array.from(e.target.files ?? []);
    e.target.value = ""; // allow picking the same files again
    const added: Draft[] = [];
    let hidden = 0;
    for (const file of picked) {
      // A folder pick reports "lab/r1/frr.conf"; drop the chosen folder's own name.
      const path = fromFolder ? file.webkitRelativePath.split("/").slice(1).join("/") : file.name;
      if (path.split("/").some((s) => s.startsWith("."))) {
        hidden += 1;
        continue;
      }
      added.push({ key: nextKey.current++, path, file });
    }
    added.sort((a, b) => a.path.localeCompare(b.path));
    setDrafts((d) => [...d, ...added]);
    setSkipped((n) => n + hidden);
    setErrors([]);
  }

  const totalBytes = drafts.reduce((sum, d) => sum + d.file.size, 0);
  const problems = new Map(drafts.map((d) => [d.key, problemWith(d, drafts)]));
  const overall =
    drafts.length > MAX_FILES
      ? `A snapshot can hold at most ${MAX_FILES} files.`
      : totalBytes > MAX_TOTAL_BYTES
        ? `A snapshot can be at most ${formatBytes(MAX_TOTAL_BYTES)}.`
        : null;
  const valid = name.trim() !== "" && drafts.length > 0 && !overall && [...problems.values()].every((p) => !p);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    setErrors([]);
    try {
      const files = await Promise.all(drafts.map(async (d) => ({ path: d.path, content: await d.file.text() })));
      const { snapshot } = await api<{ snapshot: Snapshot }>("/snapshots", {
        method: "POST",
        body: { name: name.trim(), files },
      });
      navigate(`/snapshots/${snapshot.id}`);
    } catch (err) {
      setBusy(false);
      if (err instanceof ApiError && err.issues.length > 0) {
        setErrors(
          err.issues.map((issue) => {
            const index = issue.path[0] === "files" && typeof issue.path[1] === "number" ? issue.path[1] : null;
            const label = index === null ? null : drafts[index]?.path;
            return label ? `${label}: ${issue.message}` : issue.message;
          }),
        );
      } else if (err instanceof ApiError && err.status === 413) {
        setErrors(["The snapshot is too large to upload."]);
      } else {
        setErrors(["Couldn't upload the snapshot. Check that the API is running and try again."]);
      }
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Upload snapshot</h1>
      </div>

      <form className="card form" onSubmit={onSubmit}>
        <div className="field">
          <label htmlFor="name">Name</label>
          <input
            id="name"
            required
            maxLength={120}
            placeholder="Lab baseline"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>

        <div className="field">
          <span className="label">Configuration files</span>
          <p className="muted small">
            Choose a folder with one subfolder per router (for example <code>r1/frr.conf</code>), or add individual
            files and set each path.
          </p>
          <div className="row">
            <label className="button secondary">
              Add folder
              <input
                type="file"
                className="visually-hidden"
                multiple
                onChange={(e) => onPick(e, true)}
                {...({ webkitdirectory: "" } as object)}
              />
            </label>
            <label className="button secondary">
              Add files
              <input type="file" className="visually-hidden" multiple onChange={(e) => onPick(e, false)} />
            </label>
          </div>
        </div>

        {skipped > 0 && (
          <p className="muted small">
            Skipped {skipped} hidden {skipped === 1 ? "file" : "files"}.
          </p>
        )}

        {drafts.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Path in snapshot</th>
                  <th className="num">Size</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {drafts.map((d) => {
                  const problem = problems.get(d.key);
                  return (
                    <tr key={d.key}>
                      <td>
                        <input
                          className="mono"
                          aria-label={`Path for ${d.file.name}`}
                          aria-invalid={problem ? true : undefined}
                          value={d.path}
                          onChange={(e) =>
                            setDrafts((all) => all.map((x) => (x.key === d.key ? { ...x, path: e.target.value } : x)))
                          }
                        />
                        {problem && <p className="error">{problem}</p>}
                      </td>
                      <td className="num">{formatBytes(d.file.size)}</td>
                      <td className="num">
                        <button
                          type="button"
                          className="secondary"
                          aria-label={`Remove ${d.path || d.file.name}`}
                          onClick={() => setDrafts((all) => all.filter((x) => x.key !== d.key))}
                        >
                          Remove
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="muted small">
              {drafts.length} {drafts.length === 1 ? "file" : "files"}, {formatBytes(totalBytes)}
            </p>
          </div>
        )}

        {overall && <p className="error">{overall}</p>}

        {errors.length > 0 && (
          <div className="error" role="alert">
            {errors.map((message, i) => (
              <p key={i}>{message}</p>
            ))}
          </div>
        )}

        <div className="row">
          <button type="submit" disabled={!valid || busy}>
            {busy ? "Uploading…" : "Upload snapshot"}
          </button>
          <Link className="button secondary" to="/">
            Cancel
          </Link>
        </div>
      </form>
    </>
  );
}
