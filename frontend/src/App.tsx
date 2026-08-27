import { useCallback, useEffect, useRef, useState } from "react";
import { Composer } from "./components/Composer";
import { MessageBubble } from "./components/MessageBubble";
import { ProviderBadge } from "./components/ProviderBadge";
import { SessionSidebar } from "./components/SessionSidebar";
import { api, streamMessage } from "./lib/api";
import type {
  AgentStep,
  ApiErrorBody,
  Citation,
  Message,
  Readiness,
  Session,
} from "./lib/types";

/** The reply currently being streamed. Kept out of `messages` so a partial
 *  answer never looks like persisted history. */
interface Live {
  text: string;
  steps: AgentStep[];
  citations: Citation[];
  abstained: boolean;
  error: ApiErrorBody | null;
  provider?: string;
  model?: string;
}

const EMPTY_LIVE: Live = {
  text: "",
  steps: [],
  citations: [],
  abstained: false,
  error: null,
};

export default function App() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [live, setLive] = useState<Live | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [loading, setLoading] = useState(true);

  const abortRef = useRef<(() => void) | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  const refreshReadiness = useCallback(() => {
    api.readiness().then(setReadiness).catch(() => setReadiness(null));
  }, []);

  // Boot: readiness, sessions, and a session to land in.
  useEffect(() => {
    (async () => {
      refreshReadiness();
      try {
        const { sessions } = await api.listSessions();
        setSessions(sessions);
        if (sessions.length > 0) {
          setActiveId(sessions[0].id);
        } else {
          const created = await api.createSession();
          setSessions([created]);
          setActiveId(created.id);
        }
      } catch {
        /* the banner reports it; don't blank the UI */
      } finally {
        setLoading(false);
      }
    })();
  }, [refreshReadiness]);

  // Poll readiness so a provider or database going down surfaces without a reload.
  useEffect(() => {
    const id = setInterval(refreshReadiness, 20_000);
    return () => clearInterval(id);
  }, [refreshReadiness]);

  useEffect(() => {
    if (!activeId) return;
    setLive(null);
    api
      .listMessages(activeId)
      .then(({ messages }) => setMessages(messages))
      .catch(() => setMessages([]));
  }, [activeId]);

  // Follow the stream only while the user is already at the bottom — yanking
  // the view while they are reading earlier text is worse than not following.
  useEffect(() => {
    if (pinnedRef.current) {
      bottomRef.current?.scrollIntoView({ block: "end" });
    }
  }, [messages, live?.text, live?.steps.length]);

  useEffect(() => () => abortRef.current?.(), []);

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  async function newChat() {
    abortRef.current?.();
    const created = await api.createSession();
    setSessions((prev) => [created, ...prev]);
    setActiveId(created.id);
    setMessages([]);
    setSidebarOpen(false);
  }

  async function removeChat(id: string) {
    await api.deleteSession(id);
    const remaining = sessions.filter((s) => s.id !== id);
    setSessions(remaining);
    if (id === activeId) {
      if (remaining.length > 0) setActiveId(remaining[0].id);
      else await newChat();
    }
  }

  function send(text: string) {
    if (!activeId) return;
    pinnedRef.current = true;

    // Optimistic user turn: the input should never appear to be swallowed while
    // the first token is still ~1s away.
    setMessages((prev) => [
      ...prev,
      {
        id: `pending-${Date.now()}`,
        session_id: activeId,
        role: "user",
        content: text,
        created_at: new Date().toISOString(),
        provider: null, model: null, intent: null,
        latency_ms: null, tokens_in: null, tokens_out: null,
        citations: [], finish_reason: null, error: null,
      },
    ]);
    setLive({ ...EMPTY_LIVE });

    abortRef.current = streamMessage(activeId, text, {
      onEvent: (event) => {
        setLive((current) => {
          const l = current ?? { ...EMPTY_LIVE };
          switch (event.type) {
            case "meta":
              return { ...l, provider: event.provider, model: event.model };
            case "tool":
              return { ...l, steps: [...l.steps, event.step] };
            case "token":
              return { ...l, text: l.text + event.text };
            case "replace":
              // The answer cited nothing resolvable. Discard what streamed
              // rather than leave a plausible uncited answer on screen.
              return { ...l, text: event.text, abstained: true };
            case "citations":
              return { ...l, citations: event.citations };
            case "done":
              return { ...l, abstained: l.abstained || event.abstained };
            case "error":
              return { ...l, error: event.error };
            default:
              return l;
          }
        });
      },
      onDone: () => {
        abortRef.current = null;
        // Re-read from the server so what's on screen is what was persisted,
        // including the real message id and token accounting.
        if (activeId) {
          api
            .listMessages(activeId)
            .then(({ messages }) => {
              setMessages(messages);
              setLive(null);
            })
            .catch(() => setLive(null));
          api.listSessions().then(({ sessions }) => setSessions(sessions)).catch(() => {});
        }
      },
    });
  }

  function stop() {
    abortRef.current?.();
    abortRef.current = null;
  }

  const streaming = live !== null && live.error === null;
  const corpusEmpty = readiness !== null && readiness.corpus.chunks === 0;
  const dbDown = readiness !== null && !readiness.database.reachable;
  const blocked = corpusEmpty || dbDown;

  return (
    <div className="flex h-full">
      <SessionSidebar
        sessions={sessions}
        activeId={activeId}
        open={sidebarOpen}
        onSelect={(id) => {
          setActiveId(id);
          setSidebarOpen(false);
        }}
        onCreate={newChat}
        onDelete={removeChat}
        onClose={() => setSidebarOpen(false)}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-border px-4 py-2.5">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            className="rounded-lg border border-border px-2 py-1.5 text-[13px] md:hidden"
            aria-label="Open chat history"
          >
            ☰
          </button>

          <div className="min-w-0 flex-1">
            <h1 className="truncate text-[15px] font-semibold">The Lenny Growth Assistant</h1>
            {readiness && (
              <p className="text-[11.5px] text-muted">
                {readiness.corpus.episodes} episodes · {readiness.corpus.chunks.toLocaleString()} passages
              </p>
            )}
          </div>

          <ProviderBadge readiness={readiness} onSwitched={refreshReadiness} />
        </header>

        {blocked && <Banner corpusEmpty={corpusEmpty} dbDown={dbDown} />}

        <main
          ref={scrollRef}
          onScroll={onScroll}
          className="min-h-0 flex-1 overflow-y-auto px-4 py-5"
        >
          <div className="mx-auto flex max-w-3xl flex-col gap-5">
            {loading ? (
              <p className="text-[14px] text-muted">Loading…</p>
            ) : messages.length === 0 && !live ? (
              <EmptyState />
            ) : (
              messages.map((m) => (
                <MessageBubble
                  key={m.id}
                  role={m.role === "user" ? "user" : "assistant"}
                  content={m.content}
                  citations={m.citations}
                  abstained={m.finish_reason === "abstain"}
                  error={m.error}
                  meta={{ provider: m.provider, model: m.model, latencyMs: m.latency_ms }}
                />
              ))
            )}

            {live && (
              <MessageBubble
                role="assistant"
                content={live.text}
                citations={live.citations}
                steps={live.steps}
                streaming={streaming && live.text.length > 0}
                abstained={live.abstained}
                error={live.error}
              />
            )}

            <div ref={bottomRef} />
          </div>
        </main>

        <Composer
          onSend={send}
          onStop={stop}
          streaming={streaming}
          disabled={blocked}
          showSuggestions={messages.length === 0}
        />
      </div>
    </div>
  );
}

function Banner({ corpusEmpty, dbDown }: { corpusEmpty: boolean; dbDown: boolean }) {
  return (
    <div role="status" className="border-b border-warn/35 bg-warn/10 px-4 py-2.5">
      <p className="mx-auto max-w-3xl text-[13px]">
        {dbDown ? (
          <>
            <strong>The database isn't reachable.</strong> Check{" "}
            <code className="rounded bg-surface-2 px-1">docker compose ps</code>.
          </>
        ) : corpusEmpty ? (
          <>
            <strong>No transcripts ingested yet.</strong> Run{" "}
            <code className="rounded bg-surface-2 px-1">make ingest LIMIT=20</code> to
            load a subset, or{" "}
            <code className="rounded bg-surface-2 px-1">make ingest</code> for the full
            corpus.
          </>
        ) : null}
      </p>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="py-10">
      <h2 className="text-[22px] font-semibold">Ask Lenny's guests.</h2>
      <p className="mt-2 max-w-lg text-[14.5px] leading-relaxed text-muted">
        Every answer is grounded in the podcast transcripts and cites the episode
        and timestamp it came from. When the corpus doesn't cover something, this
        assistant says so rather than guessing.
      </p>
    </div>
  );
}
