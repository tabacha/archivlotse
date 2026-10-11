from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Callable

from .ai.factory import build_ai_client
from .config import AIConfig, load_ai_config, load_paperless_config
from .db import append_audit_log, get_cached_step_result, init_db, save_step_result
from .paperless import PaperlessClient
from .prompts import load_prompt_source, render_prompt
from .steps import StepConfig, load_step_config

logger = logging.getLogger(__name__)

# Answer the AI is instructed to give (see archivlotse/prompts/document_type.j2)
# when none of the offered candidates fits - handled the same as a rejected
# suggestion (nothing is written back), but logged as an expected outcome
# rather than a validation failure.
UNKNOWN_SENTINEL = "UNBEKANNT"

CANDIDATE_FETCHERS: dict[str, Callable[[PaperlessClient], list[dict]]] = {
    "document_types": lambda paperless: paperless.list_document_types(),
}


def _fingerprint(*parts: str) -> str:
    payload = "\x1f".join(parts)
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

    Everything step-specific comes from the step named `step_name` in
    config/steps.yaml (see StepConfig/load_step_config). Two shapes are
    supported:

    - Plain field-to-field (default): render the prompt from
      source_field's text and write the AI's (cleaned-up) answer verbatim
      to target_field.
    - Choice from candidates (step has `candidates_endpoint` set): fetch
      an existing Paperless list (e.g. document types) via the Paperless
      API, give the prompt the candidate names, and only ever write back
      a candidate the AI's answer actually matched - a hallucinated or
      unparseable answer is rejected, never written to Paperless.

    Either way, already-computed results are cached in SQLite (keyed by a
    fingerprint of the input, prompt version, AI provider/model,
    postprocessing rules, and - for choice steps - the candidate list), so
    unchanged documents never trigger a repeat AI call. The audit log only
    ever gets a row when a field is actually written to Paperless (never
    on cache hits, no-ops, rejected suggestions, or --dry-run).
    """
    step_config = load_step_config(step_name, steps_config_path)
    paperless_config = load_paperless_config(paperless_config_path)
    ai_config = load_ai_config(ai_role or step_config.ai_role, ai_config_path)
    conn = init_db(db_path)

    with PaperlessClient(paperless_config) as paperless:
        if step_config.candidates_endpoint:
            return _process_choice_step(step_config, ai_config, paperless, conn, document_id, dry_run)
        return _process_text_step(step_config, ai_config, paperless, conn, document_id, dry_run)


def _process_text_step(
    step_config: StepConfig,
    ai_config: AIConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    dry_run: bool,
) -> str | None:
    document = paperless.get_document(document_id)
    input_text = str(document.get(step_config.source_field) or "").strip()
    old_value = document.get(step_config.target_field)

    if not input_text:
        logger.warning(
            "Dokument %s: Feld %r ist leer, Schritt %r übersprungen",
            document_id,
            step_config.source_field,
            step_config.name,
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

    cached = get_cached_step_result(conn, step_config.name, document_id, fingerprint)
    if cached is not None:
        value = cached["output_value"]
        logger.info(
            "Dokument %s, Schritt %r: unveränderte Eingabe/Prompt/Modell/Nachbearbeitung, "
            "nutze zwischengespeichertes Ergebnis %r",
            document_id,
            step_config.name,
            value,
        )
    else:
        prompt = render_prompt(step_config.prompt, input_text=truncated_text)
        ai_client = build_ai_client(ai_config)
        logger.info(
            "Dokument %s, Schritt %r: frage Ergebnis bei %s/%s an",
            document_id,
            step_config.name,
            ai_config.provider,
            ai_config.model,
        )
        raw_output = ai_client.generate(prompt)
        value = _postprocess(raw_output, step_config)
        save_step_result(
            conn,
            step_name=step_config.name,
            document_id=document_id,
            fingerprint=fingerprint,
            output_value=value,
            provider=ai_config.provider,
            model=ai_config.model,
            prompt_name=step_config.prompt,
            prompt_version=prompt_version,
        )
        logger.info(
            "Dokument %s, Schritt %r: neues Ergebnis berechnet: %r",
            document_id,
            step_config.name,
            value,
        )

    return _apply_value(step_config, paperless, conn, document_id, old_value, value, dry_run)


def _process_choice_step(
    step_config: StepConfig,
    ai_config: AIConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    dry_run: bool,
) -> str | None:
    document = paperless.get_document(document_id)
    input_text = str(document.get(step_config.source_field) or "").strip()
    old_id = document.get(step_config.target_field)

    if not input_text:
        logger.warning(
            "Dokument %s: Feld %r ist leer, Schritt %r übersprungen",
            document_id,
            step_config.source_field,
            step_config.name,
        )
        return None

    try:
        fetch_candidates = CANDIDATE_FETCHERS[step_config.candidates_endpoint]
    except KeyError as exc:
        available = ", ".join(sorted(CANDIDATE_FETCHERS))
        raise ValueError(
            f"Unbekannter candidates_endpoint {step_config.candidates_endpoint!r} für "
            f"Schritt {step_config.name!r}. Verfügbar: {available}"
        ) from exc

    candidates = fetch_candidates(paperless)
    name_by_id = {candidate["id"]: candidate["name"] for candidate in candidates}
    id_by_normalized_name = {
        candidate["name"].strip().casefold(): candidate["id"] for candidate in candidates
    }
    old_value = name_by_id.get(old_id, old_id)

    truncated_text = input_text[: step_config.max_input_chars]
    prompt_source = load_prompt_source(step_config.prompt)
    prompt_version = hashlib.sha256(prompt_source.encode("utf-8")).hexdigest()
    postprocessing_signature = (
        f"strip_quotes={step_config.strip_quotes};"
        f"max_output_length={step_config.max_output_length}"
    )
    candidates_signature = "|".join(sorted(candidate["name"] for candidate in candidates))
    fingerprint = _fingerprint(
        truncated_text,
        prompt_version,
        ai_config.provider,
        ai_config.model,
        postprocessing_signature,
        candidates_signature,
    )

    cached = get_cached_step_result(conn, step_config.name, document_id, fingerprint)
    if cached is not None:
        suggestion = cached["output_value"]
        logger.info(
            "Dokument %s, Schritt %r: unveränderte Eingabe/Kandidaten/Prompt/Modell, "
            "nutze zwischengespeicherten Vorschlag %r",
            document_id,
            step_config.name,
            suggestion,
        )
    else:
        prompt = render_prompt(
            step_config.prompt,
            input_text=truncated_text,
            candidate_names=[candidate["name"] for candidate in candidates],
        )
        ai_client = build_ai_client(ai_config)
        logger.info(
            "Dokument %s, Schritt %r: frage Vorschlag bei %s/%s an (%d Kandidaten)",
            document_id,
            step_config.name,
            ai_config.provider,
            ai_config.model,
            len(candidates),
        )
        raw_output = ai_client.generate(prompt)
        suggestion = _postprocess(raw_output, step_config)
        save_step_result(
            conn,
            step_name=step_config.name,
            document_id=document_id,
            fingerprint=fingerprint,
            output_value=suggestion,
            provider=ai_config.provider,
            model=ai_config.model,
            prompt_name=step_config.prompt,
            prompt_version=prompt_version,
        )
        logger.info(
            "Dokument %s, Schritt %r: neuer Vorschlag: %r",
            document_id,
            step_config.name,
            suggestion,
        )

    if suggestion.strip().casefold() == UNKNOWN_SENTINEL.casefold():
        logger.info(
            "Dokument %s, Schritt %r: KI konnte keinen passenden Wert bestimmen (%s), "
            "Feld %r bleibt unverändert",
            document_id,
            step_config.name,
            UNKNOWN_SENTINEL,
            step_config.target_field,
        )
        return None

    matched_id = id_by_normalized_name.get(suggestion.strip().casefold())
    if matched_id is None:
        logger.warning(
            "Dokument %s, Schritt %r: KI-Vorschlag %r entspricht keinem vorhandenen Kandidaten, "
            "wird verworfen und NICHT in Paperless übernommen",
            document_id,
            step_config.name,
            suggestion,
        )
        return None

    matched_name = name_by_id[matched_id]
    return _apply_value(
        step_config,
        paperless,
        conn,
        document_id,
        old_value,
        matched_name,
        dry_run,
        write_value=matched_id,
    )


def _apply_value(
    step_config: StepConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    old_value,
    new_value: str,
    dry_run: bool,
    write_value: int | str | None = None,
) -> str:
    """Compare old vs. new value and, unless unchanged or dry_run, write
    write_value (defaulting to new_value) to Paperless and audit-log the
    human-readable old_value/new_value pair."""
    if write_value is None:
        write_value = new_value

    if new_value == old_value:
        logger.info(
            "Dokument %s, Feld %r: unverändert (%r), keine Aktualisierung nötig",
            document_id,
            step_config.target_field,
            new_value,
        )
        return new_value

    if dry_run:
        logger.info(
            "Dokument %s, Feld %r: [DRY-RUN] würde geändert: %r -> %r",
            document_id,
            step_config.target_field,
            old_value,
            new_value,
        )
        return new_value

    paperless.update_document_field(document_id, step_config.target_field, write_value)
    append_audit_log(conn, document_id, step_config.target_field, old_value, new_value)
    logger.info(
        "Dokument %s, Feld %r in Paperless geändert: %r -> %r",
        document_id,
        step_config.target_field,
        old_value,
        new_value,
    )
    return new_value
