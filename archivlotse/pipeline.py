from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from typing import Callable

from .ai.factory import build_ai_client
from .config import AIConfig, load_ai_config, load_paperless_config
from .db import (
    append_audit_log,
    get_cached_candidates,
    get_cached_step_result,
    init_db,
    save_candidates,
    save_step_result,
)
from .docling import DoclingClient
from .ocr_quality import ocr_quality_score
from .paperless import PaperlessClient
from .prompts import load_prompt_source, render_prompt
from .steps import StepConfig, load_pipeline_order, load_step_config

logger = logging.getLogger(__name__)

# Answer the AI is instructed to give (see config/prompts/document_type.j2)
# for a single-choice step when none of the offered candidates fits -
# handled the same as a rejected suggestion (nothing is written back), but
# logged as an expected outcome rather than a validation failure.
UNKNOWN_SENTINEL = "UNBEKANNT"

# Answer for a multi-choice step (e.g. tags) when none of the offered
# candidates applies (for either the add or the delete list) - an
# explicitly empty selection, not an error.
NONE_SENTINEL = "KEINE"

# Multi-choice prompts (e.g. config/prompts/tags.j2) answer in two labeled
# lines, one for candidates to add and one for candidates to remove, so
# the AI can also flag existing values that no longer fit - see
# _parse_add_delete_lines().
_ADD_LINE_RE = re.compile(r"^\s*Hinzuf(?:ü|ue)gen\s*:\s*(.*)$", re.IGNORECASE)
_DELETE_LINE_RE = re.compile(r"^\s*L(?:ö|oe)schen\s*:\s*(.*)$", re.IGNORECASE)

CANDIDATE_FETCHERS: dict[str, Callable[[PaperlessClient], list[dict]]] = {
    "document_types": lambda paperless: paperless.list_document_types(),
    "tags": lambda paperless: paperless.list_tags(),
}

# How long a fetched candidate list (document types, tags, ...) is reused
# across separate `archivlotse process` invocations before being
# re-fetched from Paperless - these lists change rarely, and nearly every
# step needs one (directly, or indirectly via document_type_name), so
# caching avoids a Paperless round-trip per step.
CANDIDATE_CACHE_TTL_SECONDS = 2 * 60 * 60  # 2 Stunden

# A new OCR result must be at least this fraction of the previous text's
# length to be accepted - a cheap sanity floor against Docling returning
# near-empty output (e.g. on an unsupported/corrupt file), independent of
# the quality-score comparison (see ocr_quality.py for why before/after
# scores aren't compared directly).
MIN_OCR_LENGTH_RATIO = 0.3


