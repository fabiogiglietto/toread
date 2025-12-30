"""Tests for RSS generator module."""

import pytest
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from src.rss_generator import FeedGenerator
from src.bibtex_parser import BibEntry
from src.metadata_enricher import EnrichedMetadata


class TestFeedGenerator:
    """Tests for FeedGenerator class."""

    @pytest.fixture
    def generator(self):
        """Create a feed generator instance."""
        return FeedGenerator(
            feed_title="Test Feed",
            feed_description="A test feed",
            feed_link="https://example.com"
        )

    @pytest.fixture
    def sample_entry(self):
        """Create a sample BibEntry."""
        return BibEntry(
            entry_type="article",
            key="test2023",
            title="Test Paper on Machine Learning",
            authors=["John Smith", "Jane Doe"],
            year="2023",
            month="06",
            journal="Journal of Testing",
            doi="10.1234/test.2023",
            abstract="This is a test abstract.",
            discovery_date=datetime(2023, 6, 15, tzinfo=timezone.utc)
        )

    @pytest.fixture
    def sample_metadata(self):
        """Create sample enriched metadata."""
        return EnrichedMetadata(
            abstract="Enriched abstract from API.",
            doi="10.1234/test.2023",
            doi_url="https://doi.org/10.1234/test.2023",
            citation_count=42,
            reference_count=15,
            source="crossref",
            is_open_access=True,
            venue="Journal of Testing"
        )


class TestJsonFeed:
    """Tests for JSON Feed generation."""

    @pytest.fixture
    def generator(self):
        return FeedGenerator(
            feed_title="Test Feed",
            feed_description="A test feed",
            feed_link="https://example.com"
        )

    @pytest.fixture
    def sample_entry(self):
        return BibEntry(
            entry_type="article",
            key="test2023",
            title="Test Paper",
            authors=["John Smith"],
            year="2023",
            doi="10.1234/test",
            discovery_date=datetime(2023, 6, 15, tzinfo=timezone.utc)
        )

    def test_generates_valid_json(self, generator, sample_entry):
        """Should generate valid JSON."""
        output = generator.generate_json_feed([sample_entry])

        # Should parse without error
        feed = json.loads(output)

        assert "version" in feed
        assert "items" in feed

    def test_feed_metadata(self, generator, sample_entry):
        """Should include feed metadata."""
        output = generator.generate_json_feed([sample_entry])
        feed = json.loads(output)

        assert feed["title"] == "Test Feed"
        assert feed["description"] == "A test feed"
        assert "https://jsonfeed.org/version/1.1" in feed["version"]

    def test_item_structure(self, generator, sample_entry):
        """Should create properly structured items."""
        output = generator.generate_json_feed([sample_entry])
        feed = json.loads(output)

        assert len(feed["items"]) == 1
        item = feed["items"][0]

        assert "id" in item
        assert "title" in item
        assert item["title"] == "Test Paper"

    def test_doi_as_guid(self, generator, sample_entry):
        """Should use DOI as GUID when available."""
        output = generator.generate_json_feed([sample_entry])
        feed = json.loads(output)

        item = feed["items"][0]
        assert item["id"] == "doi:10.1234/test"

    def test_fallback_guid(self, generator):
        """Should use bibtex key as GUID when DOI unavailable."""
        entry = BibEntry(entry_type="article", key="nodoi2023", title="No DOI")

        output = generator.generate_json_feed([entry])
        feed = json.loads(output)

        item = feed["items"][0]
        assert item["id"] == "bibtex:nodoi2023"

    def test_academic_extensions(self, generator, sample_entry):
        """Should include academic metadata extensions."""
        metadata = EnrichedMetadata(
            citation_count=42,
            source="crossref"
        )

        output = generator.generate_json_feed(
            [sample_entry],
            {sample_entry.key: metadata}
        )
        feed = json.loads(output)

        item = feed["items"][0]
        assert "_academic" in item
        assert item["_academic"]["citation_count"] == 42

    def test_discovery_date_included(self, generator, sample_entry):
        """Should include discovery date."""
        output = generator.generate_json_feed([sample_entry])
        feed = json.loads(output)

        item = feed["items"][0]
        assert "_discovery_date" in item
        assert "2023-06-15" in item["_discovery_date"]

    def test_sorts_by_discovery_date(self, generator):
        """Should sort entries by discovery date (newest first)."""
        older = BibEntry(
            entry_type="article", key="older",
            title="Older Paper",
            discovery_date=datetime(2023, 1, 1, tzinfo=timezone.utc)
        )
        newer = BibEntry(
            entry_type="article", key="newer",
            title="Newer Paper",
            discovery_date=datetime(2023, 12, 1, tzinfo=timezone.utc)
        )

        output = generator.generate_json_feed([older, newer])
        feed = json.loads(output)

        # Newer should come first
        assert feed["items"][0]["title"] == "Newer Paper"


