from __future__ import annotations

import httpx

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

    def close(self) -> None:
        self._client.close()
