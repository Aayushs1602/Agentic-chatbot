-- 001_init — corpus, sessions, messages, artifacts, observability.
--
-- Migrations are plain SQL applied in filename order by app/db/migrate.py and
-- recorded in schema_migrations. No ORM and no Alembic: the hybrid retrieval
-- query mixes pgvector operators with tsvector ranking, which reads far better
-- as SQL, and this file doubles as the schema documentation in architecture.md.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- ────────────────────────────────────────────────────────────────────────
-- Corpus
-- ────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS episodes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            text NOT NULL UNIQUE,          -- guest folder name in the repo
    title           text NOT NULL,
    guests          text[] NOT NULL DEFAULT '{}',
    youtube_url     text,
    video_id        text,
    published_on    date,
    duration_s      integer,
    description     text,
    source_path     text NOT NULL,
    -- Hash of the raw transcript file. Re-ingest skips unchanged episodes,
    -- which is what makes `python -m app.rag.ingest` idempotent and resumable.
    content_sha256  text NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS episodes_published_on_idx ON episodes (published_on DESC);

CREATE TABLE IF NOT EXISTS chunks (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id    uuid NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    ord           integer NOT NULL,                -- position within the episode
    text          text NOT NULL,
    token_count   integer NOT NULL,
    start_char    integer NOT NULL,
    end_char      integer NOT NULL,
    -- Nearest [HH:MM:SS] marker preceding the chunk, when the transcript has
    -- them. Powers youtube.com/watch?v=<video_id>&t=<start_seconds>s citations.
    start_seconds integer,
    embedding     vector(384) NOT NULL,
    -- Generated, so the sparse index can never drift from `text`.
    tsv           tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    UNIQUE (episode_id, ord)
);

-- Dense retriever. HNSW over cosine distance: better recall than IVFFlat at
-- this corpus size (~40k rows) and no training step before it is usable.
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- Sparse retriever.
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_episode_idx ON chunks (episode_id);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    episodes_seen      integer NOT NULL DEFAULT 0,
    episodes_ingested  integer NOT NULL DEFAULT 0,
    episodes_skipped   integer NOT NULL DEFAULT 0,
    chunks_written     integer NOT NULL DEFAULT 0,
    status             text NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'ok', 'failed')),
    error              text
);

-- ────────────────────────────────────────────────────────────────────────
-- Conversations
-- ────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sessions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title       text NOT NULL DEFAULT 'New chat',
    user_id     text,                              -- anonymous cookie; no auth
    provider    text,                              -- provider at creation time
    model       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb -- user agent, locale, timezone
);

CREATE INDEX IF NOT EXISTS sessions_updated_at_idx ON sessions (updated_at DESC);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id);

CREATE TABLE IF NOT EXISTS messages (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- ON DELETE CASCADE + every read scoped by session_id is what enforces
    -- session isolation; there is a test asserting no cross-session leakage.
    session_id    uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role          text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content       text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    provider      text,
    model         text,
    intent        text,                            -- router decision
    latency_ms    integer,
    tokens_in     integer,
    tokens_out    integer,
    -- [{marker, chunk_id, episode_id, title, score, youtube_url}]
    citations     jsonb NOT NULL DEFAULT '[]'::jsonb,
    finish_reason text,
    error         jsonb
);

CREATE INDEX IF NOT EXISTS messages_session_created_idx
    ON messages (session_id, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id        uuid REFERENCES messages(id) ON DELETE SET NULL,
    kind              text NOT NULL CHECK (kind IN ('markdown', 'html')),
    title             text NOT NULL DEFAULT 'Untitled',
    -- Both forms are kept: `raw` backs the "view source" tab, `sanitized` is
    -- the only thing ever rendered, and `sanitizer_report` records what was
    -- stripped so the viewer can show the user what it blocked and why.
    content_raw       text NOT NULL,
    content_sanitized text NOT NULL,
    sanitizer_report  jsonb NOT NULL DEFAULT '{}'::jsonb,
    version           integer NOT NULL DEFAULT 1,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS artifacts_session_idx ON artifacts (session_id, created_at DESC);

-- Observability: one row per tool invocation, on every provider path, so the
-- agent's behaviour is inspectable after the fact regardless of which provider
-- served the request.
CREATE TABLE IF NOT EXISTS tool_calls (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id     uuid REFERENCES messages(id) ON DELETE CASCADE,
    session_id     uuid REFERENCES sessions(id) ON DELETE CASCADE,
    name           text NOT NULL,
    args           jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    duration_ms    integer,
    ok             boolean NOT NULL DEFAULT true,
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tool_calls_message_idx ON tool_calls (message_id);
