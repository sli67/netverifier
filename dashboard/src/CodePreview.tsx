import { Children, isValidElement, useId, useState, type ReactNode } from "react";

const PREVIEW_LINES = 24;
const PREVIEW_CHARACTERS = 2400;

function textContent(children: ReactNode): string {
  return Children.toArray(children).map((child): string => {
    if (typeof child === "string" || typeof child === "number") return String(child);
    return isValidElement<{ children?: ReactNode }>(child) ? textContent(child.props.children) : "";
  }).join("");
}

/** Keeps long configurations and agent code blocks compact until expanded. */
export function CodePreview({ children }: { children?: ReactNode }) {
  const [expanded, setExpanded] = useState(false);
  const id = useId();
  const content = textContent(children);
  const lines = content.replace(/\n$/, "").split("\n");
  const long = lines.length > PREVIEW_LINES || content.length > PREVIEW_CHARACTERS;
  const preview = lines.slice(0, PREVIEW_LINES).join("\n").slice(0, PREVIEW_CHARACTERS);

  return (
    <div className="code-preview">
      <pre id={id}><code>{long && !expanded ? `${preview}\n…` : content}</code></pre>
      {long && (
        <div className="code-preview-footer">
          <span className="muted small">{lines.length} {lines.length === 1 ? "line" : "lines"}{!expanded ? " · Preview" : ""}</span>
          <button type="button" className="secondary" aria-expanded={expanded} aria-controls={id}
            onClick={() => setExpanded((value) => !value)}>
            {expanded ? "Show less" : "Show full configuration"}
          </button>
        </div>
      )}
    </div>
  );
}
