from __future__ import annotations

import httpx


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: float = 120.0):
        self._model = model
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def generate(self, prompt: str) -> str:
        response = self._client.post(
            "/api/generate",
            json={"model": self._model, "prompt": prompt, "stream": False},
        )
        response.raise_for_status()
        return response.json()["response"]

    def close(self) -> None:
        self._client.close()
