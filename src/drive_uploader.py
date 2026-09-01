"""Google Drive upload — push Slack-ingested PDFs into the inbox folder.

The filename is chosen to match the existing fuzzy-matcher in
`fg-zettelkasten/src/drive_client.py` (which research-radio shares verbatim):

  Single author : `{Surname} {Year} - {Title}.pdf`
  Multi  authors: `{FirstSurname} et al. {Year} - {Title}.pdf`

That matcher strips non-word characters before comparing, so minor punctuation
differences don't matter. We still scrub a few characters (`/`, control chars)
that Drive itself dislikes in names, and cap the title length so filenames
stay under filesystem-friendly limits.

Service-account credentials come from `GOOGLE_CREDENTIALS_JSON` (the JSON
payload itself, base64- or raw) or `GOOGLE_APPLICATION_CREDENTIALS` (a path).
The target folder is identified by `SLACK_INBOX_DRIVE_FOLDER_ID`.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import List, Optional, Sequence


SCOPES = ["https://www.googleapis.com/auth/drive.file"]
MAX_TITLE_CHARS = 150  # leaves room for `Surname et al. YYYY - …` prefix


def _resolve_credentials():
    """Return Google credentials for the uploader from env vars.

    OAuth *user* credentials take precedence when present
    (`GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET` +
    `GOOGLE_OAUTH_REFRESH_TOKEN`): the bot then uploads *as that user*, who has
    normal Drive storage. This is required when the inbox folder is an ordinary
    My-Drive folder — a service account has no storage quota and cannot own
    uploaded files there. Falls back to the service account
    (`GOOGLE_CREDENTIALS_JSON` / `GOOGLE_APPLICATION_CREDENTIALS`), which only
    works for a Shared Drive target.
    """
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    if client_id and client_secret and refresh_token:
        from google.oauth2.credentials import Credentials  # local import
        return Credentials(
            None,  # no access token yet; the client refreshes on first use
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )

    from google.oauth2 import service_account  # local import for testability

    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw:
        try:
            info = json.loads(raw)
            return service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
        except json.JSONDecodeError:
            # `printf '%s' "$secret"` in CI sometimes contains literal
            # newlines that survived as `\n` strings; try a soft recover.
            info = json.loads(raw.replace("\\n", "\n"))
            return service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        return service_account.Credentials.from_service_account_file(
            path, scopes=SCOPES
        )

    raise RuntimeError(
        "No Google credentials found. Set OAuth user creds "
        "(GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN) for a My-Drive inbox, or "
        "GOOGLE_CREDENTIALS_JSON / GOOGLE_APPLICATION_CREDENTIALS (service "
        "account) for a Shared-Drive inbox."
    )


def build_filename(*, authors: Sequence[str], year: Optional[str],
                   title: str, bibkey: Optional[str] = None) -> str:
    """Mirror the Paperpile-style filename the downstream matcher expects,
    prefixed with the bibkey.

    The bibkey prefix is what makes PDF retrieval survive a metadata-enrichment
    failure: when no title resolves, the name degrades to `Unknown - untitled`,
    whose tokens the downstream title-overlap matcher can never match. The
    bibkey identifies the record outright (toread issue #13).

    It is always included, never only-when-the-title-is-missing: the matcher
    can only anchor on the prefix if the prefix is reliably there. The
    Paperpile-shaped remainder is kept so the author/year tie-breakers and the
    legacy exact-name path still have something to work with.
    """
    first_author = "Unknown"
    if authors:
        parts = authors[0].split()
        if parts:
            first_author = parts[-1]
    author_part = first_author + (" et al." if len(authors) > 1 else "")

    title_clean = _scrub_for_filename(title or "untitled")[:MAX_TITLE_CHARS].rstrip()

    if year:
        name = f"{author_part} {year} - {title_clean}.pdf"
    else:
        name = f"{author_part} - {title_clean}.pdf"

    if bibkey:
        # ' - ' separator: the downstream matcher anchors on `{bibkey} - ` so a
        # key is never a prefix of a longer sibling key.
        name = f"{_scrub_for_filename(bibkey)} - {name}"
    return name


def _scrub_for_filename(text: str) -> str:
    """Remove characters that confuse filesystems or Drive search.

    Kept conservative: the downstream matcher already strips non-word chars
    before comparing, so we only strip what *upload-side* tools choke on.
    """
    # Normalize unicode (combine accents)
    text = unicodedata.normalize("NFC", text)
    # Replace path separators and control characters
    text = text.replace("/", "-").replace("\\", "-")
    text = re.sub(r"[\x00-\x1f]", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


class DriveUploader:
    """Upload PDFs to a single Drive folder (the Slack-inbox folder)."""

    def __init__(self, folder_id: str, credentials=None):
        if not folder_id:
            raise ValueError("DriveUploader requires a folder_id")
        self.folder_id = folder_id
        self.logger = logging.getLogger(__name__)
        self._credentials = credentials or _resolve_credentials()
        self._service = None

    @property
    def service(self):
        """Lazy-built drive v3 service (lets tests inject a mock)."""
        if self._service is None:
            from googleapiclient.discovery import build  # local import
            self._service = build("drive", "v3", credentials=self._credentials)
        return self._service

    def upload(self, *, filename: str, content: bytes,
               overwrite: bool = False) -> dict:
        """Upload `content` as `filename` into the configured folder.

        Returns the Drive file resource dict ({id, name, webViewLink}).

        If a file with the same name already exists in the folder and
        `overwrite` is False (default), the existing file is returned
        unchanged. If `overwrite` is True, its content is replaced.
        """
        from googleapiclient.http import MediaIoBaseUpload  # local import

        existing = self._find_by_name(filename)
        media = MediaIoBaseUpload(
            io.BytesIO(content), mimetype="application/pdf", resumable=False
        )

        if existing and not overwrite:
            self.logger.info(
                "Drive: %s already exists in folder, skipping upload", filename
            )
            return existing

        if existing and overwrite:
            self.logger.info("Drive: replacing %s", filename)
            updated = self.service.files().update(
                fileId=existing["id"],
                media_body=media,
                fields="id,name,webViewLink",
            ).execute()
            return updated

        self.logger.info("Drive: uploading %s (%d bytes)", filename, len(content))
        created = self.service.files().create(
            body={
                "name": filename,
                "parents": [self.folder_id],
            },
            media_body=media,
            fields="id,name,webViewLink",
        ).execute()
        return created

    def list_pdfs(self) -> List[dict]:
        """Every PDF in the configured folder, as {id, name}.

        Used by the rename pass, which has to work out which existing file
        belongs to which bibkey — the upload path never recorded the Drive
        file id in state, so the name is the only link back.
        """
        files: List[dict] = []
        page_token = None
        query = (
            f"'{self.folder_id}' in parents and "
            f"mimeType='application/pdf' and trashed = false"
        )
        while True:
            resp = self.service.files().list(
                q=query, fields="nextPageToken, files(id,name)",
                pageSize=1000, pageToken=page_token,
                # The Slack-inbox folder may live on a Shared Drive.
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return files

    def rename(self, file_id: str, new_name: str) -> dict:
        """Rename one file in place, keeping its id (and so its URL)."""
        return self.service.files().update(
            fileId=file_id, body={"name": new_name},
            fields="id,name", supportsAllDrives=True,
        ).execute()

    def _find_by_name(self, filename: str) -> Optional[dict]:
        # Drive's `name = '...'` is exact; escape single quotes per API rules.
        safe = filename.replace("'", "\\'")
        query = (
            f"'{self.folder_id}' in parents and "
            f"mimeType='application/pdf' and "
            f"name = '{safe}' and trashed = false"
        )
        results = self.service.files().list(
            q=query, fields="files(id,name,webViewLink)", pageSize=2
        ).execute()
        files = results.get("files", [])
        return files[0] if files else None
