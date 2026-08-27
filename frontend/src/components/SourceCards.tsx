import { useState } from "react";
import type { Citation } from "../lib/types";

/**
 * Sources behind an answer.
 *
 * Every citation deep-links to the exact second of the episode, which is what
 * makes grounding checkable rather than asserted: the user can watch the guest
 * say it. Collapsed by default so it never competes with the answer.
 */
export function SourceCards({
  citations,
  highlighted,
}: {
  citations: Citation[];
  highlighted?: string | null;
}) {
  const [open, setOpen] = useState(false);

  if (citations.length === 0) return null;

  return (
    <div className="mt-4 border-t border-border pt-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 text-[13px] font-medium text-muted hover:text-text"
      >
        <span
          aria-hidden
          className="transition-transform"
          style={{ transform: open ? "rotate(90deg)" : "none" }}
        >
          ▸
        </span>
        {citations.length} source{citations.length === 1 ? "" : "s"}
      </button>

      {open && (
        <ul className="mt-2.5 space-y-2">
          {citations.map((c) => (
            <li
              key={c.chunk_id}
              id={`source-${c.marker}`}
              className={[
                "rounded-lg border p-2.5 text-[13px] transition-colors",
                highlighted === c.marker
                  ? "border-accent bg-accent-soft"
                  : "border-border bg-surface-2",
              ].join(" ")}
            >
              <div className="flex items-start gap-2">
                <span className="mt-px rounded bg-accent-soft px-1.5 py-0.5 text-[11px] font-bold text-accent">
                  {c.marker}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="font-medium leading-snug">{c.title}</p>
                  {c.guests.length > 0 && (
                    <p className="mt-0.5 text-muted">{c.guests.join(", ")}</p>
                  )}
                  {c.url && (
                    <a
                      href={c.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="mt-1 inline-block text-accent underline underline-offset-2"
                    >
                      {c.start_seconds !== null
                        ? `Listen from ${formatTime(c.start_seconds)}`
                        : "Listen"}
                      <span className="sr-only"> (opens in a new tab)</span>
                    </a>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function formatTime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
