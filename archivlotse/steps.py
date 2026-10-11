from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import load_yaml


@dataclass
class StepConfig:
    name: str
    prompt: str
    ai_role: str
    max_input_chars: int = 8000
    strip_quotes: bool = True
    max_output_length: int | None = None


def load_step_config(name: str, path: str | Path = "config/steps.yaml") -> StepConfig:
    """Load the pipeline definition for one processing step.

    Each step in config/steps.yaml names its Jinja2 prompt, the AI role
    (from config/ai.yaml) it uses, and simple pre-/post-processing rules
    (input truncation, output cleanup) applied around the AI call.
    """
    data = load_yaml(path)
    steps = data.get("steps", {})
    try:
        step = steps[name]
    except KeyError as exc:
        available = ", ".join(sorted(steps)) or "(keine)"
        raise ValueError(
            f"Unbekannter Verarbeitungsschritt {name!r} in {path}. "
            f"Verfügbare Schritte: {available}"
        ) from exc

    preprocessing = step.get("preprocessing", {}) or {}
    postprocessing = step.get("postprocessing", {}) or {}

    return StepConfig(
        name=name,
        prompt=step["prompt"],
        ai_role=step["ai_role"],
        max_input_chars=preprocessing.get("max_input_chars", 8000),
        strip_quotes=postprocessing.get("strip_quotes", True),
        max_output_length=postprocessing.get("max_length"),
    )
