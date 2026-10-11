from __future__ import annotations

import httpx


class OpenAICompatibleClient:
    """Chat-completion client for Scaleway Model-as-a-Service and other
    OpenAI-compatible APIs."""

    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 120.0):
        self._model = model
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def generate(self, prompt: str) -> str:
        response = self._client.post(
            "/chat/completions",
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def close(self) -> None:
        self._client.close()