def _fingerprint(*parts: str) -> str:
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_candidates(conn: sqlite3.Connection, paperless: PaperlessClient, endpoint: str) -> list[dict]:
    """Fetch a candidate list (e.g. Paperless tags/document types) for
    `endpoint`, reusing a cached copy (see CANDIDATE_CACHE_TTL_SECONDS) if
    one is still fresh enough instead of hitting Paperless again."""
    cached = get_cached_candidates(conn, endpoint, CANDIDATE_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    try:
        fetch = CANDIDATE_FETCHERS[endpoint]
    except KeyError as exc:
        available = ", ".join(sorted(CANDIDATE_FETCHERS))
        raise ValueError(
            f"Unbekannter candidates_endpoint {endpoint!r}. Verfügbar: {available}"
        ) from exc
    candidates = fetch(paperless)
    save_candidates(conn, endpoint, candidates)
    return candidates


def _resolve_document_type_name(
    document: dict, paperless: PaperlessClient, conn: sqlite3.Connection
) -> str | None:
    """Resolve the document's current document_type ID to its name, so
    it can be used in step conditions and exposed to prompt templates as
    `document_type_name`. Returns None if no document_type is set."""
    type_id = document.get("document_type")
    if type_id is None:
        return None
    for candidate in _get_candidates(conn, paperless, "document_types"):
        if candidate["id"] == type_id:
            return candidate["name"]
    return None


def _condition_met(step_config: StepConfig, document: dict, document_type_name: str | None) -> bool:
    if not step_config.condition:
        return True
    for field, expected in step_config.condition.items():
        actual = document_type_name if field == "document_type" else document.get(field)
        if actual != expected:
            return False
    return True


def _estimate_cost(
    ai_config: AIConfig, prompt_tokens: int | None, completion_tokens: int | None
) -> float | None:
    """Estimate the USD cost of one AI call from its token usage. Ollama
    runs locally, so it's always treated as free ($0), independent of
    whether pricing is configured. For other providers, returns None if
    pricing (config/ai.yaml: price_per_1m_input/output) or usage data
    isn't available, rather than silently guessing $0."""
    if ai_config.provider == "ollama":
        return 0.0
    if ai_config.price_per_1m_input is None or ai_config.price_per_1m_output is None:
        return None
    if prompt_tokens is None or completion_tokens is None:
        return None
    return (
        prompt_tokens / 1_000_000 * ai_config.price_per_1m_input
        + completion_tokens / 1_000_000 * ai_config.price_per_1m_output
    )


def _log_usage(document_id: int, step_name: str, ai_config: AIConfig, ai_result) -> float | None:
    cost = _estimate_cost(ai_config, ai_result.prompt_tokens, ai_result.completion_tokens)
    cost_str = f"${cost:.6f}" if cost is not None else "unbekannt (kein Preis konfiguriert)"
    logger.info(
        "Dokument %s, Schritt %r: Tokenverbrauch prompt=%s completion=%s, geschätzte Kosten: %s",
        document_id,
        step_name,
        ai_result.prompt_tokens,
        ai_result.completion_tokens,
        cost_str,
    )
    return cost


def _postprocess(raw_output: str, step_config: StepConfig) -> str:
    value = " ".join(raw_output.split())
    if step_config.strip_quotes:
        value = value.strip("\"'„“”«»")
    if step_config.max_output_length is not None:
        value = value[: step_config.max_output_length].rstrip()
    return value


def _clean_multiline(raw_output: str, step_config: StepConfig) -> str:
    """Like _postprocess, but preserves line breaks - for a multi-choice
    answer laid out as labeled "Hinzufügen:"/"Löschen:" lines, where
    _postprocess's newline-collapsing would merge both lines into one."""
    value = raw_output.strip()
    if step_config.max_output_length is not None:
        value = value[: step_config.max_output_length].rstrip()
    return value


def _parse_add_delete_lines(text: str) -> tuple[str, str]:
    """Parse a two-line 'Hinzufügen: ...' / 'Löschen: ...' multi-choice
    answer into (add_part, delete_part) raw strings. Falls back to
    treating the whole text as the add-list (delete part empty) if
    neither label is found, so a plain comma-list/KEINE answer (older
    prompt version, or a model that ignores the two-line format) still
    works instead of silently losing the suggestion."""
    add_part = ""
    delete_part = ""
    found_label = False
    for line in text.splitlines():
        match = _ADD_LINE_RE.match(line)
        if match:
            add_part = match.group(1).strip()
            found_label = True
            continue
        match = _DELETE_LINE_RE.match(line)
        if match:
            delete_part = match.group(1).strip()
            found_label = True
            continue
    if not found_label:
        add_part = text.strip()
    return add_part, delete_part


def _split_candidate_names(part: str) -> list[str]:
    if not part or part.strip().casefold() == NONE_SENTINEL.casefold():
        return []
    return [piece.strip() for piece in part.split(",") if piece.strip()]


def process_pipeline(
    document_id: int,
    dry_run: bool = False,
    paperless_config_path: str = "config/paperless.yaml",
    ai_config_path: str = "config/ai.yaml",
    steps_config_path: str = "config/steps.yaml",
    db_path: str = "data/archivlotse.db",
) -> dict[str, str | None]:
    """Run every step listed under `pipeline:` in config/steps.yaml, in
    order, for one document.

    Each step re-fetches the document fresh from Paperless, so a later
    step sees an earlier step's write (e.g. "title" sees the document
    type "document_type" just set) - except in --dry-run, where nothing
    is actually written, so later steps still see the original values.

    A step that raises is logged and skipped rather than aborting the
    rest of the pipeline, so e.g. an unreachable Docling instance for
    ocr_check doesn't block document_type/title from still running.
    """
    order = load_pipeline_order(steps_config_path)
    results: dict[str, str | None] = {}
    for step_name in order:
        logger.info("Dokument %s: starte Pipeline-Schritt %r", document_id, step_name)
        try:
            results[step_name] = process_step(
                step_name=step_name,
                document_id=document_id,
                dry_run=dry_run,
                paperless_config_path=paperless_config_path,
                ai_config_path=ai_config_path,
                steps_config_path=steps_config_path,
                db_path=db_path,
            )
        except Exception:
            logger.exception(
                "Dokument %s, Schritt %r: fehlgeschlagen, überspringe und fahre mit dem "
                "nächsten Schritt fort",
                document_id,
                step_name,
            )
            results[step_name] = None
    return results


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
    config/steps.yaml (see StepConfig/load_step_config). Four shapes are
    supported:

    - Plain field-to-field (default): render the prompt from
      source_field's text and write the AI's (cleaned-up) answer verbatim
      to target_field.
    - Choice from candidates (step has `candidates_endpoint` set): fetch
      an existing Paperless list (e.g. document types) via the Paperless
      API, give the prompt the candidate names, and only ever write back
      a candidate the AI's answer actually matched - a hallucinated or
      unparseable answer is rejected, never written to Paperless.
    - Multi-choice from candidates (`candidates_endpoint` + `multi: true`,
      e.g. tags): the AI answers with a "Hinzufügen:"/"Löschen:" pair of
      comma-separated candidate lists (see `_parse_add_delete_lines`);
      each entry in both lists is validated independently (invalid ones
      are dropped, not fatal to the step), and the final value is
      `(current | add) - delete` - existing values are kept unless the AI
      explicitly proposes removing them.
    - OCR quality check (step has `quality_check` set): no AI role/prompt
      at all - estimate the existing text's OCR quality, and only if it's
      below quality_threshold, re-OCR the original file via Docling Serve
      and (if the result passes a basic sanity check) write it back.

    All shapes except the OCR check also support a `condition`: the step
    is skipped entirely (no AI call) unless the document's current
    field(s) match the configured expectation.

    Already-computed results are cached in SQLite (keyed by a fingerprint
    of the input, prompt version, AI provider/model, postprocessing
    rules, and - for choice steps - the candidate list), so unchanged
    documents never trigger a repeat AI/Docling call. The audit log only
    ever gets a row when a field is actually written to Paperless (never
    on cache hits, no-ops, rejected suggestions, skipped conditions, or
    --dry-run).
    """
    step_config = load_step_config(step_name, steps_config_path)
    paperless_config = load_paperless_config(paperless_config_path)
    conn = init_db(db_path)

    with PaperlessClient(paperless_config) as paperless:
        if step_config.quality_check:
            return _process_ocr_step(step_config, paperless, conn, document_id, dry_run)

        ai_config = load_ai_config(ai_role or step_config.ai_role, ai_config_path)
        if step_config.candidates_endpoint and step_config.multi:
            return _process_multi_choice_step(
                step_config, ai_config, paperless, conn, document_id, dry_run
            )
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

    document_type_name = _resolve_document_type_name(document, paperless, conn)
    if not _condition_met(step_config, document, document_type_name):
        logger.info(
            "Dokument %s, Schritt %r: Bedingung %r nicht erfüllt, Schritt übersprungen",
            document_id,
            step_config.name,
            step_config.condition,
        )
        return None

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
        document_type_name or "",
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
        prompt = render_prompt(
            step_config.prompt, input_text=truncated_text, document_type_name=document_type_name
        )
        ai_client = build_ai_client(ai_config)
        logger.info(
            "Dokument %s, Schritt %r: frage Ergebnis bei %s/%s an",
            document_id,
            step_config.name,
            ai_config.provider,
            ai_config.model,
        )
        logger.debug(
            "Dokument %s, Schritt %r: vollständiger Prompt:\n%s", document_id, step_config.name, prompt
        )
        ai_result = ai_client.generate(prompt)
        logger.debug(
            "Dokument %s, Schritt %r: rohe KI-Antwort: %r", document_id, step_config.name, ai_result.text
        )
        cost = _log_usage(document_id, step_config.name, ai_config, ai_result)
        value = _postprocess(ai_result.text, step_config)
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
            prompt_tokens=ai_result.prompt_tokens,
            completion_tokens=ai_result.completion_tokens,
            estimated_cost_usd=cost,
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

    candidates = _get_candidates(conn, paperless, step_config.candidates_endpoint)
    name_by_id = {candidate["id"]: candidate["name"] for candidate in candidates}
    id_by_normalized_name = {
        candidate["name"].strip().casefold(): candidate["id"] for candidate in candidates
    }

    # Reuse the candidates list to resolve document_type_name when this IS
    # the document_type step itself, instead of an extra lookup.
    if step_config.candidates_endpoint == "document_types":
        document_type_name = name_by_id.get(document.get("document_type"))
    else:
        document_type_name = _resolve_document_type_name(document, paperless, conn)

    if not _condition_met(step_config, document, document_type_name):
        logger.info(
            "Dokument %s, Schritt %r: Bedingung %r nicht erfüllt, Schritt übersprungen",
            document_id,
            step_config.name,
            step_config.condition,
        )
        return None

    input_text = str(document.get(step_config.source_field) or "").strip()
    old_id = document.get(step_config.target_field)
    old_value = name_by_id.get(old_id, old_id)

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
            document_type_name=document_type_name,
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
        logger.debug(
            "Dokument %s, Schritt %r: vollständiger Prompt:\n%s", document_id, step_config.name, prompt
        )
        ai_result = ai_client.generate(prompt)
        logger.debug(
            "Dokument %s, Schritt %r: rohe KI-Antwort: %r", document_id, step_config.name, ai_result.text
        )
        cost = _log_usage(document_id, step_config.name, ai_config, ai_result)
        suggestion = _postprocess(ai_result.text, step_config)
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
            prompt_tokens=ai_result.prompt_tokens,
            completion_tokens=ai_result.completion_tokens,
            estimated_cost_usd=cost,
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
    write_value: int | str | list[int] | None = None,
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


def _process_ocr_step(
    step_config: StepConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    dry_run: bool,
) -> str | None:
    """OCR quality check/improve step. No AI role or prompt is used.

    The existing text's quality is estimated first (cheap, no network
    call beyond fetching the document); only if it's below
    quality_threshold is the original file downloaded and re-OCR'd via
    Docling Serve. The new text is written back only if it passes a
    basic sanity floor (see MIN_OCR_LENGTH_RATIO) - see ocr_quality.py
    for why old/new quality scores are deliberately NOT compared
    directly to decide whether to accept the new text.
    """
    document = paperless.get_document(document_id)
    old_text = str(document.get(step_config.source_field) or "")

    if len(old_text.strip()) < step_config.min_chars_for_check:
        logger.info(
            "Dokument %s, Schritt %r: Text zu kurz (%d Zeichen) für Qualitätsprüfung, übersprungen",
            document_id,
            step_config.name,
            len(old_text.strip()),
        )
        return None

    if step_config.quality_check == "ai_judge":
        raise NotImplementedError(
            f"Schritt {step_config.name!r}: quality_check 'ai_judge' ist noch nicht "
            "implementiert - bitte 'heuristic' verwenden."
        )
    if step_config.quality_check != "heuristic":
        raise ValueError(
            f"Schritt {step_config.name!r}: unbekannter quality_check "
            f"{step_config.quality_check!r} (erwartet 'heuristic' oder 'ai_judge')"
        )

    old_score = ocr_quality_score(old_text)
    logger.info(
        "Dokument %s, Schritt %r: OCR-Qualitätsscore des vorhandenen Texts=%.3f (Schwelle %.3f)",
        document_id,
        step_config.name,
        old_score,
        step_config.quality_threshold,
    )

    if old_score >= step_config.quality_threshold:
        logger.info(
            "Dokument %s, Schritt %r: OCR-Qualität ausreichend, kein Re-OCR nötig",
            document_id,
            step_config.name,
        )
        return None

    logger.warning(
        "Dokument %s, Schritt %r: OCR-Qualität unter Schwelle (%.3f < %.3f), versuche Re-OCR via Docling",
        document_id,
        step_config.name,
        old_score,
        step_config.quality_threshold,
    )

    fingerprint = _fingerprint(
        old_text, step_config.docling_base_url or "", str(step_config.docling_force_ocr)
    )
    cached = get_cached_step_result(conn, step_config.name, document_id, fingerprint)
    if cached is not None:
        new_text = cached["output_value"]
        logger.info(
            "Dokument %s, Schritt %r: nutze zwischengespeichertes Docling-Ergebnis",
            document_id,
            step_config.name,
        )
    else:
        original_bytes = paperless.download_original(document_id)
        with DoclingClient(base_url=step_config.docling_base_url) as docling:
            new_text = docling.convert_to_text(
                f"{document_id}.pdf", original_bytes, force_ocr=step_config.docling_force_ocr
            )
        save_step_result(
            conn,
            step_name=step_config.name,
            document_id=document_id,
            fingerprint=fingerprint,
            output_value=new_text,
            provider="docling",
            model=step_config.docling_base_url or "",
            prompt_name="-",
            prompt_version="-",
        )
        logger.info(
            "Dokument %s, Schritt %r: Docling-Konvertierung abgeschlossen (%d Zeichen)",
            document_id,
            step_config.name,
            len(new_text),
        )

    new_score = ocr_quality_score(new_text)
    logger.info(
        "Dokument %s, Schritt %r: OCR-Qualitätsscore nach Docling=%.3f (vorher %.3f)",
        document_id,
        step_config.name,
        new_score,
        old_score,
    )

    min_acceptable_length = max(step_config.min_chars_for_check, len(old_text) * MIN_OCR_LENGTH_RATIO)
    if len(new_text.strip()) < min_acceptable_length:
        logger.warning(
            "Dokument %s, Schritt %r: Docling-Ergebnis deutlich kürzer/leerer als vorhandener Text "
            "(%d < %d Zeichen), wird NICHT übernommen (Sicherheitscheck)",
            document_id,
            step_config.name,
            len(new_text.strip()),
            int(min_acceptable_length),
        )
        return None

    old_summary = f"[{len(old_text)} Zeichen, Score {old_score:.2f}]"
    new_summary = f"[{len(new_text)} Zeichen, Score {new_score:.2f}]"

    return _apply_value(
        step_config,
        paperless,
        conn,
        document_id,
        old_summary,
        new_summary,
        dry_run,
        write_value=new_text,
    )


def _process_multi_choice_step(
    step_config: StepConfig,
    ai_config: AIConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    dry_run: bool,
) -> str | None:
    """Like _process_choice_step, but for fields that hold a LIST of
    candidate ids (e.g. Paperless tags, a many-to-many relation) rather
    than a single one. The AI answers in two labeled lines - "Hinzufügen:"
    (add) and "Löschen:" (remove) - each a comma-separated list of
    existing candidates or KEINE; each entry in both lists is validated
    independently (an unmatched one is dropped with a warning, not fatal).
    The final value is (current | add) - delete: suggestions to add are
    additive, but the AI can also propose removing a currently-set value
    that no longer fits. If a candidate appears in both lists, delete
    wins (applied last) and a warning is logged. step_config.protected_candidates
    names candidates that are never deleted, no matter what the AI
    suggests - an enforced guarantee, independent of the prompt text."""
    document = paperless.get_document(document_id)

    candidates = _get_candidates(conn, paperless, step_config.candidates_endpoint)
    name_by_id = {candidate["id"]: candidate["name"] for candidate in candidates}
    id_by_normalized_name = {
        candidate["name"].strip().casefold(): candidate["id"] for candidate in candidates
    }

    document_type_name = _resolve_document_type_name(document, paperless, conn)
    if not _condition_met(step_config, document, document_type_name):
        logger.info(
            "Dokument %s, Schritt %r: Bedingung %r nicht erfüllt, Schritt übersprungen",
            document_id,
            step_config.name,
            step_config.condition,
        )
        return None

    input_text = str(document.get(step_config.source_field) or "").strip()
    current_ids = sorted(document.get(step_config.target_field) or [])
    current_names_list = [name_by_id.get(i, str(i)) for i in current_ids]
    current_names = ", ".join(current_names_list)

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
    candidates_signature = "|".join(sorted(candidate["name"] for candidate in candidates))
    # Current tags are part of the prompt now (so the AI can propose
    # removing one), so they must be part of the fingerprint too.
    current_tags_signature = "|".join(current_names_list)
    fingerprint = _fingerprint(
        truncated_text,
        prompt_version,
        ai_config.provider,
        ai_config.model,
        postprocessing_signature,
        candidates_signature,
        current_tags_signature,
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
            current_tag_names=current_names_list,
            document_type_name=document_type_name,
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
        logger.debug(
            "Dokument %s, Schritt %r: vollständiger Prompt:\n%s", document_id, step_config.name, prompt
        )
        ai_result = ai_client.generate(prompt)
        logger.debug(
            "Dokument %s, Schritt %r: rohe KI-Antwort: %r", document_id, step_config.name, ai_result.text
        )
        cost = _log_usage(document_id, step_config.name, ai_config, ai_result)
        suggestion = _clean_multiline(ai_result.text, step_config)
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
            prompt_tokens=ai_result.prompt_tokens,
            completion_tokens=ai_result.completion_tokens,
            estimated_cost_usd=cost,
        )
        logger.info(
            "Dokument %s, Schritt %r: neuer Vorschlag: %r",
            document_id,
            step_config.name,
            suggestion,
        )

    add_part, delete_part = _parse_add_delete_lines(suggestion)

    def _match(parts: list[str], action: str) -> set[int]:
        matched: set[int] = set()
        for piece in parts:
            if step_config.strip_quotes:
                piece = piece.strip("\"'„“”«»")
            matched_id = id_by_normalized_name.get(piece.casefold())
            if matched_id is None:
                logger.warning(
                    "Dokument %s, Schritt %r: %s-Vorschlag %r entspricht keinem vorhandenen "
                    "Kandidaten, wird ignoriert",
                    document_id,
                    step_config.name,
                    action,
                    piece,
                )
                continue
            matched.add(matched_id)
        return matched

    add_ids = _match(_split_candidate_names(add_part), "Hinzufügen")
    delete_ids = _match(_split_candidate_names(delete_part), "Löschen")

    if step_config.protected_candidates:
        protected_normalized = {name.strip().casefold() for name in step_config.protected_candidates}
        blocked_delete_ids = {
            tag_id
            for tag_id in delete_ids
            if name_by_id.get(tag_id, "").strip().casefold() in protected_normalized
        }
        if blocked_delete_ids:
            logger.info(
                "Dokument %s, Schritt %r: Löschen von %s wird ignoriert (in config/steps.yaml "
                "als protected_candidates geschützt)",
                document_id,
                step_config.name,
                ", ".join(sorted(name_by_id[i] for i in blocked_delete_ids)),
            )
            delete_ids -= blocked_delete_ids

    overlap = add_ids & delete_ids
    if overlap:
        logger.warning(
            "Dokument %s, Schritt %r: Tags sowohl zum Hinzufügen als auch Löschen vorgeschlagen "
            "(%s) - werden entfernt",
            document_id,
            step_config.name,
            ", ".join(sorted(name_by_id[i] for i in overlap)),
        )

    new_ids = sorted((set(current_ids) | add_ids) - delete_ids)
    new_names = ", ".join(name_by_id.get(i, str(i)) for i in new_ids)

    return _apply_value(
        step_config,
        paperless,
        conn,
        document_id,
        current_names or "(keine)",
        new_names or "(keine)",
        dry_run,
        write_value=new_ids,
    )
