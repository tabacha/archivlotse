from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class PaperlessConfig:
    url: str
    token: str


@dataclass
class AIConfig:
    provider: str
    base_url: str
    model: str
    api_key: str | None = None


def _load_yaml(path: str | Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    text = os.path.expandvars(text)
    return yaml.safe_load(text) or {}


def load_paperless_config(path: str | Path = "config/paperless.yaml") -> PaperlessConfig:
    data = _load_yaml(path)
    return PaperlessConfig(url=data["url"].rstrip("/"), token=data["token"])


def load_ai_config(path: str | Path = "config/ai.yaml") -> AIConfig:
    data = _load_yaml(path)
    provider = data["provider"]
    provider_data = data[provider]
    return AIConfig(
        provider=provider,
        base_url=provider_data["base_url"].rstrip("/"),
        model=provider_data["model"],
        api_key=provider_data.get("api_key"),
    )
