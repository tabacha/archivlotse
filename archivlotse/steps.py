from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import load_yaml


@dataclass
class StepConfig:
    name: str
    prompt: str
    ai_role: str
    source_field: str
    target_field: str
    max_input_chars: int = 8000
    strip_quotes: bool = True
    max_output_length: int | None = None


def load_step_config(name: str, path: str | Path = "config/steps.yaml") -> StepConfig:
    """Load the pipeline definition for one processing step.

    Each step in config/steps.yaml fully describes one field-to-field AI
    call: which Paperless field to read (source_field), which Jinja2
    prompt and AI role (from config/ai.yaml) to use, simple pre-/post-
    processing rules (input truncation, output cleanup), and which
    Paperless field to write the result to (target_field). The Python
    pipeline code itself has no knowledge of what "title" or any other
    specific field/step means - that all lives here.
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
        source_field=step["source_field"],
        target_field=step["target_field"],
        max_input_chars=preprocessing.get("max_input_chars", 8000),
        strip_quotes=postprocessing.get("strip_quotes", True),
        max_output_length=postprocessing.get("max_length"),
    )
