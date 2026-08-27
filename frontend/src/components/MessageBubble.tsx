import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize from "rehype-sanitize";
import { AgentSteps } from "./AgentSteps";
import { SourceCards } from "./SourceCards";
import type { AgentStep, ApiErrorBody, Citation } from "../lib/types";

export interface BubbleProps {
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  steps?: AgentStep[];
  streaming?: boolean;
  abstained?: boolean;
  error?: ApiErrorBody | null;
  meta?: { provider?: string | null; model?: string | null; latencyMs?: number | null };
}

export function MessageBubble({
  role,
  content,
  citations = [],
  steps = [],
  streaming = false,
  abstained = false,
  error = null,
  meta,
}: BubbleProps) {
  const [highlighted, setHighlighted] = useState<string | null>(null);

  if (role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-accent px-4 py-2.5 text-white">
          <p className="whitespace-pre-wrap break-words text-[15px] leading-relaxed">
            {content}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-[92%]">
      <AgentSteps steps={steps} active={streaming} />

      {error ? (
        <ErrorCard error={error} />
      ) : (
        <div
          className={[
            "rounded-2xl rounded-bl-md border px-4 py-3",
            abstained
              ? "border-warn/35 bg-warn/8"
              : "border-border bg-surface",
          ].join(" ")}
        >
          {abstained && (
            <p className="mb-2 flex items-center gap-1.5 text-[12px] font-semibold uppercase tracking-wide text-warn">
              <span aria-hidden>⚠</span> Outside the corpus
            </p>
          )}

          <div
            className="prose-answer text-[15px]"
            /* Announce the answer once it settles rather than on every token —
               a per-token live region makes screen readers unusable. */
            aria-live={streaming ? "off" : "polite"}
            aria-busy={streaming}
            onClick={(e) => {
              const target = e.target as HTMLElement;
              if (target.dataset.marker) {
                setHighlighted(target.dataset.marker);
                document
                  .getElementById(`source-${target.dataset.marker}`)
                  ?.scrollIntoView({ block: "nearest", behavior: "smooth" });
              }
            }}
          >
            <Markdown content={content} />
            {streaming && <span className="streaming-caret" aria-hidden />}
          </div>

          <SourceCards citations={citations} highlighted={highlighted} />
        </div>
      )}

      {meta?.provider && !streaming && (
        <p className="mt-1.5 text-[11px] text-muted">
          {meta.provider}
          {meta.model ? ` · ${meta.model}` : ""}
          {meta.latencyMs ? ` · ${(meta.latencyMs / 1000).toFixed(1)}s` : ""}
        </p>
      )}
    </div>
  );
}

/**
 * Markdown rendering for chat answers.
 *
 * `rehypeSanitize` runs with raw HTML disabled — a chat answer never needs to
 * emit HTML, so the safest policy is to allow none. Generated HTML *artifacts*
 * take an entirely different, sandboxed path; keeping the two separate means a
 * loosening there can never widen what chat renders.
 */
function Markdown({ content }: { content: string }) {
  const withMarkers = useMemo(() => content, [content]);

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeSanitize]}
      components={{
        a: ({ children, href }) => (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {children}
          </a>
        ),
        p: ({ children }) => <p>{renderMarkers(children)}</p>,
        li: ({ children }) => <li>{renderMarkers(children)}</li>,
      }}
    >
      {withMarkers}
    </ReactMarkdown>
  );
}

/** Turn `[S1]` in rendered text into a clickable chip. */
function renderMarkers(children: React.ReactNode): React.ReactNode {
  return mapStrings(children, (text, key) => {
    const parts = text.split(/(\[S\d+(?:\s*,\s*S\d+)*\])/g);
    if (parts.length === 1) return text;

    return parts.map((part, i) => {
      const match = part.match(/^\[(S\d+(?:\s*,\s*S\d+)*)\]$/);
      if (!match) return part;
      return match[1].split(/\s*,\s*/).map((marker, j) => (
        <span
          key={`${key}-${i}-${j}`}
          className="citation-marker"
          data-marker={marker}
          role="button"
          tabIndex={0}
          title={`Jump to source ${marker}`}
        >
          {marker}
        </span>
      ));
    });
  });
}

function mapStrings(
  node: React.ReactNode,
  fn: (text: string, key: string) => React.ReactNode,
): React.ReactNode {
  if (typeof node === "string") return fn(node, "s");
  if (Array.isArray(node)) return node.map((child, i) =>
    typeof child === "string" ? <span key={i}>{fn(child, String(i))}</span> : child,
  );
  return node;
}

function ErrorCard({ error }: { error: ApiErrorBody }) {
  const hint = error.detail?.hint as string | undefined;
  return (
    <div className="rounded-2xl rounded-bl-md border border-danger/40 bg-danger-soft px-4 py-3" role="alert">
      <p className="text-[13px] font-semibold text-danger">{error.message}</p>
      {hint && <p className="mt-1 text-[13px] text-muted">{hint}</p>}
      {error.request_id && (
        <p className="mt-2 font-mono text-[11px] text-muted">
          request {error.request_id}
        </p>
      )}
    </div>
  );
}
