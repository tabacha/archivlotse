from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import PaperlessConfig

# Paperless's default page size is 25, which turns listing a few hundred
# correspondents into dozens of round trips (~2 minutes measured live
# against a ~1000-correspondent instance). Requesting a much larger page
# size up front collapses that into a single request in practice, while
# _list_all still follows "next" links for anything beyond this.
_LIST_PAGE_SIZE = 1000


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
        return self._list_all("/api/document_types/", params={"page_size": _LIST_PAGE_SIZE})

    def list_tags(self) -> list[dict]:
        """Fetch all tags configured in Paperless, following pagination,
        each as {"id": ..., "name": ..., ...}."""
        return self._list_all("/api/tags/", params={"page_size": _LIST_PAGE_SIZE})

    def list_correspondents(self) -> list[dict]:
        """Fetch all correspondents configured in Paperless, following
        pagination, each as {"id": ..., "name": ..., ...}. Paperless's
        default page size (25) would mean ~20+ round trips for an
        instance with hundreds of correspondents (~2 minutes measured
        live); requesting _LIST_PAGE_SIZE at once cuts that to a single
        request in practice."""
        return self._list_all("/api/correspondents/", params={"page_size": _LIST_PAGE_SIZE})

    def list_documents(
        self, tags_all: list[int] | None = None, tags_none: list[int] | None = None
    ) -> list[dict]:
        """Fetch all documents (only id, title, tags) that carry ALL of
        `tags_all` and NONE of `tags_none`, following pagination. Restricting
        `fields` keeps the full OCR text out of what may be thousands of
        list entries."""
        params: dict = {"page_size": _LIST_PAGE_SIZE, "fields": "id,title,tags"}
        if tags_all:
            params["tags__id__all"] = ",".join(str(tag_id) for tag_id in tags_all)
        if tags_none:
            params["tags__id__none"] = ",".join(str(tag_id) for tag_id in tags_none)
        return self._list_all("/api/documents/", params=params)

    def download_original(self, document_id: int) -> bytes:
        """Fetch the original (unmodified) document file."""
        response = self._client.get(f"/api/documents/{document_id}/download/")
        response.raise_for_status()
        return response.content

    def _list_all(self, path: str, params: dict | None = None) -> list[dict]:
        """Follow pagination from `path`, starting with `params` (only
        applied to the first request - a "next" link is already a full
        absolute URL with all query params, including page_size, baked
        in by Paperless itself)."""
        results: list[dict] = []
        url: str | None = path
        first_request = True
        while url:
            response = self._client.get(url, params=params if first_request else None)
            first_request = False
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
