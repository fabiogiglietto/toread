"""Tests for OpenAlex client."""

import pytest
from src.metadata_enricher import OpenAlexClient


class TestOpenAlexAbstractReconstruction:
    """Tests for abstract reconstruction from inverted index."""

    def test_reconstructs_simple_abstract(self):
        """Should reconstruct abstract from inverted index format."""
        client = OpenAlexClient()
        inverted_index = {
            "This": [0],
            "is": [1],
            "a": [2],
            "test": [3],
            "abstract": [4]
        }
        result = client._reconstruct_abstract(inverted_index)
        assert result == "This is a test abstract"

    def test_handles_repeated_words(self):
        """Should handle words that appear multiple times."""
        client = OpenAlexClient()
        inverted_index = {
            "the": [0, 3],
            "cat": [1],
            "chased": [2],
            "dog": [4]
        }
        result = client._reconstruct_abstract(inverted_index)
        assert result == "the cat chased the dog"

    def test_returns_none_for_empty_index(self):
        """Should return None for empty inverted index."""
        client = OpenAlexClient()
        assert client._reconstruct_abstract({}) is None
        assert client._reconstruct_abstract(None) is None


class TestOpenAlexDOICleaning:
    """Tests for DOI cleaning."""

    def test_cleans_doi_url(self):
        """Should remove DOI URL prefix."""
        client = OpenAlexClient()
        assert client._clean_doi("https://doi.org/10.1234/test") == "10.1234/test"
        assert client._clean_doi("http://dx.doi.org/10.1234/test") == "10.1234/test"

    def test_cleans_doi_prefix(self):
        """Should remove doi: prefix."""
        client = OpenAlexClient()
        assert client._clean_doi("doi:10.1234/test") == "10.1234/test"

    def test_handles_clean_doi(self):
        """Should handle already clean DOI."""
        client = OpenAlexClient()
        assert client._clean_doi("10.1234/test") == "10.1234/test"

    def test_handles_empty_doi(self):
        """Should handle empty DOI."""
        client = OpenAlexClient()
        assert client._clean_doi("") == ""
        assert client._clean_doi(None) == ""


class TestOpenAlexResponseParsing:
    """Tests for OpenAlex response parsing."""

    def test_parses_basic_work(self):
        """Should parse basic work response."""
        client = OpenAlexClient()
        work = {
            "doi": "https://doi.org/10.1234/test",
            "title": "Test Paper",
            "publication_date": "2024-01-15",
            "cited_by_count": 42,
            "authorships": [
                {"author": {"display_name": "John Doe"}},
                {"author": {"display_name": "Jane Smith"}}
            ],
            "primary_location": {
                "source": {"display_name": "Nature"},
                "landing_page_url": "https://example.com/paper"
            },
            "open_access": {
                "is_oa": True,
                "oa_url": "https://example.com/paper.pdf"
            }
        }

        result = client._parse_response(work)

        assert result.doi == "10.1234/test"
        assert result.doi_url == "https://doi.org/10.1234/test"
        assert result.publication_date == "2024-01-15"
        assert result.citation_count == 42
        assert result.authors == ["John Doe", "Jane Smith"]
        assert result.venue == "Nature"
        assert result.url == "https://example.com/paper"
        assert result.is_open_access is True
        assert result.pdf_url == "https://example.com/paper.pdf"
        assert result.source == "openalex"

    def test_handles_missing_fields(self):
        """Should handle work with missing fields."""
        client = OpenAlexClient()
        work = {"title": "Minimal Paper"}

        result = client._parse_response(work)

        assert result.source == "openalex"
        assert result.doi is None
        assert result.authors == []
        assert result.venue is None
