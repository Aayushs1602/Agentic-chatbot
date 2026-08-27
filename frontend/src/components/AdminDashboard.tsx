import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import type { AdminEpisode, AdminStats, EpisodeChunks, FlaggedChunks } from "../lib/types";

/**
 * Corpus inspection.
 *
 * Several real defects in this project were invisible in every metric and
 * obvious the moment someone read the chunks — passages starting mid-word,
 * sponsor reads outranking real content, timestamp markers being read as
 * durations. This is the view that makes that inspection a normal thing to do
 * rather than an archaeology exercise with psql.
 *
 * Read-only. There is no auth in this product, so nothing here mutates.
 */

type Tab = "overview" | "episodes" | "flagged";

export function AdminDashboard({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<Tab>("overview");
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.adminStats().then(setStats).catch((e) => setError(String(e)));
  }, []);

  return (
    <div className="flex h-full flex-col bg-bg">
      <header className="flex items-center gap-3 border-b border-border px-4 py-2.5">
        <div className="min-w-0 flex-1">
          <h1 className="text-[15px] font-semibold">Corpus inspector</h1>
          <p className="text-[11.5px] text-muted">
            What ingestion actually produced — read-only
          </p>
        </div>
        <nav className="flex rounded-lg border border-border p-0.5" role="tablist">
          {(["overview", "episodes", "flagged"] as Tab[]).map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={[
                "rounded-md px-3 py-1 text-[12.5px] capitalize",
                tab === t ? "bg-accent-soft text-accent" : "text-muted hover:text-text",
              ].join(" ")}
            >
              {t}
            </button>
          ))}
        </nav>
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg border border-border px-2.5 py-1.5 text-[12.5px] hover:bg-surface-2"
        >
          Back to chat
        </button>
      </header>

      {error && (
        <p className="border-b border-danger/40 bg-danger-soft px-4 py-2 text-[13px] text-danger">
          {error}
        </p>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === "overview" && <Overview stats={stats} />}
        {tab === "episodes" && <Episodes />}
        {tab === "flagged" && <Flagged />}
      </div>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string | number; tone?: "warn" | "ok" }) {
  return (
    <div className="rounded-xl border border-border bg-surface p-3">
      <p className="text-[11.5px] uppercase tracking-wide text-muted">{label}</p>
      <p
        className={[
          "mt-1 text-[22px] font-semibold tabular-nums",
          tone === "warn" ? "text-warn" : tone === "ok" ? "text-ok" : "",
        ].join(" ")}
      >
        {typeof value === "number" ? value.toLocaleString() : value}
      </p>
    </div>
  );
}

function Overview({ stats }: { stats: AdminStats | null }) {
  if (!stats) return <p className="p-4 text-[14px] text-muted">Loading…</p>;
  const { corpus, gaps, last_ingest } = stats;

  return (
    <div className="space-y-5 p-4">
      <section>
        <h2 className="mb-2 text-[13px] font-semibold">Corpus</h2>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="Episodes" value={corpus.episodes} />
          <Stat label="Passages" value={corpus.chunks} />
          <Stat label="Avg tokens" value={corpus.avg_tokens} />
          <Stat
            label="Max tokens"
            value={corpus.max_tokens}
            // The encoder truncates silently past 512, so this is the number
            // that quietly destroys recall if it ever drifts.
            tone={corpus.max_tokens > 480 ? "warn" : "ok"}
          />
        </div>
        <p className="mt-2 text-[12px] text-muted">
          Every passage must stay under the embedding model's 512-token window —
          past it the encoder truncates silently and the tail becomes invisible
          to vector search.
        </p>
      </section>

      <section>
        <h2 className="mb-2 text-[13px] font-semibold">Metadata gaps</h2>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          <Stat
            label="Episodes without a date"
            value={gaps.episodes_without_date}
            tone={gaps.episodes_without_date > 0 ? "warn" : "ok"}
          />
          <Stat
            label="Episodes without duration"
            value={gaps.episodes_without_duration}
            tone={gaps.episodes_without_duration > 0 ? "warn" : "ok"}
          />
          <Stat label="Passages without a timestamp" value={gaps.chunks_without_timestamp} />
        </div>
      </section>

      {last_ingest && (
        <section>
          <h2 className="mb-2 text-[13px] font-semibold">Last ingest</h2>
          <div className="rounded-xl border border-border bg-surface p-3 text-[13px]">
            <p>
              <span className="text-muted">Status</span>{" "}
              <span className={last_ingest.status === "ok" ? "text-ok" : "text-danger"}>
                {last_ingest.status}
              </span>
            </p>
            <p className="text-muted">
              {last_ingest.episodes_ingested} episodes ·{" "}
              {last_ingest.chunks_written?.toLocaleString()} passages written
            </p>
            <p className="mt-1 text-[11.5px] text-muted">
              {new Date(last_ingest.started_at).toLocaleString()}
            </p>
          </div>
        </section>
      )}
    </div>
  );
}

