from __future__ import annotations

import httpx

from ..errors import ServiceError
from .base import AIResult


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
    """OCR client for a Docling Serve instance - same ocr_pdf interface as
    OpenAICompatibleClient, so the OCR step doesn't care which one it got.
    Every page is OCR'd anew (force_ocr): the OCR step only sends scans,
    whose existing text layer is exactly what's being replaced."""

    def __init__(self, base_url: str, timeout: float = 600.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def ocr_pdf(self, pdf_bytes: bytes, filename: str) -> AIResult:
        response = self._client.post(
            "/v1/convert/file",
            files={"files": (filename, pdf_bytes, "application/pdf")},
            data={"to_formats": "text", "force_ocr": "true"},
        )
        response.raise_for_status()
        data = response.json()
        status = data.get("status")
        if status not in ("success", "partial_success"):
            raise ServiceError(
                f"Docling-Konvertierung fehlgeschlagen (status={status!r}): "
                f"{_format_errors(data.get('errors'))}"
            )
        return AIResult(text=(data.get("document") or {}).get("text_content") or "")

    def close(self) -> None:
        self._client.close()
