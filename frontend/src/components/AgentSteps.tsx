import type { AgentStep } from "../lib/types";

/**
 * The agent's reasoning trace, rendered live.
 *
 * This is the most important non-obvious piece of the UI. A grounded assistant
 * that refuses to answer looks broken unless the user can see *why* — that it
 * classified the question, searched 300 transcripts, found five passages, and
 * judged them insufficient. Showing the steps turns an apparent failure into a
 * legible decision, and turns the agent from a black box into something an
 * evaluator can audit without reading logs.
 */

const LABELS: Record<string, string> = {
  classify_intent: "Understanding the question",
  search_transcripts: "Searching transcripts",
  check_relevance: "Checking the sources answer it",
  apply_skill: "Applying skill",
  verify_citations: "Verifying citations",
};

function describe(step: AgentStep): string | null {
  const s = step.summary ?? {};
  switch (step.name) {
    case "classify_intent":
      return s.intent ? String(s.intent).replace(/_/g, " ") : null;
    case "search_transcripts":
      return s.chunks !== undefined
        ? `${s.chunks} passages from ${s.episodes} episodes`
        : null;
    case "check_relevance":
      return s.answerable ? "sources support an answer" : "sources don't answer it";
    case "apply_skill":
      return s.skill ? String(s.skill) : null;
    case "verify_citations": {
      const removed = (s.removed_invented as string[]) ?? [];
      return removed.length ? `removed ${removed.length} invented citation(s)` : "all citations resolved";
    }
    default:
      return null;
  }
}

export function AgentSteps({ steps, active }: { steps: AgentStep[]; active: boolean }) {
  if (steps.length === 0) return null;

  return (
    <ol className="mb-3 space-y-1.5 text-[13px]" aria-label="Agent steps">
      {steps.map((step, i) => {
        const isLast = i === steps.length - 1;
        const running = active && isLast;
        const detail = describe(step);
        return (
          <li key={`${step.name}-${i}`} className="flex items-start gap-2 text-muted">
            <span
              aria-hidden
              className={[
                "mt-[6px] size-1.5 shrink-0 rounded-full",
                !step.ok ? "bg-danger" : running ? "animate-pulse bg-accent" : "bg-ok",
              ].join(" ")}
            />
            <span>
              <span className="text-text/80">{LABELS[step.name] ?? step.name}</span>
              {detail && <span className="text-muted"> — {detail}</span>}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
