from __future__ import annotations

import httpx

from .config import PaperlessConfig


class PaperlessClient:
    def __init__(self, config: PaperlessConfig, timeout: float = 30.0):
        self._client = httpx.Client(
            base_url=config.url,
            headers={"Authorization": f"Token {config.token}"},
            timeout=timeout,
        )

    def get_document(self, document_id: int) -> dict:
        response = self._client.get(f"/api/documents/{document_id}/")
        response.raise_for_status()
        return response.json()

    def update_document_field(self, document_id: int, field: str, value: str) -> None:
        response = self._client.patch(
            f"/api/documents/{document_id}/", json={field: value}
        )
        response.raise_for_status()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PaperlessClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
