from __future__ import annotations

from ..config import AIConfig
from .ollama import OllamaClient
from .scaleway import OpenAICompatibleClient


def build_ai_client(config: AIConfig):
    if config.provider == "ollama":
        return OllamaClient(base_url=config.base_url, model=config.model, timeout=config.timeout)
    if config.provider == "scaleway":
        if not config.secret_key:
            raise ValueError("Scaleway provider requires 'secret_key' in config/ai.yaml")
        return OpenAICompatibleClient(
            base_url=config.base_url,
            model=config.model,
            secret_key=config.secret_key,
            timeout=config.timeout,
        )
    raise ValueError(f"Unknown AI provider: {config.provider!r}")
