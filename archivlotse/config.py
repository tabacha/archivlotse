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
    access_key_id: str | None = None
    secret_key: str | None = None
    # Preis je 1 Million Tokens (Input/Output), fuer die Kostenschaetzung
    # pro Aufruf. Bei "ollama" (lokal) wird unabhaengig davon mit $0
    # gerechnet - diese Felder sind nur fuer Cloud-Provider relevant.
    price_per_1m_input: float | None = None
    price_per_1m_output: float | None = None
    # Wie lange (Sekunden) auf eine Antwort gewartet wird, bevor der
    # Request abgebrochen wird. Lokales Ollama (insb. CPU-Inferenz oder
    # ein "kaltes", noch nicht geladenes Modell) kann deutlich laenger
    # brauchen als ein Cloud-Dienst - pro Provider in config/ai.yaml
    # einstellbar statt hartcodiert.
    timeout: float = 120.0


def load_yaml(path: str | Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    text = os.path.expandvars(text)
    return yaml.safe_load(text) or {}


def load_paperless_config(path: str | Path = "config/paperless.yaml") -> PaperlessConfig:
    data = load_yaml(path)
    return PaperlessConfig(url=data["url"].rstrip("/"), token=data["token"])


def load_ai_models(path: str | Path = "config/ai.yaml") -> dict[str, AIConfig]:
    """Load all models from config/ai.yaml.

    The file defines reusable "providers" (connection details, incl. any
    credentials) once, and "models" keyed by model name, each pointing at
    the provider that serves it (plus optional pricing). Steps in
    config/steps.yaml reference these model names directly. This way cloud
    credentials are only ever written once, even if several models use them.

    The key is sent to the provider as the model name, unless the entry
    sets an explicit `model:` - useful when the same model is available
    from two providers and needs two distinct keys.
    """
    data = load_yaml(path)
    providers = data.get("providers", {})
    models_data = data.get("models", {})

    if "roles" in data and not models_data:
        raise ValueError(
            f"{path} benutzt noch das alte 'roles:'-Format - bitte auf 'models:' umstellen "
            "(Modellname -> provider, siehe config/ai.example.yaml)"
        )

    models: dict[str, AIConfig] = {}
    for model_name, entry in models_data.items():
        entry = entry or {}
        provider_name = entry.get("provider")
        try:
            provider = providers[provider_name]
        except KeyError as exc:
            raise ValueError(
                f"Modell {model_name!r} in {path} verweist auf unbekannten "
                f"Provider {provider_name!r}"
            ) from exc
        models[model_name] = AIConfig(
            provider=provider["type"],
            base_url=provider["base_url"].rstrip("/"),
            model=entry.get("model", model_name),
            access_key_id=provider.get("access_key_id"),
            secret_key=provider.get("secret_key"),
            price_per_1m_input=entry.get("price_per_1m_input"),
            price_per_1m_output=entry.get("price_per_1m_output"),
            timeout=provider.get("timeout", 120.0),
        )
    return models


def load_ai_config(model: str, path: str | Path = "config/ai.yaml") -> AIConfig:
    models = load_ai_models(path)
    try:
        return models[model]
    except KeyError as exc:
        available = ", ".join(sorted(models)) or "(keine)"
        raise ValueError(
            f"Unbekanntes Modell {model!r} in {path} (models:). Verfügbare Modelle: {available}"
        ) from exc
