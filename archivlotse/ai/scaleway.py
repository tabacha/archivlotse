from __future__ import annotations

import base64

import httpx

from ..errors import ServiceError
from .base import TEMPERATURE, AIResult


class OpenAICompatibleClient:
    """Chat-completion client for Scaleway Model-as-a-Service and other
    OpenAI-compatible APIs.

    Scaleway's Generative APIs authenticate with the Secret Key half of
    an IAM API key pair (sent as a Bearer token); the Access Key ID is
    not needed for this endpoint, only kept in config for reference."""

    def __init__(self, base_url: str, model: str, secret_key: str, timeout: float = 120.0):
        self._model = model
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {secret_key}"},
            timeout=timeout,
        )

    def generate(self, prompt: str) -> AIResult:
        response = self._client.post(
            "/chat/completions",
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": TEMPERATURE,
            },
        )
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage") or {}
        return AIResult(
            text=data["choices"][0]["message"]["content"],
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    def ocr_pdf(self, pdf_bytes: bytes, filename: str) -> AIResult:
        """Transcribe a whole PDF with an OCR model (e.g. LightOnOCR), which
        takes the file itself as the only message content - no prompt.
        Scaleway only accepts PDFs here, not images. A transcription cut off
        at the token limit is an error, not a result: writing it back would
        silently drop the rest of the document."""
        file_data = "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode("ascii")
        response = self._client.post(
            "/chat/completions",
            json={
                "model": self._model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "file", "file": {"file_data": file_data, "filename": filename}}
                        ],
                    }
                ],
                "temperature": TEMPERATURE,
            },
        )
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ServiceError(
                f"OCR-Ergebnis von {self._model!r} wurde am Token-Limit abgeschnitten "
                "(Dokument zu lang?) - nicht übernommen"
            )
        usage = data.get("usage") or {}
        return AIResult(
            text=choice["message"]["content"] or "",
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    def close(self) -> None:
        self._client.close()
