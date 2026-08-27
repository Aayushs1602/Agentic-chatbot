"""Configuration validation.

Every check here exists because the failure it prevents is expensive and
non-obvious: a silent infinite loop, a corrupted vector index, or an opaque
asyncpg error thousands of rows into an ingest.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.config import Settings


def make(**overrides) -> Settings:
    base = dict(_env_file=None)  # ignore any real .env on the machine
    base.update(overrides)
    return Settings(**base)


class TestValidation:
    def test_defaults_are_valid(self):
        assert make().embeddings_dim == 384

    def test_overlap_must_be_smaller_than_chunk(self):
        # Otherwise the chunk window never advances and ingest loops forever.
        with pytest.raises(PydanticValidationError, match="smaller than"):
            make(chunk_tokens=400, chunk_overlap=400)

    def test_embedding_dim_must_match_the_model(self):
        # The vector column is fixed-width; a mismatch corrupts the index.
        with pytest.raises(PydanticValidationError, match="does not match"):
            make(embeddings_model="BAAI/bge-small-en-v1.5", embeddings_dim=768)

    def test_known_alternative_model_dim_is_accepted(self):
        assert make(embeddings_model="nomic-embed-text", embeddings_dim=768).embeddings_dim == 768

    def test_unknown_model_skips_the_dim_check(self):
        # Don't block someone bringing their own embedding model.
        assert make(embeddings_model="some/custom-model", embeddings_dim=1024).embeddings_dim == 1024

    def test_chunk_tokens_cannot_exceed_the_encoder_window(self):
        # Regression guard: 800-token chunks fed to a 512-token encoder were
        # silently truncated, making a third of every chunk invisible to vector
        # search while still present in keyword search.
        with pytest.raises(PydanticValidationError, match="too large for"):
            make(embeddings_model="snowflake/snowflake-arctic-embed-xs", chunk_tokens=800)

    def test_chunk_tokens_within_the_window_is_accepted(self):
        assert make(
            embeddings_model="snowflake/snowflake-arctic-embed-xs", chunk_tokens=400
        ).chunk_tokens == 400

    def test_short_window_models_demand_smaller_chunks(self):
        with pytest.raises(PydanticValidationError, match="too large for"):
            make(embeddings_model="sentence-transformers/all-MiniLM-L6-v2", chunk_tokens=400)

    def test_long_window_models_allow_large_chunks(self):
        assert make(
            embeddings_model="nomic-embed-text", embeddings_dim=768, chunk_tokens=2000
        ).chunk_tokens == 2000

    def test_top_k_cannot_exceed_candidates(self):
        with pytest.raises(PydanticValidationError, match="exceeds"):
            make(retrieval_top_k=50, retrieval_candidates=40)

    def test_similarity_threshold_is_bounded(self):
        with pytest.raises(PydanticValidationError):
            make(retrieval_min_sim=1.5)


class TestDerived:
    def test_cors_origins_split(self):
        s = make(cors_origins="http://a.com, http://b.com ,")
        assert s.cors_origin_list == ["http://a.com", "http://b.com"]

    def test_fallback_order_split(self):
        assert make(provider_fallback_order="ollama, cloud").fallback_order == ["ollama", "cloud"]

    def test_asyncpg_dsn_strips_sqlalchemy_driver(self):
        # asyncpg rejects `postgresql+asyncpg://`; accepting it here means a
        # copy-pasted SQLAlchemy URL doesn't break the app.
        s = make(database_url="postgresql+asyncpg://u:p@h:5432/d")
        assert s.asyncpg_dsn == "postgresql://u:p@h:5432/d"

    def test_plain_dsn_is_untouched(self):
        s = make(database_url="postgresql://u:p@h:5432/d")
        assert s.asyncpg_dsn == "postgresql://u:p@h:5432/d"


class TestRedaction:
    def test_password_never_appears_in_readyz(self):
        from app.api.health import _redact_dsn

        out = _redact_dsn("postgresql://lenny:supersecret@db:5432/lenny")
        assert "supersecret" not in out
        assert out == "postgresql://lenny:***@db:5432/lenny"

    def test_handles_dsn_without_credentials(self):
        from app.api.health import _redact_dsn

        assert _redact_dsn("postgresql:///lenny") == "postgresql:///lenny"
