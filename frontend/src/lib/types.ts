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

// ── Corpus inspector ────────────────────────────────────────────────────

export interface AdminStats {
  corpus: { episodes: number; chunks: number; avg_tokens: number; min_tokens: number; max_tokens: number };
  gaps: {
    episodes_without_date: number;
    episodes_without_duration: number;
    chunks_without_timestamp: number;
  };
  last_ingest: {
    started_at: string;
    finished_at: string | null;
    episodes_ingested: number;
    chunks_written: number;
    status: string;
  } | null;
}

export interface AdminEpisode {
  id: string;
  slug: string;
  title: string;
  guests: string[];
  published_on: string | null;
  duration_s: number | null;
  youtube_url: string | null;
  chunk_count: number;
  ingested_at: string | null;
}

export interface AdminChunk {
  id: string;
  ord: number;
  text: string;
  token_count: number;
  start_char: number;
  end_char: number;
  start_seconds: number | null;
  /** Quality problems, each corresponding to a defect that has shipped before. */
  flags: string[];
}

export interface EpisodeChunks {
  episode: AdminEpisode & { source_path: string; content_sha256: string };
  chunk_count: number;
  flagged: number;
  chunks: AdminChunk[];
}

export interface FlaggedChunks {
  scanned: number;
  counts: Record<string, number>;
  chunks: (AdminChunk & { episode_id: string; title: string })[];
}
