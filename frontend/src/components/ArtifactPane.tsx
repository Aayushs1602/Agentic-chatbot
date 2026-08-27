import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize from "rehype-sanitize";
import { ArtifactFrame } from "./ArtifactFrame";
import type { Artifact } from "../lib/types";

type Tab = "preview" | "source";

export function ArtifactPane({
  artifacts,
  activeId,
  onSelect,
  onClose,
}: {
  artifacts: Artifact[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>("preview");
  const [allowScripts, setAllowScripts] = useState(false);
  const [showReport, setShowReport] = useState(false);

  const active = artifacts.find((a) => a.id === activeId) ?? artifacts.at(-1) ?? null;

  // Scripts are re-disabled whenever a different document is shown. An opt-in
  // granted for one artifact must not silently carry to the next.
  useEffect(() => {
    setAllowScripts(false);
    setTab("preview");
  }, [active?.id]);

  if (!active) return null;

  const report = active.sanitizer_report;
  const removed = report?.total_removed ?? 0;

  return (
    <section
      aria-label="Artifact viewer"
      className="flex h-full min-w-0 flex-col border-l border-border bg-surface"
    >
      <header className="flex items-center gap-2 border-b border-border px-3 py-2">
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-[13.5px] font-semibold">{active.title}</h2>
          <p className="text-[11px] text-muted">
            {active.kind === "html" ? "HTML document" : "Markdown document"}
          </p>
        </div>

        <div className="flex rounded-lg border border-border p-0.5" role="tablist">
          {(["preview", "source"] as Tab[]).map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={[
                "rounded-md px-2.5 py-1 text-[12px] capitalize",
                tab === t ? "bg-accent-soft text-accent" : "text-muted hover:text-text",
              ].join(" ")}
            >
              {t}
            </button>
          ))}
        </div>

        <button
          type="button"
          onClick={() => navigator.clipboard?.writeText(active.content)}
          className="rounded-lg border border-border px-2 py-1.5 text-[12px] hover:bg-surface-2"
        >
          Copy
        </button>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close artifact viewer"
          className="rounded-lg border border-border px-2 py-1.5 text-[12px] hover:bg-surface-2"
        >
          ✕
        </button>
      </header>

      {artifacts.length > 1 && (
        <nav className="flex gap-1 overflow-x-auto border-b border-border px-3 py-1.5">
          {artifacts.map((a) => (
            <button
              key={a.id}
              onClick={() => onSelect(a.id)}
              className={[
                "shrink-0 rounded-md px-2 py-1 text-[12px]",
                a.id === active.id
                  ? "bg-accent-soft text-accent"
                  : "text-muted hover:bg-surface-2",
              ].join(" ")}
            >
              {a.title}
            </button>
          ))}
        </nav>
      )}

      {/* The security disclosure. A sanitizer that strips silently is a black
          box; showing what it removed and why is what the brief asks for. */}
      {removed > 0 && (
        <div className="border-b border-warn/30 bg-warn/10 px-3 py-2">
          <button
            type="button"
            onClick={() => setShowReport((v) => !v)}
            aria-expanded={showReport}
            className="flex w-full items-center gap-1.5 text-[12.5px] font-medium text-warn"
          >
            <span aria-hidden>🛡</span>
            {removed} element{removed === 1 ? "" : "s"} removed for safety
            <span aria-hidden className="ml-auto">{showReport ? "▾" : "▸"}</span>
          </button>

          {showReport && (
            <div className="mt-2 space-y-1.5 text-[12px] text-muted">
              <ul className="space-y-0.5">
                {report.notes?.map((note) => (
                  <li key={note}>• {renderNote(note)}</li>
                ))}
              </ul>
              <p className="pt-1">
                Rendered in a sandboxed frame with no scripts, no network access,
                and no access to this page.
              </p>
            </div>
          )}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto">
        {tab === "source" ? (
          <pre className="h-full overflow-auto p-3 text-[12px] leading-relaxed">
            <code>{active.content}</code>
          </pre>
        ) : active.kind === "html" ? (
          <ArtifactFrame
            html={active.content}
            allowScripts={allowScripts}
            title={active.title}
          />
        ) : (
          <div className="prose-answer p-4 text-[14px]">
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
              {active.content}
            </ReactMarkdown>
          </div>
        )}
      </div>

      {active.kind === "html" && tab === "preview" && (
        <footer className="flex items-center gap-2 border-t border-border px-3 py-2">
          <label className="flex items-center gap-2 text-[12px] text-muted">
            <input
              type="checkbox"
              checked={allowScripts}
              onChange={(e) => setAllowScripts(e.target.checked)}
            />
            Allow scripts
          </label>
          <span className="text-[11px] text-muted">
            {allowScripts
              ? "Running in an opaque origin — still no network or page access."
              : "Off by default."}
          </span>
        </footer>
      )}
    </section>
  );
}

/** Render inline `<tag>` mentions in a note as code. */
function renderNote(note: string) {
  return note.split(/(`[^`]+`)/g).map((part, i) =>
    part.startsWith("`") && part.endsWith("`") ? (
      <code key={i} className="rounded bg-surface-2 px-1">
        {part.slice(1, -1)}
      </code>
    ) : (
      part
    ),
  );
}
