"""Application configuration.

Every setting is env-driven with a working default, so the stack boots with an
unedited `.env.example`. Validation happens at import time and fails loudly:
a bad config should stop the process, not surface as a confusing 500 later.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["ollama", "cloud", "anthropic"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env is shared with docker-compose and the frontend
        case_sensitive=False,
    )

    # ── Core ────────────────────────────────────────────────────────────
    app_env: str = "local"
    app_version: str = "0.1.0"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    api_port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ── Database ────────────────────────────────────────────────────────
    database_url: str = "postgresql://lenny:lenny@db:5432/lenny"
    db_pool_min: int = 1
    db_pool_max: int = 10

    # ── Provider selection ──────────────────────────────────────────────
    llm_provider: ProviderName = "ollama"
    provider_fallback: bool = True
    provider_fallback_order: str = "ollama,cloud,anthropic"

    # ── Ollama ──────────────────────────────────────────────────────────
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen2.5:3b-instruct-q4_K_M"
    ollama_num_ctx: int = 8192
    ollama_timeout_s: float = 120.0
    ollama_temperature: float = 0.3

    # ── Cloud (any OpenAI-compatible endpoint; Gemini by default) ───────
    cloud_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    cloud_model: str = "gemini-2.0-flash"
    cloud_api_key: str = ""
    cloud_timeout_s: float = 60.0

    # ── Anthropic Claude Agent SDK ──────────────────────────────────────
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # ── Embeddings ──────────────────────────────────────────────────────
    embeddings_provider: Literal["fastembed", "ollama"] = "fastembed"
    embeddings_model: str = "snowflake/snowflake-arctic-embed-xs"
    embeddings_dim: int = 384
    embeddings_batch: int = 64
    ollama_embed_model: str = "nomic-embed-text"

    # ── Retrieval ───────────────────────────────────────────────────────
    retrieval_top_k: int = 5
    retrieval_candidates: int = 40
    retrieval_max_per_episode: int = 3
    retrieval_rrf_k: int = 60
    retrieval_min_sim: float = Field(default=0.35, ge=0.0, le=1.0)

    # ── Corpus ──────────────────────────────────────────────────────────
    transcripts_repo: str = "https://github.com/ChatPRD/lennys-podcast-transcripts"
    transcripts_dir: str = "/data/transcripts"
    chunk_tokens: int = 400
    chunk_overlap: int = 80

    # ── Derived ─────────────────────────────────────────────────────────

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def fallback_order(self) -> List[str]:
        return [p.strip() for p in self.provider_fallback_order.split(",") if p.strip()]

    @property
    def asyncpg_dsn(self) -> str:
        """asyncpg rejects the SQLAlchemy-style ``postgresql+driver://`` prefix."""
        dsn = self.database_url
        if "+" in dsn.split("://", 1)[0]:
            scheme, rest = dsn.split("://", 1)
            dsn = scheme.split("+", 1)[0] + "://" + rest
        return dsn

    # ── Validation ──────────────────────────────────────────────────────

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_fits_in_chunk(cls, v: int, info) -> int:
        chunk = info.data.get("chunk_tokens", 800)
        if v >= chunk:
            raise ValueError(
                f"chunk_overlap ({v}) must be smaller than chunk_tokens ({chunk}); "
                "otherwise chunking never advances and ingestion loops forever."
            )
        return v

    @field_validator("embeddings_dim")
    @classmethod
    def _dim_matches_known_model(cls, v: int, info) -> int:
        # The embedding column is fixed-width in SQL, so a mismatch corrupts the
        # index rather than raising. Catch the common cases up front.
        known = {
            "snowflake/snowflake-arctic-embed-xs": 384,
            "snowflake/snowflake-arctic-embed-s": 384,
            "BAAI/bge-small-en-v1.5": 384,
            "BAAI/bge-small-en": 384,
            "sentence-transformers/all-MiniLM-L6-v2": 384,
            "nomic-embed-text": 768,
        }
        model = info.data.get("embeddings_model")
        expected = known.get(model)
        if expected is not None and expected != v:
            raise ValueError(
                f"EMBEDDINGS_DIM={v} does not match {model} ({expected}). "
                "Fix the dimension and re-ingest — changing it requires rebuilding "
                "the vector index."
            )
        return v

    @field_validator("chunk_tokens")
    @classmethod
    def _chunk_fits_the_encoder(cls, v: int, info) -> int:
        """Chunks must fit inside the embedding model's context window.

        Sentence-transformer encoders truncate silently at their max sequence
        length. An 800-token chunk fed to a 512-token encoder is only embedded
        for its first ~512 tokens, so a third of the text is retrievable by
        keyword search but invisible to vector search — a quiet, hard-to-notice
        recall hole. The 0.8 factor leaves headroom for the word-based token
        estimate in the chunker, which is approximate by design.
        """
        max_seq = {
            "snowflake/snowflake-arctic-embed-xs": 512,
            "snowflake/snowflake-arctic-embed-s": 512,
            "BAAI/bge-small-en-v1.5": 512,
            "BAAI/bge-small-en": 512,
            "sentence-transformers/all-MiniLM-L6-v2": 256,
            "nomic-embed-text": 8192,
        }.get(info.data.get("embeddings_model"))
        if max_seq is not None and v > max_seq * 0.8:
            raise ValueError(
                f"CHUNK_TOKENS={v} is too large for "
                f"{info.data.get('embeddings_model')} (max sequence {max_seq}). "
                f"Use {int(max_seq * 0.8)} or fewer — the encoder would silently "
                "truncate the rest of every chunk."
            )
        return v

    @field_validator("retrieval_top_k")
    @classmethod
    def _top_k_within_candidates(cls, v: int, info) -> int:
        candidates = info.data.get("retrieval_candidates", 40)
        if v > candidates:
            raise ValueError(
                f"RETRIEVAL_TOP_K ({v}) exceeds RETRIEVAL_CANDIDATES ({candidates})."
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
