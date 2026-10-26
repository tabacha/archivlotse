from __future__ import annotations

from ..config import AIConfig
from .docling import DoclingClient
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


def build_ocr_client(config: AIConfig, timeout: float) -> OpenAICompatibleClient | DoclingClient:
    """Client for the OCR step (`ocr_pdf`): a local Docling Serve
    (provider type "docling") or an OCR model behind an OpenAI-compatible
    API that accepts a PDF as "file" content (scaleway, e.g. LightOnOCR) -
    an Ollama model can't be used here. `timeout` overrides the
    provider's: OCR of a long document takes far longer than an ordinary
    prompt."""
    if config.provider == "docling":
        return DoclingClient(base_url=config.base_url, timeout=timeout)
    if config.provider != "scaleway":
        raise ValueError(
            f"OCR-Modell {config.model!r}: Provider {config.provider!r} wird für OCR nicht "
            "unterstützt (nur 'docling' und 'scaleway')"
        )
    if not config.secret_key:
        raise ValueError("Scaleway provider requires 'secret_key' in config/ai.yaml")
    return OpenAICompatibleClient(
        base_url=config.base_url,
        model=config.model,
        secret_key=config.secret_key,
        timeout=timeout,
    )
