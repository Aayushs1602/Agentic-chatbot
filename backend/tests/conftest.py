"""Shared fixtures.

The whole suite must run on a cold machine with no Docker, no Ollama, and no API
keys — that is the contract, because an evaluator will run `make test-local`
before they run anything else. Tests that genuinely need Postgres are marked
`@pytest.mark.db` and skip themselves when it isn't reachable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make `app.*` importable when pytest is invoked from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Set before importing app.config, whose Settings are cached at import time.
os.environ.setdefault("LOG_FORMAT", "console")
os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture(scope="session")
def settings():
    from app.config import get_settings

    return get_settings()


@pytest.fixture
def sample_transcript() -> str:
    """A transcript shaped like the real corpus: speaker turns and timestamps."""
    return (
        "[00:00:12] Lenny Rachitsky: Welcome to the show. Today we are talking "
        "about product-market fit and how you actually know when you have it.\n\n"
        "[00:01:30] Guest Name: The honest answer is that it feels obvious in "
        "hindsight and impossible in the moment. The signal I trust most is "
        "retention. If people come back without being reminded, something is "
        "working. Everything else is noise you can talk yourself into.\n\n"
        "[00:04:05] Lenny Rachitsky: How do you measure that in practice?\n\n"
        "[00:04:11] Guest Name: Cohort retention curves that flatten. A curve "
        "that flattens above zero means a real group of people keeps coming "
        "back. A curve that decays to zero means you have a leaky bucket and no "
        "amount of top-of-funnel growth will save you.\n\n"
        "[00:09:40] Guest Name: The second signal is pull. When users are "
        "annoyed that your product is down, when they email you asking for more, "
        "that is pull. Before product-market fit you push. After, you get pulled.\n"
    )


@pytest.fixture
async def db_pool():
    """A live Postgres pool, or skip. Never fabricate a database."""
    import asyncpg

    from app.config import settings as app_settings

    try:
        pool = await asyncpg.create_pool(dsn=app_settings.asyncpg_dsn, min_size=1, max_size=2)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not available ({type(exc).__name__}) — skipping db test")
        return
    try:
        yield pool
    finally:
        await pool.close()
