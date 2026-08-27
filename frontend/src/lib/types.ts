export type Role = "user" | "assistant" | "system";

export interface Citation {
  marker: string;
  chunk_id: string;
  episode_id: string;
  title: string;
  guests: string[];
  url: string | null;
  start_seconds: number | null;
  score: number;
}

export interface Message {
  id: string;
  session_id: string;
  role: Role;
  content: string;
  created_at: string;
  provider: string | null;
  model: string | null;
  intent: string | null;
  latency_ms: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  citations: Citation[];
  finish_reason: string | null;
  error: ApiErrorBody | null;
}

export interface Session {
  id: string;
  title: string;
  provider: string | null;
  model: string | null;
  created_at: string;
  updated_at: string;
  message_count?: number;
}

export interface Provider {
  id: string;
  label: string;
  model: string;
  available: boolean;
  reason?: string;
  hint?: string;
}

export interface ProvidersResponse {
  active: string;
  fallback_enabled: boolean;
  fallback_order: string[];
  providers: Provider[];
}

export interface SanitizerReport {
  total_removed: number;
  clean: boolean;
  removed_tags: Record<string, number>;
  removed_attributes: Record<string, number>;
  removed_urls: string[];
  notes: string[];
  policy: Record<string, unknown>;
}

export interface Artifact {
  id: string;
  session_id: string;
  message_id: string | null;
  kind: "html" | "markdown";
  title: string;
  /** Always the sanitized form. Raw output is only at /artifacts/:id/raw. */
  content: string;
  sanitizer_report: SanitizerReport;
  version: number;
  created_at: string;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  detail?: Record<string, unknown>;
  request_id?: string;
}

/**
 * One step the agent took. Rendered live so the user can see *why* an answer
 * looks the way it does — especially why it abstained.
 */
export interface AgentStep {
  name: string;
  summary: Record<string, unknown>;
  ok: boolean;
}

export interface Readiness {
  status: "ready" | "degraded" | "not_ready";
  corpus: { episodes: number; chunks: number };
  database: { reachable: boolean };
  provider: { active: string };
  providers: Provider[];
  degraded: string[];
}

/** Mirrors the backend's SSE event contract exactly. */
export type StreamEvent =
  | { type: "meta"; provider: string; model: string; fell_back_from: string | null }
  | { type: "tool"; step: AgentStep }
  | { type: "token"; text: string }
  | { type: "replace"; text: string }
  | { type: "citations"; citations: Citation[] }
  | ({ type: "artifact" } & Artifact)
  | {
      type: "done";
      message_id: string;
      intent: string;
      abstained: boolean;
      finish_reason: string;
      latency_ms: number;
      usage: { tokens_in: number; tokens_out: number };
    }
  | { type: "error"; error: ApiErrorBody };