class TestRssFeed:
    """Tests for RSS feed generation."""

    @pytest.fixture
    def generator(self):
        return FeedGenerator(
            feed_title="Test RSS Feed",
            feed_description="A test RSS feed",
            feed_link="https://example.com"
        )

    @pytest.fixture
    def sample_entry(self):
        return BibEntry(
            entry_type="article",
            key="test2023",
            title="Test Paper",
            authors=["John Smith"],
            year="2023",
            doi="10.1234/test",
            discovery_date=datetime(2023, 6, 15, tzinfo=timezone.utc)
        )

    def test_generates_valid_xml(self, generator, sample_entry):
        """Should generate valid XML."""
        output = generator.generate_rss([sample_entry])

        # Should parse without error
        root = ET.fromstring(output)

        assert root.tag == "rss"
        assert root.attrib["version"] == "2.0"

    def test_channel_metadata(self, generator, sample_entry):
        """Should include channel metadata."""
        output = generator.generate_rss([sample_entry])
        root = ET.fromstring(output)

        channel = root.find("channel")
        assert channel is not None

        title = channel.find("title")
        assert title is not None
        assert title.text == "Test RSS Feed"

    def test_item_structure(self, generator, sample_entry):
        """Should create properly structured items."""
        output = generator.generate_rss([sample_entry])
        root = ET.fromstring(output)

        channel = root.find("channel")
        items = channel.findall("item")

        assert len(items) == 1

        item = items[0]
        title = item.find("title")
        assert title is not None
        assert title.text == "Test Paper"

    def test_includes_guid(self, generator, sample_entry):
        """Should include GUID element."""
        output = generator.generate_rss([sample_entry])
        root = ET.fromstring(output)

        item = root.find(".//item")
        guid = item.find("guid")

        assert guid is not None
        assert guid.attrib["isPermaLink"] == "false"

    def test_includes_link(self, generator, sample_entry):
        """Should include DOI link."""
        output = generator.generate_rss([sample_entry])
        root = ET.fromstring(output)

        item = root.find(".//item")
        link = item.find("link")

        assert link is not None
        assert "doi.org" in link.text


class TestHtmlEscaping:
    """Tests for HTML escaping in feed content."""

    @pytest.fixture
    def generator(self):
        return FeedGenerator()

    def test_escapes_html_in_title(self, generator):
        """Should escape HTML characters in title."""
        entry = BibEntry(
            entry_type="article",
            key="html2023",
            title="<script>alert('XSS')</script> Paper"
        )

        output = generator.generate_json_feed([entry])
        feed = json.loads(output)

        # Title should be escaped or safe
        title = feed["items"][0]["title"]
        assert "<script>" not in title or "&lt;script&gt;" in title

    def test_escapes_html_in_abstract(self, generator):
        """Should escape HTML in content."""
        entry = BibEntry(
            entry_type="article",
            key="test",
            title="Test",
            abstract="<img src=x onerror=alert(1)>"
        )

        output = generator.generate_json_feed([entry])

        # Should not contain unescaped HTML
        assert '<img src=x onerror=' not in output or '&lt;img' in output
