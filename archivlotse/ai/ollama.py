from __future__ import annotations

import httpx

from ..errors import ServiceError
from .base import CHECK_TIMEOUT, TEMPERATURE, AIResult


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: float = 120.0):
        self._model = model
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def check(self) -> None:
        """Raise unless Ollama is up and has this model pulled (Ollama
        lists an untagged model with its implicit ":latest")."""
        response = self._client.get("/api/tags", timeout=CHECK_TIMEOUT)
        response.raise_for_status()
        names = {entry.get("name") for entry in response.json().get("models") or []}
        if self._model not in names and f"{self._model}:latest" not in names:
            raise ServiceError(f"Ollama hat das Modell {self._model!r} nicht geladen")

    def generate(self, prompt: str) -> AIResult:
        response = self._client.post(
            "/api/generate",
            json={
                "model": self._model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": TEMPERATURE},
            },
        )
        response.raise_for_status()
        data = response.json()
        return AIResult(
            text=data["response"],
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
        )

    def close(self) -> None:
        self._client.close()
