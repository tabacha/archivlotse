from __future__ import annotations

import httpx

from .errors import ServiceError


def _format_errors(errors: list[dict] | None) -> str:
    """Docling's response includes a structured "errors" list (component,
    module, message, page) on failure - surface it instead of just the
    bare status, e.g. to see at a glance that a failure was PIL's
    decompression-bomb guard on an oversized rendered page, not a
    generic/unknown failure."""
    if not errors:
        return "keine Details in der Docling-Antwort"
    return "; ".join(
        f"Seite {error.get('page_no', '?')}: {error.get('error_message', error)}" for error in errors
    )


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
            raise ServiceError(
                f"Docling-Konvertierung fehlgeschlagen (status={status!r}): "
                f"{_format_errors(data.get('errors'))}"
            )
        return data["document"].get("text_content") or ""

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DoclingClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
