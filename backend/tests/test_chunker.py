"""Chunker behaviour.

The properties tested here are the ones that determine whether a citation
actually supports the claim it is attached to.
"""

from __future__ import annotations

import pytest

from app.rag.chunker import Chunk, chunk_transcript, estimate_tokens, parse_timestamp


class TestTimestamps:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("[00:12:34] Speaker: hi", 754),
            ("(01:02:03) Speaker: hi", 3723),
            ("12:34 Speaker: hi", 754),          # bare MM:SS
            ("[00:00:00] start", 0),
            ("no timestamp at all here", None),
        ],
    )
    def test_parses_common_formats(self, text, expected):
        assert parse_timestamp(text) == expected

    def test_mm_ss_is_not_read_as_hh_mm(self):
        # "05:30" in a podcast means 5m30s, not 5h30m. Getting this wrong
        # produces deep links hours away from the quoted passage.
        assert parse_timestamp("05:30 Speaker: hi") == 330


class TestTokenEstimate:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_scales_with_length(self):
        short = estimate_tokens("one two three")
        long = estimate_tokens(" ".join(["word"] * 300))
        assert 0 < short < long

    def test_overestimates_rather_than_under(self):
        # Overestimating keeps chunks inside the context window; underestimating
        # silently overflows it.
        assert estimate_tokens(" ".join(["word"] * 100)) >= 100


class TestChunking:
    def test_empty_input_yields_nothing(self):
        assert chunk_transcript("") == []
        assert chunk_transcript("   \n\n  ") == []

    def test_short_transcript_is_one_chunk(self, sample_transcript):
        chunks = chunk_transcript(sample_transcript, target_tokens=5000, overlap_tokens=100)
        assert len(chunks) == 1
        assert chunks[0].ord == 0

    def test_long_transcript_splits(self):
        text = ". ".join(f"This is sentence number {i} in a long transcript" for i in range(1200))
        chunks = chunk_transcript(text, target_tokens=200, overlap_tokens=40)
        assert len(chunks) > 5

    def test_ordinals_are_contiguous_from_zero(self):
        text = ". ".join(f"Sentence {i} carries some content" for i in range(800))
        chunks = chunk_transcript(text, target_tokens=150, overlap_tokens=30)
        assert [c.ord for c in chunks] == list(range(len(chunks)))

    def test_offsets_map_back_to_source(self):
        text = ". ".join(f"Sentence {i} carries some content" for i in range(500))
        for chunk in chunk_transcript(text, target_tokens=150, overlap_tokens=30):
            assert 0 <= chunk.start_char < chunk.end_char <= len(text)
            # The stored text must be findable in the slice it claims to come
            # from — otherwise "cited from characters X-Y" is a lie.
            assert chunk.text in text[chunk.start_char : chunk.end_char]

    def test_chunks_overlap(self):
        text = ". ".join(f"Sentence {i} carries some content" for i in range(600))
        chunks = chunk_transcript(text, target_tokens=200, overlap_tokens=60)
        assert len(chunks) >= 2
        # Consecutive chunks must share source range, or a claim spanning a
        # boundary is retrievable from neither side.
        assert chunks[1].start_char < chunks[0].end_char

    def test_captures_timestamps(self, sample_transcript):
        chunks = chunk_transcript(sample_transcript, target_tokens=60, overlap_tokens=10)
        assert any(c.start_seconds is not None for c in chunks)

    def test_is_deterministic(self, sample_transcript):
        a = chunk_transcript(sample_transcript, target_tokens=80, overlap_tokens=20)
        b = chunk_transcript(sample_transcript, target_tokens=80, overlap_tokens=20)
        assert [(c.ord, c.start_char, c.end_char) for c in a] == [
            (c.ord, c.start_char, c.end_char) for c in b
        ]

    def test_rejects_overlap_larger_than_chunk(self):
        # This configuration makes the window never advance; failing loudly
        # beats an ingest that spins forever.
        with pytest.raises(ValueError, match="smaller than"):
            chunk_transcript("some text here", target_tokens=100, overlap_tokens=100)

    def test_terminates_on_pathological_input(self):
        # No sentence boundaries, no speakers, no blank lines — the fallback
        # path must still make forward progress.
        text = "x" * 50_000
        chunks = chunk_transcript(text, target_tokens=100, overlap_tokens=20)
        assert len(chunks) > 1
        assert all(isinstance(c, Chunk) for c in chunks)
