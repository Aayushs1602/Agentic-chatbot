import type {
  ApiErrorBody,
  Message,
  ProvidersResponse,
  Readiness,
  Session,
  StreamEvent,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE_URL ?? "";

export class ApiError extends Error {
  constructor(readonly body: ApiErrorBody, readonly status: number) {
    super(body.message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    // Sends the anonymous user cookie, which scopes the session list.
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });

  if (!response.ok) {
    // The backend returns a structured envelope for every non-2xx. Fall back
    // only when something upstream (a proxy, a crash) returns non-JSON.
    let body: ApiErrorBody;
    try {
      body = (await response.json()).error;
    } catch {
      body = { code: "network_error", message: `${response.status} ${response.statusText}` };
    }
    throw new ApiError(body, response.status);
  }

  return response.status === 204 ? (undefined as T) : response.json();
}

export const api = {
  readiness: () => request<Readiness>("/readyz"),
  providers: () => request<ProvidersResponse>("/api/providers"),
  setProvider: (provider: string) =>
    request<unknown>("/api/providers/active", {
      method: "POST",
      body: JSON.stringify({ provider }),
    }),

  listSessions: () => request<{ sessions: Session[] }>("/api/sessions"),
  createSession: () =>
    request<Session>("/api/sessions", { method: "POST", body: JSON.stringify({}) }),
  deleteSession: (id: string) => request<void>(`/api/sessions/${id}`, { method: "DELETE" }),
  renameSession: (id: string, title: string) =>
    request<Session>(`/api/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  listMessages: (id: string) =>
    request<{ messages: Message[] }>(`/api/sessions/${id}/messages`),
};

/**
 * Stream a reply.
 *
 * Uses fetch + a manual SSE parser rather than EventSource, because EventSource
 * cannot issue a POST and cannot send a JSON body. The parser handles the one
 * thing that actually breaks naive implementations: an event can be split
 * across chunk boundaries mid-frame, so bytes are buffered until a complete
 * `\n\n`-terminated frame is available.
 *
 * Returns an abort function so navigating away stops generation server-side —
 * an abandoned tab otherwise keeps a 4GB GPU busy for a minute.
 */
export function streamMessage(
  sessionId: string,
  content: string,
  handlers: {
    onEvent: (event: StreamEvent) => void;
    onDone?: () => void;
  },
  provider?: string,
): () => void {
  const controller = new AbortController();

  (async () => {
    try {
      const response = await fetch(`${BASE}/api/sessions/${sessionId}/messages`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, provider }),
        signal: controller.signal,
      });

      if (!response.ok || !response.body) {
        let body: ApiErrorBody;
        try {
          body = (await response.json()).error;
        } catch {
          body = { code: "network_error", message: `${response.status} ${response.statusText}` };
        }
        handlers.onEvent({ type: "error", error: body });
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // Frames are separated by a blank line. Anything after the last one is
        // an incomplete frame and stays buffered for the next chunk.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          const parsed = parseFrame(frame);
          if (parsed) handlers.onEvent(parsed);
        }
      }
    } catch (error) {
      // An intentional abort is not a failure worth showing the user.
      if ((error as Error).name === "AbortError") return;
      handlers.onEvent({
        type: "error",
        error: {
          code: "network_error",
          message: "Lost connection to the assistant.",
          detail: { hint: "Is the backend still running?" },
        },
      });
    } finally {
      handlers.onDone?.();
    }
  })();

  return () => controller.abort();
}

function parseFrame(frame: string): StreamEvent | null {
  let name = "";
  const dataLines: string[] = [];

  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) name = line.slice(7).trim();
    else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
  }
  if (!name || dataLines.length === 0) return null;

  let data: Record<string, never>;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch {
    return null;
  }

  switch (name) {
    case "meta":
    case "token":
    case "replace":
    case "citations":
    case "done":
    case "error":
      return { type: name, ...data } as unknown as StreamEvent;
    case "tool":
      return { type: "tool", step: data as never } as StreamEvent;
    default:
      // Unknown event names are ignored rather than thrown, so adding a new
      // backend event never breaks an older frontend build.
      return null;
  }
}
