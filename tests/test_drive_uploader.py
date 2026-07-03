"""Tests for src.drive_uploader.

We don't have google-api-python-client in the dev env reliably, so the
filename-building tests stand on their own and the upload logic is exercised
against a fake `service` object the tests inject.
"""

from unittest.mock import MagicMock

import pytest

from src.drive_uploader import (
    DriveUploader,
    build_filename,
    _scrub_for_filename,
)


# ---- filename builder ----------------------------------------------------


def test_build_filename_single_author():
    name = build_filename(
        authors=["Jane Smith"], year="2026", title="The widget paradox"
    )
    assert name == "Smith 2026 - The widget paradox.pdf"


def test_build_filename_multi_author_et_al():
    name = build_filename(
        authors=["Jane Smith", "John Doe"], year="2026",
        title="Of widgets and woes",
    )
    assert name == "Smith et al. 2026 - Of widgets and woes.pdf"


def test_build_filename_handles_long_title():
    title = "x" * 500
    name = build_filename(authors=["Jane Smith"], year="2026", title=title)
    # Title portion stays below MAX_TITLE_CHARS.
    title_part = name.split(" - ", 1)[1].replace(".pdf", "")
    assert len(title_part) <= 150


def test_build_filename_strips_path_separators():
    name = build_filename(
        authors=["Smith"], year="2026", title="a / b \\ c"
    )
    assert "/" not in name
    assert "\\" not in name
    assert "a - b - c" in name


def test_build_filename_no_year_no_authors():
    name = build_filename(authors=[], year=None, title="Lonely")
    assert name == "Unknown - Lonely.pdf"


def test_scrub_normalizes_whitespace():
    out = _scrub_for_filename("hello    world\n\t  ")
    assert out == "hello world"


def test_scrub_keeps_unicode():
    # NFC-composed character should stay readable, just not crash.
    out = _scrub_for_filename("Bélanger")
    assert "élanger" in out


# ---- uploader logic ------------------------------------------------------


def _make_uploader(folder_id="folder-x"):
    """DriveUploader with credentials short-circuited and a mock service."""
    up = DriveUploader.__new__(DriveUploader)  # bypass __init__
    up.folder_id = folder_id
    up._credentials = None
    up._service = MagicMock()
    import logging
    up.logger = logging.getLogger("test")
    return up


def test_upload_creates_when_not_present():
    up = _make_uploader()
    # _find_by_name (which uses list) returns no files.
    up._service.files().list().execute.return_value = {"files": []}
    up._service.files().create().execute.return_value = {
        "id": "FILE_ID", "name": "Smith 2026 - X.pdf",
        "webViewLink": "https://drive/Smith"
    }

    # We can't patch the local import of MediaIoBaseUpload easily — skip the
    # upload pipe path by patching the create() chain at the call site.
    from unittest.mock import patch
    with patch("src.drive_uploader.MediaIoBaseUpload", create=True,
               return_value=MagicMock()):
        result = up.upload(filename="Smith 2026 - X.pdf",
                           content=b"%PDF-1.4\n" + b"x" * 100)

    assert result["id"] == "FILE_ID"
    up._service.files().create.assert_called()


def test_upload_skips_when_present_and_no_overwrite():
    up = _make_uploader()
    up._service.files().list().execute.return_value = {
        "files": [{
            "id": "EXISTING", "name": "Smith 2026 - X.pdf",
            "webViewLink": "https://drive/Smith"
        }]
    }
    up._service.files().create.reset_mock()
    up._service.files().update.reset_mock()

    from unittest.mock import patch
    with patch("src.drive_uploader.MediaIoBaseUpload", create=True,
               return_value=MagicMock()):
        result = up.upload(
            filename="Smith 2026 - X.pdf",
            content=b"%PDF-1.4\n" + b"x" * 100,
        )
    assert result["id"] == "EXISTING"
    up._service.files().create.assert_not_called()
    up._service.files().update.assert_not_called()


def test_upload_overwrites_when_requested():
    up = _make_uploader()
    up._service.files().list().execute.return_value = {
        "files": [{"id": "EXISTING", "name": "Smith 2026 - X.pdf",
                   "webViewLink": "https://drive/Smith"}]
    }
    up._service.files().create.reset_mock()
    up._service.files().update().execute.return_value = {
        "id": "EXISTING", "name": "Smith 2026 - X.pdf",
        "webViewLink": "https://drive/Smith"
    }

    from unittest.mock import patch
    with patch("src.drive_uploader.MediaIoBaseUpload", create=True,
               return_value=MagicMock()):
        result = up.upload(
            filename="Smith 2026 - X.pdf",
            content=b"%PDF-1.4\n" + b"x" * 100,
            overwrite=True,
        )
    assert result["id"] == "EXISTING"
    up._service.files().update.assert_called()


def test_construct_requires_folder_id():
    with pytest.raises(ValueError):
        DriveUploader.__init__(
            DriveUploader.__new__(DriveUploader),
            folder_id="",
            credentials=object(),
        )


def test_oauth_user_credentials_take_precedence(monkeypatch):
    """When OAuth user-cred env vars are set, _resolve_credentials returns
    user Credentials (the bot uploads as the user, who has Drive quota)."""
    from src import drive_uploader
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "rtok")
    # Even if a service-account JSON is also present, OAuth wins.
    monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", '{"type":"service_account"}')
    creds = drive_uploader._resolve_credentials()
    from google.oauth2.credentials import Credentials as UserCredentials
    assert isinstance(creds, UserCredentials)
    assert creds.refresh_token == "rtok"
