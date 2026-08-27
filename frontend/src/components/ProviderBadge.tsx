import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { ProvidersResponse, Readiness } from "../lib/types";

/**
 * Which model is answering, and is it healthy.
 *
 * The brief requires the selected provider to be visible and switchable without
 * code changes. Unavailable providers stay listed but disabled, each showing
 * *why* and what to do about it — a greyed-out option with no explanation is
 * how people conclude a product is broken.
 */
export function ProviderBadge({
  readiness,
  onSwitched,
}: {
  readiness: Readiness | null;
  onSwitched: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ProvidersResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    api.providers().then(setData).catch(() => setData(null));

    const onClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const active = readiness?.providers.find((p) => p.id === readiness.provider.active);
  const healthy = active?.available ?? false;

  async function choose(id: string) {
    setBusy(true);
    try {
      await api.setProvider(id);
      onSwitched();
      setOpen(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="listbox"
        className="flex items-center gap-2 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[13px] hover:bg-surface-2"
      >
        <span
          aria-hidden
          className={`size-2 rounded-full ${healthy ? "bg-ok" : "bg-danger"}`}
        />
        <span className="max-w-[13rem] truncate">
          {active?.model ?? readiness?.provider.active ?? "no provider"}
        </span>
        <span className="sr-only">
          {healthy ? "provider healthy" : `provider unavailable: ${active?.reason ?? ""}`}
        </span>
        <span aria-hidden className="text-muted">▾</span>
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute right-0 z-20 mt-1.5 w-80 rounded-xl border border-border bg-surface p-1.5 shadow-lg"
        >
          {(data?.providers ?? readiness?.providers ?? []).map((p) => {
            const isActive = p.id === (data?.active ?? readiness?.provider.active);
            return (
              <button
                key={p.id}
                type="button"
                role="option"
                aria-selected={isActive}
                disabled={!p.available || busy}
                onClick={() => choose(p.id)}
                className={[
                  "flex w-full flex-col items-start gap-0.5 rounded-lg px-2.5 py-2 text-left",
                  p.available ? "hover:bg-surface-2" : "cursor-not-allowed opacity-55",
                  isActive ? "bg-accent-soft" : "",
                ].join(" ")}
              >
                <span className="flex w-full items-center gap-2 text-[13px] font-medium">
                  <span
                    aria-hidden
                    className={`size-2 shrink-0 rounded-full ${p.available ? "bg-ok" : "bg-danger"}`}
                  />
                  {p.label}
                  {isActive && <span className="ml-auto text-[11px] text-accent">active</span>}
                </span>
                <span className="pl-4 font-mono text-[11px] text-muted">{p.model}</span>
                {!p.available && (
                  <span className="pl-4 text-[12px] text-muted">
                    {p.reason}
                    {p.hint ? ` — ${p.hint}` : ""}
                  </span>
                )}
              </button>
            );
          })}

          {data?.fallback_enabled && (
            <p className="border-t border-border px-2.5 pb-1 pt-2 text-[11px] text-muted">
              Falls back to the next healthy provider automatically.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