function formatDuration(seconds: number | null): string {
  if (!seconds) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

function Episodes() {
  const [query, setQuery] = useState("");
  const [order, setOrder] = useState("title");
  const [episodes, setEpisodes] = useState<AdminEpisode[]>([]);
  const [total, setTotal] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);

  useEffect(() => {
    // Debounced so typing doesn't fire a query per keystroke.
    const timer = setTimeout(() => {
      api
        .adminEpisodes({ q: query || undefined, order })
        .then((r) => {
          setEpisodes(r.episodes);
          setTotal(r.total);
        })
        .catch(() => setEpisodes([]));
    }, 250);
    return () => clearTimeout(timer);
  }, [query, order]);

  return (
    <div className="p-4">
      <div className="mb-3 flex flex-wrap gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter by title or guest…"
          aria-label="Filter episodes"
          className="min-w-0 flex-1 rounded-lg border border-border bg-surface px-3 py-2 text-[14px] focus:border-accent focus:outline-none"
        />
        <select
          value={order}
          onChange={(e) => setOrder(e.target.value)}
          aria-label="Sort episodes"
          className="rounded-lg border border-border bg-surface px-2.5 py-2 text-[13px]"
        >
          <option value="title">Title</option>
          <option value="duration">Longest</option>
          <option value="date">Newest</option>
          <option value="chunks">Most passages</option>
        </select>
      </div>

      <p className="mb-2 text-[12px] text-muted">
        {episodes.length} of {total.toLocaleString()} episodes
      </p>

      <ul className="space-y-1.5">
        {episodes.map((ep) => (
          <li key={ep.id} className="rounded-xl border border-border bg-surface">
            <button
              type="button"
              onClick={() => setOpenId(openId === ep.id ? null : ep.id)}
              aria-expanded={openId === ep.id}
              className="flex w-full items-start gap-3 p-3 text-left"
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13.5px] font-medium">{ep.title}</span>
                <span className="mt-0.5 block text-[12px] text-muted">
                  {ep.guests.join(", ") || "unknown guest"} · {formatDuration(ep.duration_s)} ·{" "}
                  {ep.published_on ?? "no date"} · {ep.chunk_count} passages
                </span>
              </span>
              <span aria-hidden className="text-muted">
                {openId === ep.id ? "▾" : "▸"}
              </span>
            </button>
            {openId === ep.id && <ChunkList episodeId={ep.id} />}
          </li>
        ))}
      </ul>
    </div>
  );
}

const FLAG_LABEL: Record<string, string> = {
  ad: "sponsor read",
  "starts-midword": "starts mid-word",
  "no-timestamp": "no timestamp",
  "very-short": "very short",
};

function Flag({ name }: { name: string }) {
  const bad = name !== "no-timestamp";
  return (
    <span
      className={[
        "rounded px-1.5 py-0.5 text-[10.5px] font-medium",
        bad ? "bg-danger-soft text-danger" : "bg-surface-2 text-muted",
      ].join(" ")}
    >
      {FLAG_LABEL[name] ?? name}
    </span>
  );
}

function ChunkList({ episodeId }: { episodeId: string }) {
  const [data, setData] = useState<EpisodeChunks | null>(null);
  const [onlyFlagged, setOnlyFlagged] = useState(false);

  useEffect(() => {
    api.adminEpisodeChunks(episodeId).then(setData).catch(() => setData(null));
  }, [episodeId]);

  const shown = useMemo(() => {
    if (!data) return [];
    return onlyFlagged ? data.chunks.filter((c) => c.flags.length) : data.chunks;
  }, [data, onlyFlagged]);

  if (!data) return <p className="px-3 pb-3 text-[13px] text-muted">Loading passages…</p>;

  return (
    <div className="border-t border-border px-3 py-2">
      <div className="mb-2 flex flex-wrap items-center gap-3 text-[12px] text-muted">
        <span>
          {data.chunk_count} passages
          {data.flagged > 0 && <span className="text-warn"> · {data.flagged} flagged</span>}
        </span>
        <label className="flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={onlyFlagged}
            onChange={(e) => setOnlyFlagged(e.target.checked)}
          />
          flagged only
        </label>
        <span className="font-mono">{data.episode.source_path}</span>
      </div>

      <ol className="space-y-1.5">
        {shown.map((c) => (
          <li key={c.id} className="rounded-lg bg-surface-2 p-2.5 text-[12.5px]">
            <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-muted">
              <span className="font-mono">#{c.ord}</span>
              <span>{c.token_count} tokens</span>
              <span>
                chars {c.start_char}–{c.end_char}
              </span>
              {c.start_seconds !== null && <span>at {c.start_seconds}s</span>}
              {c.flags.map((f) => (
                <Flag key={f} name={f} />
              ))}
            </div>
            <p className="whitespace-pre-wrap leading-relaxed">{c.text}</p>
          </li>
        ))}
      </ol>
    </div>
  );
}

function Flagged() {
  const [data, setData] = useState<FlaggedChunks | null>(null);

  useEffect(() => {
    api.adminFlagged().then(setData).catch(() => setData(null));
  }, []);

  if (!data) return <p className="p-4 text-[14px] text-muted">Scanning…</p>;

  const actionable = data.chunks.length;

  return (
    <div className="space-y-4 p-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Scanned" value={data.scanned} />
        {Object.entries(data.counts).map(([flag, count]) => (
          <Stat
            key={flag}
            label={FLAG_LABEL[flag] ?? flag}
            value={count}
            tone={flag === "no-timestamp" ? undefined : "warn"}
          />
        ))}
      </div>

      {actionable === 0 ? (
        <p className="rounded-xl border border-ok/40 bg-ok/10 p-3 text-[13px]">
          Nothing needs attention. No sponsor reads, no passages starting mid-word,
          no truncated fragments.
        </p>
      ) : (
        <ol className="space-y-2">
          {data.chunks.map((c) => (
            <li key={c.id} className="rounded-xl border border-border bg-surface p-3">
              <div className="mb-1 flex flex-wrap items-center gap-2 text-[11.5px] text-muted">
                <span className="truncate font-medium text-text">{c.title}</span>
                <span className="font-mono">#{c.ord}</span>
                {c.flags.map((f) => (
                  <Flag key={f} name={f} />
                ))}
              </div>
              <p className="text-[12.5px] leading-relaxed">{c.text}</p>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
