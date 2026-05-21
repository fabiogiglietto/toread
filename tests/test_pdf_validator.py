"""Tests for src.pdf_validator."""

from unittest.mock import patch, MagicMock

import pytest

from src.pdf_validator import (
    PDFValidationError,
    download_and_validate,
    MIN_PDF_BYTES,
)


def _streaming_response(status_code=200, content_type="application/pdf",
                       body=b""):
    """Build a Mock that mimics requests.Response with .iter_content()."""
    chunks = [body[i:i + 4096] for i in range(0, len(body), 4096)] or [b""]
    mock = MagicMock()
    mock.status_code = status_code
    mock.headers = {"Content-Type": content_type}
    mock.iter_content = MagicMock(return_value=iter(chunks))
    mock.close = MagicMock()
    return mock


def _real_pdf_bytes(size=MIN_PDF_BYTES + 1024):
    """Plausible PDF body: magic header + filler."""
    filler = b"x" * (size - 5)
    return b"%PDF-" + filler


def test_validates_real_pdf():
    body = _real_pdf_bytes()
    with patch("src.pdf_validator.requests.get",
               return_value=_streaming_response(body=body)):
        result = download_and_validate("https://example.org/a.pdf")
    assert result.size == len(body)
    assert result.content.startswith(b"%PDF-")


def test_rejects_html_masquerade():
    body = b"<!DOCTYPE html><html>...not a PDF...</html>" * 1000
    with patch("src.pdf_validator.requests.get",
               return_value=_streaming_response(
                   content_type="text/html", body=body)):
        with pytest.raises(PDFValidationError, match="non-PDF content-type"):
            download_and_validate("https://example.org/a.pdf")


def test_rejects_pdf_content_type_but_html_body():
    """Some servers lie in headers. Magic-byte check catches it."""
    body = b"<html>" + b"x" * (MIN_PDF_BYTES + 100)
    with patch("src.pdf_validator.requests.get",
               return_value=_streaming_response(body=body)):
        with pytest.raises(PDFValidationError, match="missing %PDF- magic"):
            download_and_validate("https://example.org/a.pdf")


def test_rejects_too_small_pdf():
    body = b"%PDF-" + b"x" * 100  # well below MIN_PDF_BYTES
    with patch("src.pdf_validator.requests.get",
               return_value=_streaming_response(body=body)):
        with pytest.raises(PDFValidationError, match="PDF too small"):
            download_and_validate("https://example.org/a.pdf")


def test_rejects_non_200():
    with patch("src.pdf_validator.requests.get",
               return_value=_streaming_response(status_code=403)):
        with pytest.raises(PDFValidationError, match="HTTP 403"):
            download_and_validate("https://example.org/a.pdf")


def test_accepts_octet_stream_with_pdf_url():
    body = _real_pdf_bytes()
    with patch("src.pdf_validator.requests.get",
               return_value=_streaming_response(
                   content_type="application/octet-stream", body=body)):
        result = download_and_validate("https://example.org/a.pdf")
    assert result.content_type == "application/octet-stream"


def test_rejects_octet_stream_with_non_pdf_url():
    body = _real_pdf_bytes()
    with patch("src.pdf_validator.requests.get",
               return_value=_streaming_response(
                   content_type="application/octet-stream", body=body)):
        with pytest.raises(PDFValidationError, match="non-PDF content-type"):
            download_and_validate("https://example.org/get?id=42")


def test_auth_header_passed_through():
    body = _real_pdf_bytes()
    with patch("src.pdf_validator.requests.get",
               return_value=_streaming_response(body=body)) as mock_get:
        download_and_validate(
            "https://files.slack.com/x.pdf",
            auth_header="Bearer xoxb-test",
        )
    headers = mock_get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer xoxb-test"


def test_max_bytes_enforced():
    body = b"%PDF-" + b"x" * (200 * 1024)  # 200KB
    with patch("src.pdf_validator.requests.get",
               return_value=_streaming_response(body=body)):
        with pytest.raises(PDFValidationError, match="exceeds max_bytes"):
            download_and_validate(
                "https://example.org/big.pdf",
                max_bytes=100 * 1024,
            )
