from __future__ import annotations

import hashlib
import logging

from .ai.factory import build_ai_client
from .config import load_ai_config, load_paperless_config
from .db import append_audit_log, get_cached_title, init_db, save_title_result
from .paperless import PaperlessClient
from .prompts import load_prompt_source, render_prompt

logger = logging.getLogger(__name__)

PROMPT_NAME = "title.j2"
MAX_OCR_CHARS = 8000


def _fingerprint(ocr_text: str, prompt_version: str, provider: str, model: str) -> str:
    payload = "\x1f".join([ocr_text, prompt_version, provider, model])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def process_title(
    document_id: int,
    dry_run: bool = False,
    paperless_config_path: str = "config/paperless.yaml",
    ai_config_path: str = "config/ai.yaml",
    db_path: str = "data/archivlotse.db",
) -> str | None:
    """Compute a title for a Paperless document from its OCR text and,
    unless dry_run is set, write it back to Paperless if it changed.

    Already-computed titles are cached in SQLite, keyed by a fingerprint of
    the OCR text, prompt version, AI provider and model, so unchanged
    documents never trigger a repeat AI call.
    """
    paperless_config = load_paperless_config(paperless_config_path)
    ai_config = load_ai_config(ai_config_path)
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

        truncated_text = ocr_text[:MAX_OCR_CHARS]
        prompt_source = load_prompt_source(PROMPT_NAME)
        prompt_version = hashlib.sha256(prompt_source.encode("utf-8")).hexdigest()
        fingerprint = _fingerprint(
            truncated_text, prompt_version, ai_config.provider, ai_config.model
        )

        cached = get_cached_title(conn, document_id, fingerprint)
        if cached is not None:
            title = cached["title"]
            logger.info(
                "Dokument %s: unveränderter Text/Prompt/Modell, nutze zwischengespeicherten Titel %r",
                document_id,
                title,
            )
        else:
            prompt = render_prompt(PROMPT_NAME, ocr_text=truncated_text)
            ai_client = build_ai_client(ai_config)
            logger.info(
                "Dokument %s: frage Titel bei %s/%s an",
                document_id,
                ai_config.provider,
                ai_config.model,
            )
            title = ai_client.generate(prompt).strip().strip('"')
            save_title_result(
                conn,
                document_id=document_id,
                fingerprint=fingerprint,
                title=title,
                provider=ai_config.provider,
                model=ai_config.model,
                prompt_name=PROMPT_NAME,
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
