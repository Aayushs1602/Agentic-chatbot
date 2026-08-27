import { useEffect, useRef, useState } from "react";

const SUGGESTIONS = [
  "How do I know when I've found product-market fit?",
  "When should a startup hire its first product manager?",
  "How do you pick a north star metric that doesn't get gamed?",
  "Turn that into a Ship 30 for 30 essay",
];

export function Composer({
  onSend,
  onStop,
  streaming,
  disabled,
  showSuggestions,
}: {
  onSend: (text: string) => void;
  onStop: () => void;
  streaming: boolean;
  disabled?: boolean;
  showSuggestions?: boolean;
}) {
  const [value, setValue] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  // Return focus to the composer when a reply finishes, so a keyboard user can
  // keep typing without hunting for the input.
  useEffect(() => {
    if (!streaming) ref.current?.focus();
  }, [streaming]);

  // Grow with content, capped so the conversation stays visible.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [value]);

  function submit(text: string) {
    const trimmed = text.trim();
    if (!trimmed || streaming || disabled) return;
    onSend(trimmed);
    setValue("");
  }

  return (
    <div className="border-t border-border bg-bg px-4 py-3">
      {showSuggestions && !streaming && (
        <div className="mx-auto mb-2.5 flex max-w-3xl flex-wrap gap-1.5">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => submit(s)}
              disabled={disabled}
              className="rounded-full border border-border bg-surface px-3 py-1.5 text-[12.5px] text-muted hover:border-accent hover:text-text disabled:opacity-50"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      <form
        className="mx-auto flex max-w-3xl items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          submit(value);
        }}
      >
        <label htmlFor="composer" className="sr-only">
          Ask about product, growth, hiring, or pricing
        </label>
        <textarea
          id="composer"
          ref={ref}
          rows={1}
          value={value}
          disabled={disabled}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends, Shift+Enter breaks the line — the convention people
            // already have muscle memory for.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit(value);
            }
          }}
          placeholder={
            disabled
              ? "The assistant is unavailable — see the banner above"
              : "Ask about product, growth, hiring, pricing…"
          }
          className="flex-1 resize-none rounded-xl border border-border bg-surface px-3.5 py-2.5 text-[15px] leading-relaxed placeholder:text-muted focus:border-accent focus:outline-none disabled:opacity-60"
        />

        {streaming ? (
          <button
            type="button"
            onClick={onStop}
            className="shrink-0 rounded-xl border border-border bg-surface px-4 py-2.5 text-[14px] font-medium hover:bg-surface-2"
          >
            Stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={!value.trim() || disabled}
            className="shrink-0 rounded-xl bg-accent px-4 py-2.5 text-[14px] font-medium text-white disabled:opacity-40"
          >
            Send
          </button>
        )}
      </form>

      <p className="mx-auto mt-1.5 max-w-3xl text-[11px] text-muted">
        Answers come only from Lenny's Podcast transcripts, with sources. If the
        corpus doesn't cover something, it says so.
      </p>
    </div>
  );
}
