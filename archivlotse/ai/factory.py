from __future__ import annotations

from ..config import AIConfig
from .ollama import OllamaClient
from .scaleway import OpenAICompatibleClient


def build_ai_client(config: AIConfig):
    if config.provider == "ollama":
        return OllamaClient(base_url=config.base_url, model=config.model)
    if config.provider == "scaleway":
        if not config.api_key:
            raise ValueError("Scaleway provider requires 'api_key' in config/ai.yaml")
        return OpenAICompatibleClient(
            base_url=config.base_url, model=config.model, api_key=config.api_key
        )
    raise ValueError(f"Unknown AI provider: {config.provider!r}")
