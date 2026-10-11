from __future__ import annotations

import httpx


class DoclingClient:
    """Client for a Docling Serve instance (document conversion/OCR)."""

    def __init__(self, base_url: str, timeout: float = 300.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def convert_to_text(self, filename: str, content: bytes, force_ocr: bool = True) -> str:
        response = self._client.post(
            "/v1/convert/file",
            files={"files": (filename, content, "application/pdf")},
            data={"to_formats": "text", "force_ocr": "true" if force_ocr else "false"},
        )
        response.raise_for_status()
        data = response.json()
        status = data.get("status")
        if status not in ("success", "partial_success"):
            raise RuntimeError(f"Docling-Konvertierung fehlgeschlagen (status={status!r})")
        return data["document"].get("text_content") or ""

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DoclingClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
