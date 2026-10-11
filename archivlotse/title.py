from __future__ import annotations

import hashlib
import logging

from .ai.factory import build_ai_client
from .config import load_ai_config, load_paperless_config
from .db import append_audit_log, get_cached_title, init_db, save_title_result
from .paperless import PaperlessClient
from .prompts import load_prompt_source, render_prompt
from .steps import StepConfig, load_step_config

logger = logging.getLogger(__name__)

STEP_NAME = "title"


def _fingerprint(
    ocr_text: str, prompt_version: str, provider: str, model: str, postprocessing: str
) -> str:
    payload = "\x1f".join([ocr_text, prompt_version, provider, model, postprocessing])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _postprocess(raw_title: str, step_config: StepConfig) -> str:
    title = " ".join(raw_title.split())
    if step_config.strip_quotes:
        title = title.strip("\"'„“”«»")
    if step_config.max_output_length is not None:
        title = title[: step_config.max_output_length].rstrip()
    return title


def process_title(
    document_id: int,
    dry_run: bool = False,
    ai_role: str | None = None,
    paperless_config_path: str = "config/paperless.yaml",
    ai_config_path: str = "config/ai.yaml",
    steps_config_path: str = "config/steps.yaml",
    db_path: str = "data/archivlotse.db",
) -> str | None:
    """Compute a title for a Paperless document from its OCR text and,
    unless dry_run is set, write it back to Paperless if it changed.

    Which prompt, AI role, and pre-/post-processing rules apply comes from
    the "title" step in config/steps.yaml (ai_role overrides the role
    configured there, if given).

    Already-computed titles are cached in SQLite, keyed by a fingerprint of
    the OCR text, prompt version, AI provider/model and the postprocessing
    rules, so unchanged documents never trigger a repeat AI call.
    """
    step_config = load_step_config(STEP_NAME, steps_config_path)
    paperless_config = load_paperless_config(paperless_config_path)
    ai_config = load_ai_config(ai_role or step_config.ai_role, ai_config_path)
    conn = init_db(db_path)

    with PaperlessClient(paperless_config) as paperless:
        document = paperless.get_document(document_id)
        ocr_text = (document.get("content") or "").strip()
        old_title = document.get("title")

        if not ocr_text:
            logger.warning(
                "Dokument %s hat keinen OCR-Text, Titelerzeugung übersprungen", document_id
            )
            return None

        truncated_text = ocr_text[: step_config.max_input_chars]
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

        cached = get_cached_title(conn, document_id, fingerprint)
        if cached is not None:
            title = cached["title"]
            logger.info(
                "Dokument %s: unveränderter Text/Prompt/Modell/Nachbearbeitung, "
                "nutze zwischengespeicherten Titel %r",
                document_id,
                title,
            )
        else:
            prompt = render_prompt(step_config.prompt, ocr_text=truncated_text)
            ai_client = build_ai_client(ai_config)
            logger.info(
                "Dokument %s: frage Titel bei %s/%s an",
                document_id,
                ai_config.provider,
                ai_config.model,
            )
            raw_title = ai_client.generate(prompt)
            title = _postprocess(raw_title, step_config)
            save_title_result(
                conn,
                document_id=document_id,
                fingerprint=fingerprint,
                title=title,
                provider=ai_config.provider,
                model=ai_config.model,
                prompt_name=step_config.prompt,
                prompt_version=prompt_version,
            )
            logger.info("Dokument %s: neuer Titel berechnet: %r", document_id, title)

        if title == old_title:
            logger.info(
                "Dokument %s: Titel unverändert (%r), keine Aktualisierung nötig",
                document_id,
                title,
            )
            return title

        if dry_run:
            logger.info(
                "Dokument %s: [DRY-RUN] Titel würde geändert: %r -> %r",
                document_id,
                old_title,
                title,
            )
            return title

        paperless.update_document_title(document_id, title)
        append_audit_log(conn, document_id, old_title, title)
        logger.info(
            "Dokument %s: Titel in Paperless geändert: %r -> %r", document_id, old_title, title
        )
        return title
