"""Tests for cache module."""

import pytest
import json
import tempfile
import os
from datetime import datetime, timedelta
from pathlib import Path

from src.cache import MetadataCache, DiscoveryCache
from src.bibtex_parser import BibEntry


class TestMetadataCache:
    """Tests for MetadataCache class."""

    @pytest.fixture
    def temp_cache_file(self):
        """Create a temporary cache file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({}, f)
            return f.name

    @pytest.fixture
    def cache(self, temp_cache_file):
        """Create a cache instance with temp file."""
        cache = MetadataCache(cache_file=temp_cache_file, cache_duration_days=30)
        yield cache
        # Cleanup
        if os.path.exists(temp_cache_file):
            os.unlink(temp_cache_file)

    @pytest.fixture
    def sample_entry(self):
        """Create a sample BibEntry."""
        return BibEntry(
            entry_type="article",
            key="test2023",
            title="Test Paper Title",
            authors=["John Smith", "Jane Doe"],
            year="2023",
            doi="10.1234/test"
        )

    def test_store_and_retrieve_metadata(self, cache, sample_entry):
        """Should store and retrieve metadata correctly."""
        metadata = {
            "abstract": "Test abstract",
            "citation_count": 42,
            "source": "crossref"
        }

        cache.store_metadata(sample_entry, metadata)

        assert cache.is_cached(sample_entry)
        retrieved = cache.get_metadata(sample_entry)
        assert retrieved["abstract"] == "Test abstract"
        assert retrieved["citation_count"] == 42

    def test_cache_persistence(self, temp_cache_file, sample_entry):
        """Should persist cache to disk."""
        # Create cache and store data
        cache1 = MetadataCache(cache_file=temp_cache_file)
        cache1.store_metadata(sample_entry, {"abstract": "Persisted"})
        cache1.save_cache()

        # Create new cache instance and verify data persists
        cache2 = MetadataCache(cache_file=temp_cache_file)
        assert cache2.is_cached(sample_entry)
        retrieved = cache2.get_metadata(sample_entry)
        assert retrieved["abstract"] == "Persisted"

    def test_cache_not_found(self, cache):
        """Should return None for uncached entries."""
        entry = BibEntry(
            entry_type="article",
            key="uncached",
            title="Not Cached"
        )

        assert not cache.is_cached(entry)
        assert cache.get_metadata(entry) is None

    def test_store_failure(self, cache, sample_entry):
        """Should store and track failed enrichment attempts."""
        cache.store_failure(sample_entry, "API timeout")

        entry_hash = cache._get_entry_hash(sample_entry)
        cached_item = cache.cache_data[entry_hash]

        assert cached_item["failed"] is True
        assert cached_item["failure_reason"] == "API timeout"

    def test_retry_logic_for_failed_entries(self, cache, sample_entry):
        """Should allow retry after 7 days for failed entries."""
        # Store a failure
        cache.store_failure(sample_entry, "API error")

        # Should not retry immediately
        assert not cache.should_retry_failed_entry(sample_entry)

        # Manually set failure time to 8 days ago
        entry_hash = cache._get_entry_hash(sample_entry)
        old_time = (datetime.now() - timedelta(days=8)).isoformat()
        cache.cache_data[entry_hash]["last_failure_at"] = old_time
        cache.cache_data[entry_hash]["cached_at"] = old_time

        # Should now allow retry
        assert cache.should_retry_failed_entry(sample_entry)

    def test_get_uncached_entries(self, cache):
        """Should filter out cached entries."""
        entry1 = BibEntry(entry_type="article", key="cached", title="Cached Paper")
        entry2 = BibEntry(entry_type="article", key="uncached", title="Uncached Paper")

        cache.store_metadata(entry1, {"abstract": "Cached"})

        uncached = cache.get_uncached_entries([entry1, entry2])

        assert len(uncached) == 1
        assert uncached[0].key == "uncached"

    def test_cache_stats(self, cache, sample_entry):
        """Should return accurate cache statistics."""
        cache.store_metadata(sample_entry, {"abstract": "Test"})

        entry2 = BibEntry(entry_type="article", key="failed", title="Failed")
        cache.store_failure(entry2, "Error")

        stats = cache.get_cache_stats()

        assert stats["total_entries"] == 2
        assert stats["successful_entries"] == 1
        assert stats["failed_entries"] == 1

    def test_entry_hash_consistency(self, cache):
        """Same entry should always produce same hash."""
        entry1 = BibEntry(
            entry_type="article", key="test", title="Title",
            authors=["Author"], year="2023", doi="10.1234"
        )
        entry2 = BibEntry(
            entry_type="article", key="different_key", title="Title",
            authors=["Author"], year="2023", doi="10.1234"
        )

        # Same content should produce same hash (key doesn't matter for hash)
        hash1 = cache._get_entry_hash(entry1)
        hash2 = cache._get_entry_hash(entry2)
        assert hash1 == hash2


class TestDiscoveryCache:
    """Tests for DiscoveryCache class."""

    @pytest.fixture
    def temp_cache_file(self):
        """Create a temporary cache file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({}, f)
            return f.name

    @pytest.fixture
    def cache(self, temp_cache_file):
        """Create a cache instance with temp file."""
        cache = DiscoveryCache(cache_file=temp_cache_file)
        yield cache
        if os.path.exists(temp_cache_file):
            os.unlink(temp_cache_file)

    @pytest.fixture
    def sample_entry(self):
        """Create a sample BibEntry."""
        return BibEntry(
            entry_type="article",
            key="test2023",
            title="Test Paper",
            authors=["Author"]
        )

    def test_store_and_retrieve_discovery_date(self, cache, sample_entry):
        """Should store and retrieve discovery dates."""
        now = datetime.now(tz=None)
        cache.store_discovery_date(sample_entry, now)

        retrieved = cache.get_discovery_date(sample_entry)

        assert retrieved is not None
        assert retrieved.year == now.year
        assert retrieved.month == now.month
        assert retrieved.day == now.day

    def test_is_known_entry(self, cache, sample_entry):
        """Should track known entries."""
        assert not cache.is_known_entry(sample_entry)

        cache.store_discovery_date(sample_entry, datetime.now())

        assert cache.is_known_entry(sample_entry)

    def test_unknown_entry_returns_none(self, cache):
        """Should return None for unknown entries."""
        entry = BibEntry(entry_type="article", key="unknown", title="Unknown")
        assert cache.get_discovery_date(entry) is None

    def test_persistence(self, temp_cache_file, sample_entry):
        """Should persist discovery dates to disk."""
        cache1 = DiscoveryCache(cache_file=temp_cache_file)
        now = datetime.now()
        cache1.store_discovery_date(sample_entry, now)
        cache1.save_cache()

        cache2 = DiscoveryCache(cache_file=temp_cache_file)
        assert cache2.is_known_entry(sample_entry)
