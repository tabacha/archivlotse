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


def load_yaml(path: str | Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    text = os.path.expandvars(text)
    return yaml.safe_load(text) or {}


def load_paperless_config(path: str | Path = "config/paperless.yaml") -> PaperlessConfig:
    data = load_yaml(path)
    return PaperlessConfig(url=data["url"].rstrip("/"), token=data["token"])


def load_ai_roles(path: str | Path = "config/ai.yaml") -> dict[str, AIConfig]:
    """Load all model roles from config/ai.yaml.

    The file defines reusable "providers" (connection details, incl. any
    API key) once, and "roles" (logical tasks like "title") that each pick
    a provider + model. This way a cloud API key is only ever written
    once, even if several roles use it with different models.
    """
    data = load_yaml(path)
    providers = data.get("providers", {})
    roles_data = data.get("roles", {})

    roles: dict[str, AIConfig] = {}
    for role_name, role in roles_data.items():
        provider_name = role["provider"]
        try:
            provider = providers[provider_name]
        except KeyError as exc:
            raise ValueError(
                f"Rolle {role_name!r} in {path} verweist auf unbekannten "
                f"Provider {provider_name!r}"
            ) from exc
        roles[role_name] = AIConfig(
            provider=provider["type"],
            base_url=provider["base_url"].rstrip("/"),
            model=role["model"],
            api_key=provider.get("api_key"),
        )
    return roles


def load_ai_config(role: str, path: str | Path = "config/ai.yaml") -> AIConfig:
    roles = load_ai_roles(path)
    try:
        return roles[role]
    except KeyError as exc:
        available = ", ".join(sorted(roles)) or "(keine)"
        raise ValueError(
            f"Unbekannte Modellrolle {role!r} in {path}. Verfügbare Rollen: {available}"
        ) from exc
