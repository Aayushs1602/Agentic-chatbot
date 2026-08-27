import type { Session } from "../lib/types";

export function SessionSidebar({
  sessions,
  activeId,
  open,
  onSelect,
  onCreate,
  onDelete,
  onClose,
}: {
  sessions: Session[];
  activeId: string | null;
  open: boolean;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
  onClose: () => void;
}) {
  return (
    <>
      {/* Mobile scrim. Hidden from the tree because the close button below is
          the accessible way out. */}
      {open && (
        <div
          className="fixed inset-0 z-20 bg-black/40 md:hidden"
          onClick={onClose}
          aria-hidden
        />
      )}

      <aside
        aria-label="Chat history"
        className={[
          "fixed inset-y-0 left-0 z-30 flex w-64 flex-col border-r border-border bg-surface-2",
          "transition-transform md:static md:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        ].join(" ")}
      >
        <div className="flex items-center gap-2 p-3">
          <button
            type="button"
            onClick={onCreate}
            className="flex-1 rounded-lg border border-border bg-surface px-3 py-2 text-[13px] font-medium hover:border-accent"
          >
            + New chat
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-border px-2 py-2 text-[13px] md:hidden"
            aria-label="Close chat history"
          >
            ✕
          </button>
        </div>

        <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
          {sessions.length === 0 ? (
            <p className="px-2 py-3 text-[13px] text-muted">No chats yet.</p>
          ) : (
            <ul className="space-y-0.5">
              {sessions.map((s) => (
                <li key={s.id} className="group relative">
                  <button
                    type="button"
                    onClick={() => onSelect(s.id)}
                    aria-current={s.id === activeId ? "page" : undefined}
                    className={[
                      "w-full truncate rounded-lg px-2.5 py-2 pr-8 text-left text-[13px]",
                      s.id === activeId
                        ? "bg-accent-soft font-medium text-text"
                        : "text-muted hover:bg-surface hover:text-text",
                    ].join(" ")}
                    title={s.title}
                  >
                    {s.title}
                  </button>
                  <button
                    type="button"
                    onClick={() => onDelete(s.id)}
                    aria-label={`Delete chat: ${s.title}`}
                    /* Visible on hover for mice, and always reachable by keyboard
                       via focus-within — a hover-only control is unusable without
                       a pointer. */
                    className="absolute right-1 top-1.5 rounded p-1 text-[12px] text-muted opacity-0 transition-opacity hover:text-danger focus:opacity-100 group-hover:opacity-100"
                  >
                    ✕
                  </button>
                </li>
              ))}
            </ul>
          )}
        </nav>
      </aside>
    </>
  );
}
