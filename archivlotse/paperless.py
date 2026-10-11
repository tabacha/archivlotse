from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import PaperlessConfig


class PaperlessClient:
    def __init__(self, config: PaperlessConfig, timeout: float = 30.0):
        self._scheme = urlsplit(config.url).scheme
        self._client = httpx.Client(
            base_url=config.url,
            headers={"Authorization": f"Token {config.token}"},
            timeout=timeout,
        )

    def get_document(self, document_id: int) -> dict:
        response = self._client.get(f"/api/documents/{document_id}/")
        response.raise_for_status()
        return response.json()

    def update_document_field(
        self, document_id: int, field: str, value: int | str | list[int]
    ) -> None:
        response = self._client.patch(
            f"/api/documents/{document_id}/", json={field: value}
        )
        response.raise_for_status()

    def list_document_types(self) -> list[dict]:
        """Fetch all document types configured in Paperless, following
        pagination, each as {"id": ..., "name": ..., ...}."""
        return self._list_all("/api/document_types/")

    def list_tags(self) -> list[dict]:
        """Fetch all tags configured in Paperless, following pagination,
        each as {"id": ..., "name": ..., ...}."""
        return self._list_all("/api/tags/")

    def download_original(self, document_id: int) -> bytes:
        """Fetch the original (unmodified) document file."""
        response = self._client.get(f"/api/documents/{document_id}/download/")
        response.raise_for_status()
        return response.content

    def _list_all(self, path: str) -> list[dict]:
        results: list[dict] = []
        url: str | None = path
        while url:
            response = self._client.get(url)
            response.raise_for_status()
            data = response.json()
            results.extend(data["results"])
            url = self._fix_scheme(data.get("next")) if data.get("next") else None
        return results

    def _fix_scheme(self, url: str) -> str:
        """Paperless can build its "next" pagination links with the wrong
        scheme (e.g. http:// behind a reverse proxy that doesn't forward
        X-Forwarded-Proto), which httpx won't silently follow across a
        redirect by default. Force it back to the scheme we were
        configured with."""
        parts = urlsplit(url)
        if parts.scheme and parts.scheme != self._scheme:
            parts = parts._replace(scheme=self._scheme)
            url = urlunsplit(parts)
        return url

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PaperlessClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
