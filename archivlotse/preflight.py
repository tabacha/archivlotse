from __future__ import annotations

import logging
from pathlib import Path

import httpx

from .ai.factory import build_ai_client, build_ocr_client
from .config import load_ai_config, load_paperless_config
from .errors import ServiceError, describe_expected_error
from .paperless import PaperlessClient
from .steps import load_step_config

logger = logging.getLogger(__name__)


def _required_models(
    step_names: list[str], model: str | None, steps_config_path: str | Path
) -> list[tuple[str, bool]]:
    """The distinct (model name, is OCR model) pairs the given steps are
    configured with. Decided from the config alone: whether a step would
    actually call its model depends on the document (scan or not, condition,
    cache), which isn't known yet - a configured model counts as needed."""
    required: dict[tuple[str, bool], None] = {}
    for step_name in step_names:
        step_config = load_step_config(step_name, steps_config_path)
        if step_config.add_tags:
            continue
        if step_config.ocr_model:
            required[(model or step_config.ocr_model, True)] = None
            continue
        required[(model or step_config.model, False)] = None
        if step_config.model_step2:
            required[(step_config.model_step2, False)] = None
    return list(required)


def _check_model(model_name: str, is_ocr: bool, ai_config_path: str | Path) -> None:
    ai_config = load_ai_config(model_name, ai_config_path)
    if is_ocr:
        client = build_ocr_client(ai_config, ai_config.timeout)
    else:
        client = build_ai_client(ai_config)
    try:
        client.check()
    finally:
        client.close()


def _check_paperless(paperless_config_path: str | Path) -> None:
    with PaperlessClient(load_paperless_config(paperless_config_path)) as paperless:
        paperless.check()


def check_services(
    step_names: list[str],
    model: str | None = None,
    paperless_config_path: str | Path = "config/paperless.yaml",
    ai_config_path: str | Path = "config/ai.yaml",
    steps_config_path: str | Path = "config/steps.yaml",
) -> None:
    """Refuse to start unless Paperless and every AI/OCR service the given
    steps are configured with is reachable and knows its model. Only free
    status/list endpoints are called, no AI request. All problems are
    collected and raised together as one ServiceError, so a run with two
    services down doesn't need two attempts to find out."""
    checks = [("Paperless", lambda: _check_paperless(paperless_config_path))]
    for model_name, is_ocr in _required_models(step_names, model, steps_config_path):
        checks.append(
            (
                f"Modell {model_name!r}",
                lambda name=model_name, ocr=is_ocr: _check_model(name, ocr, ai_config_path),
            )
        )

    failures: list[str] = []
    for label, check in checks:
        try:
            check()
        except (httpx.HTTPError, ServiceError, ValueError) as exc:
            failures.append(f"{label}: {describe_expected_error(exc) or exc}")
        else:
            logger.debug("Vorabprüfung ok: %s", label)
    if failures:
        raise ServiceError("Start verweigert, benötigte Dienste fehlen - " + "; ".join(failures))
