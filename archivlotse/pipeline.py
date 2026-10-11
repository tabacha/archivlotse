from __future__ import annotations

import hashlib
import logging

from .ai.factory import build_ai_client
from .config import load_ai_config, load_paperless_config
from .db import append_audit_log, get_cached_step_result, init_db, save_step_result
from .paperless import PaperlessClient
from .prompts import load_prompt_source, render_prompt
from .steps import StepConfig, load_step_config

logger = logging.getLogger(__name__)


def _fingerprint(
    input_text: str, prompt_version: str, provider: str, model: str, postprocessing: str
) -> str:
    payload = "\x1f".join([input_text, prompt_version, provider, model, postprocessing])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _postprocess(raw_output: str, step_config: StepConfig) -> str:
    value = " ".join(raw_output.split())
    if step_config.strip_quotes:
        value = value.strip("\"'„“”«»")
    if step_config.max_output_length is not None:
        value = value[: step_config.max_output_length].rstrip()
    return value


def process_step(
    step_name: str,
    document_id: int,
    dry_run: bool = False,
    ai_role: str | None = None,
    paperless_config_path: str = "config/paperless.yaml",
    ai_config_path: str = "config/ai.yaml",
    steps_config_path: str = "config/steps.yaml",
    db_path: str = "data/archivlotse.db",
) -> str | None:
    """Run one config-defined processing step for a Paperless document.

    Everything step-specific - which Paperless field is read as input,
    which prompt and AI role are used, how the raw AI output is cleaned
    up, and which Paperless field the result is written to - comes from
    the step named `step_name` in config/steps.yaml. This function itself
    has no knowledge of "title" or any other particular field/step.

    Already-computed results are cached in SQLite, keyed by a fingerprint
    of the input text, prompt version, AI provider/model and the
    postprocessing rules, so unchanged documents never trigger a repeat
    AI call. The audit log only ever gets a row when a field is actually
    written to Paperless (never on cache hits, no-ops, or --dry-run).
    """
    step_config = load_step_config(step_name, steps_config_path)
    paperless_config = load_paperless_config(paperless_config_path)
    ai_config = load_ai_config(ai_role or step_config.ai_role, ai_config_path)
    conn = init_db(db_path)

    with PaperlessClient(paperless_config) as paperless:
        document = paperless.get_document(document_id)
        input_text = str(document.get(step_config.source_field) or "").strip()
        old_value = document.get(step_config.target_field)

        if not input_text:
            logger.warning(
                "Dokument %s: Feld %r ist leer, Schritt %r übersprungen",
                document_id,
                step_config.source_field,
                step_name,
            )
            return None

        truncated_text = input_text[: step_config.max_input_chars]
        prompt_source = load_prompt_source(step_config.prompt)
        prompt_version = hashlib.sha256(prompt_source.encode("utf-8")).hexdigest()
        postprocessing_signature = (
            f"strip_quotes={step_config.strip_quotes};"
            f"max_output_length={step_config.max_output_length}"
        )
        fingerprint = _fingerprint(
            truncated_text,
            prompt_version,
            ai_config.provider,
            ai_config.model,
            postprocessing_signature,
        )

        cached = get_cached_step_result(conn, step_name, document_id, fingerprint)
        if cached is not None:
            value = cached["output_value"]
            logger.info(
                "Dokument %s, Schritt %r: unveränderte Eingabe/Prompt/Modell/Nachbearbeitung, "
                "nutze zwischengespeichertes Ergebnis %r",
                document_id,
                step_name,
                value,
            )
        else:
            prompt = render_prompt(step_config.prompt, input_text=truncated_text)
            ai_client = build_ai_client(ai_config)
            logger.info(
                "Dokument %s, Schritt %r: frage Ergebnis bei %s/%s an",
                document_id,
                step_name,
                ai_config.provider,
                ai_config.model,
            )
            raw_output = ai_client.generate(prompt)
            value = _postprocess(raw_output, step_config)
            save_step_result(
                conn,
                step_name=step_name,
                document_id=document_id,
                fingerprint=fingerprint,
                output_value=value,
                provider=ai_config.provider,
                model=ai_config.model,
                prompt_name=step_config.prompt,
                prompt_version=prompt_version,
            )
            logger.info(
                "Dokument %s, Schritt %r: neues Ergebnis berechnet: %r", document_id, step_name, value
            )

        if value == old_value:
            logger.info(
                "Dokument %s, Feld %r: unverändert (%r), keine Aktualisierung nötig",
                document_id,
                step_config.target_field,
                value,
            )
            return value

        if dry_run:
            logger.info(
                "Dokument %s, Feld %r: [DRY-RUN] würde geändert: %r -> %r",
                document_id,
                step_config.target_field,
                old_value,
                value,
            )
            return value

        paperless.update_document_field(document_id, step_config.target_field, value)
        append_audit_log(conn, document_id, step_config.target_field, old_value, value)
        logger.info(
            "Dokument %s, Feld %r in Paperless geändert: %r -> %r",
            document_id,
            step_config.target_field,
            old_value,
            value,
        )
        return value
