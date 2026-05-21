"""Tests for src.unpaywall_client."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.unpaywall_client import UnpaywallClient, UnpaywallResult


@pytest.fixture
def client(tmp_path):
    return UnpaywallClient(
        email="test@example.com",
        cache_file=str(tmp_path / "unpaywall_cache.json"),
        rate_limit_seconds=0.0,
    )


def _mock_response(status_code=200, json_body=None):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_body or {}
    return mock


def test_normalize_doi_strips_prefixes(client):
    assert client._normalize_doi("https://doi.org/10.1000/AbC") == "10.1000/abc"
    assert client._normalize_doi("doi:10.1/x") == "10.1/x"
    assert client._normalize_doi("   10.1/X  ") == "10.1/x"
    assert client._normalize_doi("") == ""


def test_email_required():
    with pytest.raises(ValueError):
        UnpaywallClient(email="")


def test_lookup_open_access_hit(client):
    body = {
        "is_oa": True,
        "best_oa_location": {
            "url_for_pdf": "https://example.org/paper.pdf",
            "license": "cc-by",
            "host_type": "publisher",
        },
    }
    with patch("src.unpaywall_client.requests.get",
               return_value=_mock_response(200, body)):
        result = client.lookup("10.1/oa")
    assert result is not None
    assert result.is_oa is True
    assert result.best_oa_pdf_url == "https://example.org/paper.pdf"
    assert result.license == "cc-by"
    assert result.host_type == "publisher"


def test_lookup_closed_access(client):
    body = {"is_oa": False, "best_oa_location": None}
    with patch("src.unpaywall_client.requests.get",
               return_value=_mock_response(200, body)):
        result = client.lookup("10.1/closed")
    assert result is not None
    assert result.is_oa is False
    assert result.best_oa_pdf_url is None


def test_lookup_404_is_cached_negative(client):
    with patch("src.unpaywall_client.requests.get",
               return_value=_mock_response(404)) as mock_get:
        first = client.lookup("10.1/notfound")
        # Second call should be served from cache, not a new HTTP request.
        second = client.lookup("10.1/notfound")
    assert first is not None
    assert first.is_oa is False
    assert mock_get.call_count == 1
    assert second == first


def test_lookup_network_error_returns_none(client):
    import requests
    with patch("src.unpaywall_client.requests.get",
               side_effect=requests.ConnectionError("boom")):
        result = client.lookup("10.1/networkdown")
    assert result is None


def test_cache_round_trips(tmp_path):
    cache_file = tmp_path / "unpaywall_cache.json"
    body = {
        "is_oa": True,
        "best_oa_location": {"url_for_pdf": "https://x.org/a.pdf"},
    }
    c1 = UnpaywallClient(
        email="e@example.com",
        cache_file=str(cache_file),
        rate_limit_seconds=0.0,
    )
    with patch("src.unpaywall_client.requests.get",
               return_value=_mock_response(200, body)) as mock_get:
        c1.lookup("10.1/rt")
    c1.save()
    assert cache_file.exists()

    # Fresh client reads from disk; no live call.
    c2 = UnpaywallClient(
        email="e@example.com",
        cache_file=str(cache_file),
        rate_limit_seconds=0.0,
    )
    with patch("src.unpaywall_client.requests.get") as mock_get2:
        cached = c2.lookup("10.1/rt")
    mock_get2.assert_not_called()
    assert cached is not None
    assert cached.is_oa is True
    assert cached.best_oa_pdf_url == "https://x.org/a.pdf"


def test_doi_normalization_used_for_cache_lookup(client):
    body = {"is_oa": True, "best_oa_location": {"url_for_pdf": "x"}}
    with patch("src.unpaywall_client.requests.get",
               return_value=_mock_response(200, body)) as mock_get:
        client.lookup("https://doi.org/10.1/Foo")
        # Different surface form, same DOI — should hit cache.
        client.lookup("DOI:10.1/foo")
    assert mock_get.call_count == 1
