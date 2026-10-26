from __future__ import annotations

import datetime
import hashlib
import html
import logging
import re
import sqlite3
from typing import Callable

import httpx
from rapidfuzz import fuzz, process

from .ai.factory import build_ai_client, build_ocr_client
from .config import AIConfig, load_ai_config, load_paperless_config
from .db import (
    append_audit_log,
    clear_caches,
    field_written_since,
    get_cached_candidates,
    get_cached_step_result,
    init_db,
    save_candidates,
    save_step_result,
)
from .errors import ServiceError, log_failure
from .paperless import PaperlessClient
from .pdf_text import analyze_pdf
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

# Answer for a correspondent step (step1 or step2, see
# config/prompts/correspondent-step*.j2) when no correspondent can be
# confidently determined from the text at all - distinct from "a name was
# extracted but doesn't match an existing Paperless correspondent", which
# is a candidate for a NEW correspondent (never auto-created, see
# CLAUDE.md non-goals - just logged for manual review).
NO_CORRESPONDENT_SENTINEL = "KEINEN-GEFUNDEN"

# Answer for a document-date step (see config/prompts/create-date-*.j2)
# when no reliable creation date is recognizable - nothing is written.
NO_DATE_SENTINEL = "KEIN-DATUM-GEFUNDEN"

# Date answers: "DD.MM.YYYY" (create-date-small.j2) or "DD.MM.YYYY|SCORE"
# (create-date-part.j2). Matched against the WHOLE cleaned answer, so a
# date buried in an explanation is rejected rather than guessed at.
_DATE_ANSWER_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})(?:\s*\|\s*(\d{1,3}))?")

# Answer for a custom-field step (see e.g. config/prompts/geldbetrag.j2)
# when the value can't be reliably determined from the text - nothing is
# written.
NO_VALUE_SENTINEL = "KEIN-WERT"

# Numeric custom-field answers (data types integer/float/monetary): a plain
# number, "." or "," as decimal separator, no thousands separators - for
# monetary optionally with a 3-letter currency code before or after it.
# Matched against the WHOLE cleaned answer, like _DATE_ANSWER_RE.
_NUMBER_ANSWER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_MONETARY_ANSWER_RE = re.compile(
    r"(?:(?P<prefix>[A-Z]{3})\s*)?(?P<amount>-?\d+(?:[.,]\d{1,2})?)(?:\s*(?P<suffix>[A-Z]{3}))?"
)

# Earliest year accepted as a plausible document creation date - an older
# one is almost certainly an OCR/AI error (or a birth date etc.).
MIN_DOCUMENT_YEAR = 1900

# Multi-choice prompts (e.g. config/prompts/tags.j2) answer in two labeled
# lines, one for candidates to add and one for candidates to remove, so
# the AI can also flag existing values that no longer fit - see
# _parse_add_delete_lines().
_ADD_LINE_RE = re.compile(r"^\s*Hinzuf(?:ü|ue)gen\s*:\s*(.*)$", re.IGNORECASE)
_DELETE_LINE_RE = re.compile(r"^\s*L(?:ö|oe)schen\s*:\s*(.*)$", re.IGNORECASE)

CANDIDATE_FETCHERS: dict[str, Callable[[PaperlessClient], list[dict]]] = {
    "document_types": lambda paperless: paperless.list_document_types(),
    "tags": lambda paperless: paperless.list_tags(),
    "correspondents": lambda paperless: paperless.list_correspondents(),
    "custom_fields": lambda paperless: paperless.list_custom_fields(),
}

# How long a fetched candidate list (document types, tags, ...) is reused
# across separate `archivlotse process` invocations before being
# re-fetched from Paperless - these lists change rarely, and nearly every
# step needs one (directly, or indirectly via document_type_name), so
# caching avoids a Paperless round-trip per step.
CANDIDATE_CACHE_TTL_SECONDS = 2 * 60 * 60  # 2 Stunden

# An OCR result must be at least this fraction of the existing text's
# length to replace it. Not a quality check deciding whether to OCR - a
# safety net after OCR: LightOnOCR sometimes treats a photographed page
# (e.g. a receipt) as a picture and transcribes only a fragment of it,
# which must not replace usable text.
MIN_OCR_LENGTH_RATIO = 0.3

# Provider types (config/ai.yaml: providers.<name>.type) that run locally -
# their calls always count as $0 in the cost estimate.
LOCAL_PROVIDERS = {"ollama", "docling"}

# Markup in LightOnOCR's (markdown) output that doesn't belong into
# Paperless' plain-text content field: image placeholders, and tables as
# HTML (turned into one line per row, cells separated by " | ").
_OCR_IMAGE_PLACEHOLDER = re.compile(r"^[ \t]*!\[[^\]]*\]\([^)]*\)[ \t]*$", re.MULTILINE)
_HTML_TABLE = re.compile(r"<table\b.*?</table>", re.DOTALL | re.IGNORECASE)
_HTML_ROW = re.compile(r"<tr\b.*?</tr>", re.DOTALL | re.IGNORECASE)
_HTML_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")


def _fingerprint(*parts: str) -> str:
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_candidates(
    conn: sqlite3.Connection, paperless: PaperlessClient, endpoint: str, force: bool = False
) -> list[dict]:
    """Fetch a candidate list (e.g. Paperless tags/document types) for
    `endpoint`, reusing a cached copy (see CANDIDATE_CACHE_TTL_SECONDS) if
    one is still fresh enough instead of hitting Paperless again - unless
    force=True, which always fetches fresh (and refreshes the cache)."""
    cached = None if force else get_cached_candidates(conn, endpoint, CANDIDATE_CACHE_TTL_SECONDS)
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
    document: dict, paperless: PaperlessClient, conn: sqlite3.Connection, force: bool = False
) -> str | None:
    """Resolve the document's current document_type ID to its name, so
    it can be used in step conditions and exposed to prompt templates as
    `document_type_name`. Returns None if no document_type is set."""
    type_id = document.get("document_type")
    if type_id is None:
        return None
    for candidate in _get_candidates(conn, paperless, "document_types", force):
        if candidate["id"] == type_id:
            return candidate["name"]
    return None


def _resolve_correspondent_name(
    document: dict, paperless: PaperlessClient, conn: sqlite3.Connection, force: bool = False
) -> str | None:
    """Resolve the document's current correspondent ID to its name, so it
    can be exposed to prompt templates as `correspondent_name` (e.g. so
    title.j2 can use it as context - NOT to print it into the title
    itself, since it's already stored in its own field). Returns None if
    no correspondent is set. Note: fetches the full correspondents list
    (~1000 on this instance) via the same 2h candidate_cache as the
    correspondent step - the first call after the cache expires can take
    a while, same as that step's own candidate fetch."""
    correspondent_id = document.get("correspondent")
    if correspondent_id is None:
        return None
    for candidate in _get_candidates(conn, paperless, "correspondents", force):
        if candidate["id"] == correspondent_id:
            return candidate["name"]
    return None


def _condition_met(step_config: StepConfig, document: dict, document_type_name: str | None) -> bool:
    if not step_config.condition:
        return True
    for field, expected in step_config.condition.items():
        actual = document_type_name if field == "document_type" else document.get(field)
        allowed = expected if isinstance(expected, list) else [expected]
        if actual not in allowed:
            return False
    return True


def _estimate_cost(
    ai_config: AIConfig, prompt_tokens: int | None, completion_tokens: int | None
) -> float | None:
    """Estimate the USD cost of one AI call from its token usage. Ollama
    and Docling run locally, so they're always treated as free ($0),
    independent of whether pricing is configured. For other providers, returns None if
    pricing (config/ai.yaml: price_per_1m_input/output) or usage data
    isn't available, rather than silently guessing $0."""
    if ai_config.provider in LOCAL_PROVIDERS:
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
    if ai_config.provider in LOCAL_PROVIDERS and ai_result.prompt_tokens is None:
        return cost  # e.g. Docling: no tokens to report, nothing to pay
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


def _normalize_correspondent_name(name: str) -> str:
    """Minimal normalization before fuzzy matching: collapse whitespace
    and casefold only. Deliberately does NOT strip legal forms (GmbH,
    AG, ...), location suffixes, or terms like "Lebensversicherung" vs.
    "Versicherung" - these can distinguish genuinely different legal entities and must survive
    into the fuzzy score, not be normalized away."""
    return " ".join(name.strip().split()).casefold()


def _fuzzy_match_correspondents(
    extracted_name: str, candidates: list[dict], limit: int
) -> list[tuple[int, float]]:
    """Score `extracted_name` against every candidate's name with
    RapidFuzz, returning the top `limit` (id, score) pairs sorted by
    descending score. Empty if `candidates` is empty."""
    if not candidates:
        return []
    choices = {candidate["id"]: _normalize_correspondent_name(candidate["name"]) for candidate in candidates}
    results = process.extract(
        _normalize_correspondent_name(extracted_name), choices, scorer=fuzz.WRatio, limit=limit
    )
    return [(candidate_id, score) for _value, score, candidate_id in results]


def _split_candidate_names(part: str) -> list[str]:
    if not part or part.strip().casefold() == NONE_SENTINEL.casefold():
        return []
    return [piece.strip() for piece in part.split(",") if piece.strip()]


def clear_cache(db_path: str = "data/archivlotse.db") -> None:
    """Delete all cached step results and candidate lists (step_results,
    candidate_cache) - NOT the audit log, which stays as permanent
    history. Useful after reorganizing Paperless metadata (e.g. renamed/
    restructured tags) so stale cached suggestions don't linger."""
    conn = init_db(db_path)
    step_results_count, candidate_cache_count = clear_caches(conn)
    logger.info(
        "Cache geleert: %d Ergebnis(se) aus step_results, %d Kandidatenliste(n) aus "
        "candidate_cache entfernt",
        step_results_count,
        candidate_cache_count,
    )


def process_pipeline(
    document_id: int,
    dry_run: bool = False,
    force: bool = False,
    step_names: list[str] | None = None,
    paperless_config_path: str = "config/paperless.yaml",
    ai_config_path: str = "config/ai.yaml",
    steps_config_path: str = "config/steps.yaml",
    db_path: str = "data/archivlotse.db",
) -> dict[str, str | None]:
    """Run every step listed under `pipeline:` in config/steps.yaml, in
    order, for one document - or, if step_names is given (e.g. from a
    batch job in config/batch.yaml), just those steps, in that order.

    Each step re-fetches the document fresh from Paperless, so a later
    step sees an earlier step's write (e.g. "title" sees the document
    type "document_type" just set) - except in --dry-run, where nothing
    is actually written, so later steps still see the original values.

    A step that raises is logged and skipped rather than aborting the
    rest of the pipeline, so e.g. a failing OCR call in
    ocr doesn't block document_type/title from still running - with
    one exception: a 404 fetching the document itself means the document
    doesn't exist at all, which will fail identically for every remaining
    step. Rather than repeating the same failure (and a full traceback)
    once per step, the pipeline aborts immediately with a single clear
    message.

    force=True ignores any cached step_results/candidate_cache entry, so
    every AI/OCR call and every candidate-list fetch happens again,
    even for an unchanged document.
    """
    order = step_names if step_names is not None else load_pipeline_order(steps_config_path)
    results: dict[str, str | None] = {}
    failed_steps: list[str] = []
    for step_name in order:
        try:
            if failed_steps and load_step_config(step_name, steps_config_path).requires_previous_success:
                logger.warning(
                    "Dokument %s, Schritt %r: übersprungen, da frühere Schritte fehlgeschlagen "
                    "sind (%s)",
                    document_id,
                    step_name,
                    ", ".join(failed_steps),
                )
                results[step_name] = None
                continue
            logger.info("Dokument %s: starte Pipeline-Schritt %r", document_id, step_name)
            results[step_name] = process_step(
                step_name=step_name,
                document_id=document_id,
                dry_run=dry_run,
                force=force,
                paperless_config_path=paperless_config_path,
                ai_config_path=ai_config_path,
                steps_config_path=steps_config_path,
                db_path=db_path,
            )
        except Exception as exc:
            if _is_missing_document_error(exc, document_id):
                logger.error(
                    "Dokument %s existiert nicht in Paperless (404) - Pipeline abgebrochen "
                    "(restliche Schritte würden identisch fehlschlagen)",
                    document_id,
                )
                break
            log_failure(
                logger,
                f"Dokument {document_id}, Schritt {step_name!r} fehlgeschlagen (weiter mit "
                "nächstem Schritt)",
                exc,
            )
            results[step_name] = None
            failed_steps.append(step_name)
    return results


def _is_missing_document_error(exc: Exception, document_id: int) -> bool:
    """True only for a 404 on the Paperless document itself - not for a
    404 from another service (e.g. Ollama's "model not found")."""
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == 404
        and exc.request.url.path.rstrip("/").endswith(f"/api/documents/{document_id}")
    )


def process_step(
    step_name: str,
    document_id: int,
    dry_run: bool = False,
    force: bool = False,
    model: str | None = None,
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
    - OCR step (step has `ocr: {model: ...}` set): see _process_ocr_step -
      a scanned PDF/image is transcribed by the OCR model and the result
      replaces the text; a PDF with real text is left alone.
    - Document-date step (step has `date: {prompt_small: ...}` set): see
      _process_date_step - one full-text AI call for short documents,
      otherwise first/last part with a confidence score per answer.
    - Custom-field step (step has `custom_field` set): see
      _process_custom_field_step - like a text step, but the answer is
      validated against the Paperless custom field's data type and written
      into the document's custom_fields list.
    - Correspondent step (step has `prompt_step2` set): two-stage
      recognition - the primary prompt/model extracts a probable name
      from the text, RapidFuzz scores it against candidates_endpoint's
      list deterministically, and only an ambiguous fuzzy result (see
      fuzzy_*_min_* thresholds in StepConfig) triggers a second AI call
      (prompt_step2/model_step2) with the closest few candidates to
      disambiguate.

    All shapes except the OCR step also support a `condition`: the step
    is skipped entirely (no AI call) unless the document's current
    field(s) match the configured expectation.

    Already-computed results are cached in SQLite (keyed by a fingerprint
    of the input, prompt version, AI provider/model, postprocessing
    rules, and - for choice steps - the candidate list), so unchanged
    documents never trigger a repeat AI/OCR call. The audit log only
    ever gets a row when a field is actually written to Paperless (never
    on cache hits, no-ops, rejected suggestions, skipped conditions, or
    --dry-run).

    force=True ignores any cached step_results/candidate_cache entry for
    this step, so the AI/OCR call and candidate-list fetch happen
    again even if the input hasn't changed since the last run.
    """
    step_config = load_step_config(step_name, steps_config_path)
    paperless_config = load_paperless_config(paperless_config_path)
    conn = init_db(db_path)

    with PaperlessClient(paperless_config) as paperless:
        if step_config.ocr_model:
            ai_config = load_ai_config(model or step_config.ocr_model, ai_config_path)
            return _process_ocr_step(
                step_config, ai_config, paperless, conn, document_id, dry_run, force
            )
        if step_config.add_tags:
            return _process_add_tags_step(step_config, paperless, conn, document_id, dry_run, force)

        ai_config = load_ai_config(model or step_config.model, ai_config_path)
        if step_config.custom_field:
            return _process_custom_field_step(
                step_config, ai_config, paperless, conn, document_id, dry_run, force
            )
        if step_config.date_prompt_small:
            return _process_date_step(step_config, ai_config, paperless, conn, document_id, dry_run, force)
        if step_config.prompt_step2:
            ai_config_step2 = load_ai_config(step_config.model_step2, ai_config_path)
            return _process_correspondent_step(
                step_config, ai_config, ai_config_step2, paperless, conn, document_id, dry_run, force
            )
        if step_config.candidates_endpoint and step_config.multi:
            return _process_multi_choice_step(
                step_config, ai_config, paperless, conn, document_id, dry_run, force
            )
        if step_config.candidates_endpoint:
            return _process_choice_step(
                step_config, ai_config, paperless, conn, document_id, dry_run, force
            )
        return _process_text_step(step_config, ai_config, paperless, conn, document_id, dry_run, force)


def _process_text_step(
    step_config: StepConfig,
    ai_config: AIConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    dry_run: bool,
    force: bool = False,
) -> str | None:
    document = paperless.get_document(document_id)

    document_type_name = _resolve_document_type_name(document, paperless, conn, force)
    correspondent_name = _resolve_correspondent_name(document, paperless, conn, force)
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
        correspondent_name or "",
    )

    cached = None if force else get_cached_step_result(conn, step_config.name, document_id, fingerprint)
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
            step_config.prompt,
            input_text=truncated_text,
            document_type_name=document_type_name,
            correspondent_name=correspondent_name,
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
    force: bool = False,
) -> str | None:
    document = paperless.get_document(document_id)

    candidates = _get_candidates(conn, paperless, step_config.candidates_endpoint, force)
    name_by_id = {candidate["id"]: candidate["name"] for candidate in candidates}
    id_by_normalized_name = {
        candidate["name"].strip().casefold(): candidate["id"] for candidate in candidates
    }

    # Reuse the candidates list to resolve document_type_name when this IS
    # the document_type step itself, instead of an extra lookup.
    if step_config.candidates_endpoint == "document_types":
        document_type_name = name_by_id.get(document.get("document_type"))
    else:
        document_type_name = _resolve_document_type_name(document, paperless, conn, force)

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

    cached = None if force else get_cached_step_result(conn, step_config.name, document_id, fingerprint)
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


def _process_correspondent_step(
    step_config: StepConfig,
    ai_config: AIConfig,
    ai_config_step2: AIConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    dry_run: bool,
    force: bool = False,
) -> str | None:
    """Two-stage correspondent recognition (thresholds and their
    rationale: the `correspondent` step in config/steps.yaml):

    1. step1 (step_config.prompt/model) extracts a probable
       correspondent name from the text, or NO_CORRESPONDENT_SENTINEL.
    2. RapidFuzz scores that name against every existing Paperless
       correspondent (candidates_endpoint), deterministically, no AI
       involved.
    3. A confident, clearly-leading fuzzy match is assigned directly.
       An ambiguous result (several similarly-plausible candidates)
       triggers step2 (prompt_step2/model_step2) with just the closest
       few candidates to disambiguate using the full text. A weak match
       is left alone.

    Every non-assignment outcome (step1 found nothing, no plausible
    fuzzy match, step2 couldn't confirm one of its candidates, step2
    proposed a name matching none of them) is logged and returns None -
    ArchivLotse never creates a new correspondent automatically (see
    CLAUDE.md non-goals); that needs a human decision this step doesn't
    make for you.
    """
    document = paperless.get_document(document_id)

    document_type_name = _resolve_document_type_name(document, paperless, conn, force)
    if not _condition_met(step_config, document, document_type_name):
        logger.info(
            "Dokument %s, Schritt %r: Bedingung %r nicht erfüllt, Schritt übersprungen",
            document_id,
            step_config.name,
            step_config.condition,
        )
        return None

    input_text = str(document.get(step_config.source_field) or "").strip()
    candidates = _get_candidates(conn, paperless, step_config.candidates_endpoint, force)
    name_by_id = {candidate["id"]: candidate["name"] for candidate in candidates}
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
    postprocessing_signature = (
        f"strip_quotes={step_config.strip_quotes};"
        f"max_output_length={step_config.max_output_length}"
    )

    # --- step1: extract a probable correspondent name (free text) ---
    prompt_source = load_prompt_source(step_config.prompt)
    prompt_version = hashlib.sha256(prompt_source.encode("utf-8")).hexdigest()
    fingerprint1 = _fingerprint(
        truncated_text,
        prompt_version,
        ai_config.provider,
        ai_config.model,
        postprocessing_signature,
    )
    cache_key1 = f"{step_config.name}:step1"
    cached1 = None if force else get_cached_step_result(conn, cache_key1, document_id, fingerprint1)
    if cached1 is not None:
        extracted_name = cached1["output_value"]
        logger.info(
            "Dokument %s, Schritt %r (step1): unveränderte Eingabe/Prompt/Modell, nutze "
            "zwischengespeicherten Namen %r",
            document_id,
            step_config.name,
            extracted_name,
        )
    else:
        prompt = render_prompt(
            step_config.prompt, input_text=truncated_text, document_type_name=document_type_name
        )
        ai_client = build_ai_client(ai_config)
        logger.info(
            "Dokument %s, Schritt %r (step1): frage wahrscheinlichen Korrespondenten bei %s/%s an",
            document_id,
            step_config.name,
            ai_config.provider,
            ai_config.model,
        )
        logger.debug(
            "Dokument %s, Schritt %r (step1): vollständiger Prompt:\n%s",
            document_id,
            step_config.name,
            prompt,
        )
        ai_result = ai_client.generate(prompt)
        logger.debug(
            "Dokument %s, Schritt %r (step1): rohe KI-Antwort: %r",
            document_id,
            step_config.name,
            ai_result.text,
        )
        cost = _log_usage(document_id, f"{step_config.name}:step1", ai_config, ai_result)
        extracted_name = _postprocess(ai_result.text, step_config)
        save_step_result(
            conn,
            step_name=cache_key1,
            document_id=document_id,
            fingerprint=fingerprint1,
            output_value=extracted_name,
            provider=ai_config.provider,
            model=ai_config.model,
            prompt_name=step_config.prompt,
            prompt_version=prompt_version,
            prompt_tokens=ai_result.prompt_tokens,
            completion_tokens=ai_result.completion_tokens,
            estimated_cost_usd=cost,
        )
        logger.info(
            "Dokument %s, Schritt %r (step1): extrahierter Name: %r",
            document_id,
            step_config.name,
            extracted_name,
        )

    if extracted_name.strip().casefold() == NO_CORRESPONDENT_SENTINEL.casefold():
        logger.info(
            "Dokument %s, Schritt %r: step1 konnte keinen Korrespondenten ermitteln (%s), "
            "keine automatische Zuordnung",
            document_id,
            step_config.name,
            NO_CORRESPONDENT_SENTINEL,
        )
        return None

    # --- deterministic fuzzy matching against existing correspondents ---
    if not candidates:
        logger.warning(
            "Dokument %s, Schritt %r: keine vorhandenen Korrespondenten zum Abgleich - %r "
            "bleibt unzugeordnet (manuelle Prüfung)",
            document_id,
            step_config.name,
            extracted_name,
        )
        return None

    matches = _fuzzy_match_correspondents(extracted_name, candidates, step_config.fuzzy_candidate_limit)
    top_id, top_score = matches[0]
    second_score = matches[1][1] if len(matches) > 1 else None
    gap = top_score - second_score if second_score is not None else None
    logger.info(
        "Dokument %s, Schritt %r: Fuzzy-Match für %r -> bester Treffer %r (Score %.1f), "
        "Abstand zum zweiten %s",
        document_id,
        step_config.name,
        extracted_name,
        name_by_id[top_id],
        top_score,
        f"{gap:.1f}" if gap is not None else "n/a (nur ein Kandidat)",
    )

    matched_id: int | None = None

    if top_score >= step_config.fuzzy_assign_min_score and (
        gap is None or gap >= step_config.fuzzy_assign_min_gap
    ):
        matched_id = top_id
        logger.info(
            "Dokument %s, Schritt %r: eindeutiger Treffer, keine zweite KI-Abfrage nötig",
            document_id,
            step_config.name,
        )
    elif top_score >= step_config.fuzzy_review_min_score:
        plausible = [
            {"id": candidate_id, "name": name_by_id[candidate_id]}
            for candidate_id, score in matches
            if score >= step_config.fuzzy_review_min_score
        ]
        logger.info(
            "Dokument %s, Schritt %r: mehrdeutiger Fuzzy-Treffer (Score %.1f, Abstand %s), "
            "frage step2 mit %d Kandidaten an",
            document_id,
            step_config.name,
            top_score,
            f"{gap:.1f}" if gap is not None else "n/a",
            len(plausible),
        )

        prompt_source2 = load_prompt_source(step_config.prompt_step2)
        prompt_version2 = hashlib.sha256(prompt_source2.encode("utf-8")).hexdigest()
        candidates_signature2 = "|".join(sorted(candidate["name"] for candidate in plausible))
        fingerprint2 = _fingerprint(
            truncated_text,
            prompt_version2,
            ai_config_step2.provider,
            ai_config_step2.model,
            postprocessing_signature,
            candidates_signature2,
        )
        cache_key2 = f"{step_config.name}:step2"
        cached2 = None if force else get_cached_step_result(conn, cache_key2, document_id, fingerprint2)
        if cached2 is not None:
            step2_answer = cached2["output_value"]
            logger.info(
                "Dokument %s, Schritt %r (step2): unveränderte Eingabe/Kandidaten/Prompt/Modell, "
                "nutze zwischengespeicherte Antwort %r",
                document_id,
                step_config.name,
                step2_answer,
            )
        else:
            prompt2 = render_prompt(
                step_config.prompt_step2,
                input_text=truncated_text,
                correspondent_names=[candidate["name"] for candidate in plausible],
                document_type_name=document_type_name,
            )
            ai_client2 = build_ai_client(ai_config_step2)
            logger.info(
                "Dokument %s, Schritt %r (step2): frage Disambiguierung bei %s/%s an",
                document_id,
                step_config.name,
                ai_config_step2.provider,
                ai_config_step2.model,
            )
            logger.debug(
                "Dokument %s, Schritt %r (step2): vollständiger Prompt:\n%s",
                document_id,
                step_config.name,
                prompt2,
            )
            ai_result2 = ai_client2.generate(prompt2)
            logger.debug(
                "Dokument %s, Schritt %r (step2): rohe KI-Antwort: %r",
                document_id,
                step_config.name,
                ai_result2.text,
            )
            cost2 = _log_usage(document_id, f"{step_config.name}:step2", ai_config_step2, ai_result2)
            step2_answer = _postprocess(ai_result2.text, step_config)
            save_step_result(
                conn,
                step_name=cache_key2,
                document_id=document_id,
                fingerprint=fingerprint2,
                output_value=step2_answer,
                provider=ai_config_step2.provider,
                model=ai_config_step2.model,
                prompt_name=step_config.prompt_step2,
                prompt_version=prompt_version2,
                prompt_tokens=ai_result2.prompt_tokens,
                completion_tokens=ai_result2.completion_tokens,
                estimated_cost_usd=cost2,
            )
            logger.info(
                "Dokument %s, Schritt %r (step2): Antwort: %r",
                document_id,
                step_config.name,
                step2_answer,
            )

        if step2_answer.strip().casefold() == NO_CORRESPONDENT_SENTINEL.casefold():
            logger.info(
                "Dokument %s, Schritt %r: step2 konnte keinen der vorgeschlagenen Korrespondenten "
                "bestätigen, keine automatische Zuordnung",
                document_id,
                step_config.name,
            )
            return None

        id_by_normalized_name = {
            candidate["name"].strip().casefold(): candidate["id"] for candidate in plausible
        }
        matched_id = id_by_normalized_name.get(step2_answer.strip().casefold())
        if matched_id is None:
            logger.info(
                "Dokument %s, Schritt %r: step2 schlägt einen neuen Korrespondenten vor (%r), "
                "der zu keinem vorgeschlagenen Kandidaten passt - wird NICHT automatisch "
                "angelegt, manuelle Prüfung nötig",
                document_id,
                step_config.name,
                step2_answer,
            )
            return None
    else:
        logger.info(
            "Dokument %s, Schritt %r: kein ausreichend plausibler Treffer für %r (Score %.1f), "
            "keine automatische Zuordnung",
            document_id,
            step_config.name,
            extracted_name,
            top_score,
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


def _parse_date_answer(answer: str, today: datetime.date) -> tuple[datetime.date, int | None] | None:
    """Parse a document-date answer ("DD.MM.YYYY" or "DD.MM.YYYY|SCORE")
    into (date, score). None for the NO_DATE_SENTINEL, anything not in
    exactly that format, an invalid calendar date, a date before
    MIN_DOCUMENT_YEAR or after `today`, or a score outside 1-100 - the AI
    proposes, this decides whether it's usable at all."""
    value = answer.strip()
    if value.split("|", 1)[0].strip().casefold() == NO_DATE_SENTINEL.casefold():
        return None
    match = _DATE_ANSWER_RE.fullmatch(value)
    if match is None:
        return None
    day, month, year, score = match.groups()
    try:
        date = datetime.date(int(year), int(month), int(day))
    except ValueError:
        return None
    if date.year < MIN_DOCUMENT_YEAR or date > today:
        return None
    if score is None:
        return date, None
    score_value = int(score)
    if not 1 <= score_value <= 100:
        return None
    return date, score_value


def _process_date_step(
    step_config: StepConfig,
    ai_config: AIConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    dry_run: bool,
    force: bool = False,
) -> str | None:
    """Determine the document's creation date (e.g. Paperless `created`):

    1. Text <= date_small_max_chars: analyze it once in full with
       date_prompt_small (answer: a date only, no score) - a valid date is
       applied directly.
    2. Otherwise send the first date_part_chars to `prompt` (answer:
       "DD.MM.YYYY|SCORE"). A valid date with score >= date_min_score is
       applied without looking further.
    3. Otherwise also send the last date_part_chars, and take the valid
       date with the highest score (the first part wins a tie).
    4. If that best score is still below date_min_score, it's only
       written when target_field is empty - an existing date is never
       overwritten by an uncertain guess.

    An answer that isn't a valid, plausible date (see _parse_date_answer)
    counts as "no date found" for that part. Each AI call is cached on
    its own (cache keys "<step>:small"/"<step>:first"/"<step>:last").
    """
    document = paperless.get_document(document_id)

    document_type_name = _resolve_document_type_name(document, paperless, conn, force)
    correspondent_name = _resolve_correspondent_name(document, paperless, conn, force)
    if not _condition_met(step_config, document, document_type_name):
        logger.info(
            "Dokument %s, Schritt %r: Bedingung %r nicht erfüllt, Schritt übersprungen",
            document_id,
            step_config.name,
            step_config.condition,
        )
        return None

    input_text = str(document.get(step_config.source_field) or "").strip()
    # Paperless returns `created` as "YYYY-MM-DD" (newer) or a full ISO
    # datetime (older versions) - only the date part is compared/written.
    old_value = str(document.get(step_config.target_field) or "")[:10] or None

    if not input_text:
        logger.warning(
            "Dokument %s: Feld %r ist leer, Schritt %r übersprungen",
            document_id,
            step_config.source_field,
            step_config.name,
        )
        return None

    postprocessing_signature = (
        f"strip_quotes={step_config.strip_quotes};"
        f"max_output_length={step_config.max_output_length}"
    )
    today = datetime.date.today()

    def _ask(part: str, prompt_name: str, text: str) -> tuple[datetime.date, int | None] | None:
        cache_key = f"{step_config.name}:{part}"
        prompt_source = load_prompt_source(prompt_name)
        prompt_version = hashlib.sha256(prompt_source.encode("utf-8")).hexdigest()
        fingerprint = _fingerprint(
            text,
            prompt_version,
            ai_config.provider,
            ai_config.model,
            postprocessing_signature,
            document_type_name or "",
            correspondent_name or "",
        )
        cached = None if force else get_cached_step_result(conn, cache_key, document_id, fingerprint)
        if cached is not None:
            answer = cached["output_value"]
            logger.info(
                "Dokument %s, Schritt %r (%s): unveränderte Eingabe/Prompt/Modell, nutze "
                "zwischengespeicherte Antwort %r",
                document_id,
                step_config.name,
                part,
                answer,
            )
        else:
            prompt = render_prompt(
                prompt_name,
                input_text=text,
                document_type_name=document_type_name,
                correspondent_name=correspondent_name,
            )
            ai_client = build_ai_client(ai_config)
            logger.info(
                "Dokument %s, Schritt %r (%s): frage Erstellungsdatum bei %s/%s an (%d Zeichen)",
                document_id,
                step_config.name,
                part,
                ai_config.provider,
                ai_config.model,
                len(text),
            )
            logger.debug(
                "Dokument %s, Schritt %r (%s): vollständiger Prompt:\n%s",
                document_id,
                step_config.name,
                part,
                prompt,
            )
            ai_result = ai_client.generate(prompt)
            logger.debug(
                "Dokument %s, Schritt %r (%s): rohe KI-Antwort: %r",
                document_id,
                step_config.name,
                part,
                ai_result.text,
            )
            cost = _log_usage(document_id, cache_key, ai_config, ai_result)
            answer = _postprocess(ai_result.text, step_config)
            save_step_result(
                conn,
                step_name=cache_key,
                document_id=document_id,
                fingerprint=fingerprint,
                output_value=answer,
                provider=ai_config.provider,
                model=ai_config.model,
                prompt_name=prompt_name,
                prompt_version=prompt_version,
                prompt_tokens=ai_result.prompt_tokens,
                completion_tokens=ai_result.completion_tokens,
                estimated_cost_usd=cost,
            )
            logger.info(
                "Dokument %s, Schritt %r (%s): Antwort: %r", document_id, step_config.name, part, answer
            )

        parsed = _parse_date_answer(answer, today)
        if parsed is None and answer.split("|", 1)[0].strip().casefold() != NO_DATE_SENTINEL.casefold():
            logger.warning(
                "Dokument %s, Schritt %r (%s): Antwort %r ist kein gültiges/plausibles Datum, "
                "wird verworfen",
                document_id,
                step_config.name,
                part,
                answer,
            )
        return parsed

    if len(input_text) <= step_config.date_small_max_chars:
        result = _ask("small", step_config.date_prompt_small, input_text)
        if result is None:
            logger.info(
                "Dokument %s, Schritt %r: kein Erstellungsdatum ermittelt, Feld %r bleibt unverändert",
                document_id,
                step_config.name,
                step_config.target_field,
            )
            return None
        best_date, best_score = result
        confident = True
    else:
        part_chars = step_config.date_part_chars
        results = []
        first = _ask("first", step_config.prompt, input_text[:part_chars])
        if first is not None:
            results.append(first)
        if first is None or first[1] < step_config.date_min_score:
            last = _ask("last", step_config.prompt, input_text[-part_chars:])
            if last is not None:
                results.append(last)
        if not results:
            logger.info(
                "Dokument %s, Schritt %r: kein Erstellungsdatum ermittelt, Feld %r bleibt unverändert",
                document_id,
                step_config.name,
                step_config.target_field,
            )
            return None
        best_date, best_score = max(results, key=lambda result: result[1])
        confident = best_score >= step_config.date_min_score

    new_value = best_date.isoformat()
    if not confident and old_value:
        logger.info(
            "Dokument %s, Schritt %r: bestes Datum %s nur mit Score %s (< %s) - vorhandenes "
            "Datum %s wird NICHT überschrieben",
            document_id,
            step_config.name,
            new_value,
            best_score,
            step_config.date_min_score,
            old_value,
        )
        return None

    return _apply_value(step_config, paperless, conn, document_id, old_value, new_value, dry_run)


def _parse_custom_field_answer(answer: str, field: dict) -> int | float | str | None:
    """Turn a cleaned-up custom-field answer into the value Paperless
    expects for `field`'s data_type, or None for NO_VALUE_SENTINEL / an
    answer that doesn't fit the type (e.g. text where a number is
    expected) - the AI proposes, this decides whether it's usable at all.
    Raises for a data type this step doesn't support (yet), since that's
    a config error, not a bad answer."""
    value = answer.strip()
    if not value or value.casefold() == NO_VALUE_SENTINEL.casefold():
        return None
    data_type = field.get("data_type")
    if data_type in ("string", "longtext"):
        return value
    if data_type in ("integer", "float"):
        if not _NUMBER_ANSWER_RE.fullmatch(value):
            return None
        number = float(value.replace(",", "."))
        if data_type == "float":
            return number
        return int(number) if number.is_integer() else None
    if data_type == "monetary":
        match = _MONETARY_ANSWER_RE.fullmatch(value)
        if match is None or (match["prefix"] and match["suffix"]):
            return None
        currency = (
            match["prefix"]
            or match["suffix"]
            or (field.get("extra_data") or {}).get("default_currency")
            or ""
        )
        return f"{currency}{float(match['amount'].replace(',', '.')):.2f}"
    raise ValueError(
        f"Zusatzfeld {field.get('name')!r}: Datentyp {data_type!r} wird (noch) nicht "
        "unterstützt (unterstützt: string, longtext, integer, float, monetary)"
    )


def _process_custom_field_step(
    step_config: StepConfig,
    ai_config: AIConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    dry_run: bool,
    force: bool = False,
) -> str | None:
    """Fill one Paperless custom field (step_config.custom_field, e.g.
    "Geldbetrag") from the document text:

    1. Look up the field definition by name (cached custom_fields list,
       re-fetched once before giving up, like _process_add_tags_step) - a
       field missing from Paperless is an error, never auto-created.
    2. Skip (before any AI call) if the condition isn't met, or if the
       document already has a value and overwrite_existing is off.
    3. Ask the AI (cached via fingerprint like a text step) and validate
       the answer against the field's data type (_parse_custom_field_answer)
       - NO_VALUE_SENTINEL or an answer of the wrong type writes nothing.
    4. Write the document's whole custom_fields list back with just this
       field's value replaced/added, so other custom fields keep their
       values whether or not Paperless treats the PATCH as a full replace.
    """
    field_name = step_config.custom_field
    field_label = f"custom_field:{field_name}"
    fields = _get_candidates(conn, paperless, "custom_fields", force)
    field = next((f for f in fields if f["name"].strip().casefold() == field_name.strip().casefold()), None)
    if field is None and not force:
        fields = _get_candidates(conn, paperless, "custom_fields", force=True)
        field = next((f for f in fields if f["name"].strip().casefold() == field_name.strip().casefold()), None)
    if field is None:
        raise ValueError(
            f"Schritt {step_config.name!r}: Zusatzfeld {field_name!r} existiert nicht in Paperless - "
            "bitte dort anlegen (ArchivLotse legt keine Zusatzfelder selbst an)"
        )

    document = paperless.get_document(document_id)

    document_type_name = _resolve_document_type_name(document, paperless, conn, force)
    correspondent_name = _resolve_correspondent_name(document, paperless, conn, force)
    if not _condition_met(step_config, document, document_type_name):
        logger.info(
            "Dokument %s, Schritt %r: Bedingung %r nicht erfüllt, Schritt übersprungen",
            document_id,
            step_config.name,
            step_config.condition,
        )
        return None

    current_fields = [
        {"field": entry["field"], "value": entry.get("value")}
        for entry in document.get("custom_fields") or []
    ]
    old_raw = next((entry["value"] for entry in current_fields if entry["field"] == field["id"]), None)
    old_value = None if old_raw in (None, "") else str(old_raw)
    if old_value is not None and not step_config.overwrite_existing:
        logger.info(
            "Dokument %s, Schritt %r: Zusatzfeld %r bereits gesetzt (%r), wird nicht überschrieben "
            "(overwrite_existing: false)",
            document_id,
            step_config.name,
            field_name,
            old_value,
        )
        return None

    input_text = str(document.get(step_config.source_field) or "").strip()
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
        correspondent_name or "",
        f"{field['name']}:{field.get('data_type')}",
    )

    cached = None if force else get_cached_step_result(conn, step_config.name, document_id, fingerprint)
    if cached is not None:
        answer = cached["output_value"]
        logger.info(
            "Dokument %s, Schritt %r: unveränderte Eingabe/Prompt/Modell, nutze "
            "zwischengespeicherte Antwort %r",
            document_id,
            step_config.name,
            answer,
        )
    else:
        prompt = render_prompt(
            step_config.prompt,
            input_text=truncated_text,
            document_type_name=document_type_name,
            correspondent_name=correspondent_name,
            field_name=field["name"],
        )
        ai_client = build_ai_client(ai_config)
        logger.info(
            "Dokument %s, Schritt %r: frage Zusatzfeld %r bei %s/%s an",
            document_id,
            step_config.name,
            field_name,
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
        answer = _postprocess(ai_result.text, step_config)
        save_step_result(
            conn,
            step_name=step_config.name,
            document_id=document_id,
            fingerprint=fingerprint,
            output_value=answer,
            provider=ai_config.provider,
            model=ai_config.model,
            prompt_name=step_config.prompt,
            prompt_version=prompt_version,
            prompt_tokens=ai_result.prompt_tokens,
            completion_tokens=ai_result.completion_tokens,
            estimated_cost_usd=cost,
        )
        logger.info("Dokument %s, Schritt %r: Antwort: %r", document_id, step_config.name, answer)

    new_raw = _parse_custom_field_answer(answer, field)
    if new_raw is None:
        if answer.strip().casefold() == NO_VALUE_SENTINEL.casefold():
            logger.info(
                "Dokument %s, Schritt %r: KI konnte keinen Wert für %r bestimmen (%s), "
                "Zusatzfeld bleibt unverändert",
                document_id,
                step_config.name,
                field_name,
                NO_VALUE_SENTINEL,
            )
        else:
            logger.warning(
                "Dokument %s, Schritt %r: Antwort %r passt nicht zum Datentyp %r von %r, "
                "wird verworfen und NICHT in Paperless übernommen",
                document_id,
                step_config.name,
                answer,
                field.get("data_type"),
                field_name,
            )
        return None

    others = [entry for entry in current_fields if entry["field"] != field["id"]]
    write_value = others + [{"field": field["id"], "value": new_raw}]
    return _apply_value(
        step_config,
        paperless,
        conn,
        document_id,
        old_value,
        str(new_raw),
        dry_run,
        write_value=write_value,
        field_label=field_label,
    )


def _apply_value(
    step_config: StepConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    old_value,
    new_value: str,
    dry_run: bool,
    write_value: int | str | list[int] | list[dict] | None = None,
    field_label: str | None = None,
) -> str:
    """Compare old vs. new value and, unless unchanged or dry_run, write
    write_value (defaulting to new_value) to Paperless and audit-log the
    human-readable old_value/new_value pair - under field_label (default:
    target_field), e.g. "custom_field:Geldbetrag" instead of the generic
    "custom_fields"."""
    if write_value is None:
        write_value = new_value
    label = field_label or step_config.target_field

    if new_value == old_value:
        logger.info(
            "Dokument %s, Feld %r: unverändert (%r), keine Aktualisierung nötig",
            document_id,
            label,
            new_value,
        )
        return new_value

    if dry_run:
        logger.info(
            "Dokument %s, Feld %r: [DRY-RUN] würde geändert: %r -> %r",
            document_id,
            label,
            old_value,
            new_value,
        )
        return new_value

    paperless.update_document_field(document_id, step_config.target_field, write_value)
    append_audit_log(conn, document_id, label, old_value, new_value)
    logger.info(
        "Dokument %s, Feld %r in Paperless geändert: %r -> %r",
        document_id,
        label,
        old_value,
        new_value,
    )
    return new_value


def _table_to_text(match: re.Match) -> str:
    lines = []
    for row in _HTML_ROW.findall(match.group(0)):
        cells = [" ".join(html.unescape(_HTML_TAG.sub(" ", cell)).split()) for cell in _HTML_CELL.findall(row)]
        cells = [cell for cell in cells if cell]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _clean_ocr_text(raw: str) -> str:
    """Turn the OCR model's markdown into plain text for Paperless."""
    text = _OCR_IMAGE_PLACEHOLDER.sub("", raw)
    text = _HTML_TABLE.sub(_table_to_text, text)
    text = re.sub(r"[ \t]+\n", "\n", text)  # markdown hard line breaks ("  ")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _text_summary(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"[{len(text)} Zeichen, {digest}]"


def _process_ocr_step(
    step_config: StepConfig,
    ai_config: AIConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    dry_run: bool,
    force: bool = False,
) -> str | None:
    """Re-OCR a scanned document with the configured OCR engine (a local
    Docling Serve, or an OCR model like LightOnOCR - both are entries
    under models: in config/ai.yaml) and replace its text - no prompt, no quality metric deciding whether to.

    Only scans are sent: a PDF original is analyzed structurally first
    (archivlotse/pdf_text.py) and left alone if it already carries real,
    visible text on every page; an image original is always a scan and is
    sent as Paperless' archived PDF (the OCR API only takes PDFs). Other
    originals (e-mail, plain text, office files) have native text and are
    skipped.

    A result far shorter than the existing text (MIN_OCR_LENGTH_RATIO) is
    not applied: the OCR model then most likely skipped part of the page.

    The raw OCR answer is cached by a hash of the file sent plus the model,
    so a document is OCR'd (and paid for) once. A cached result is applied
    unless it was already written before and the text has been changed in
    Paperless since - a manual correction is never overwritten by a cache
    hit (only by --force, which OCRs again).
    """
    document = paperless.get_document(document_id)
    old_text = str(document.get(step_config.source_field) or "")
    mime_type = str(document.get("mime_type") or "")

    if mime_type == "application/pdf":
        pdf_bytes = paperless.download_original(document_id)
        analysis = analyze_pdf(pdf_bytes)
        if not analysis.needs_ocr:
            logger.info(
                "Dokument %s, Schritt %r: PDF enthält bereits echten Text (%d Seite(n)), kein OCR nötig",
                document_id,
                step_config.name,
                analysis.page_count,
            )
            return None
        logger.info(
            "Dokument %s, Schritt %r: %d von %d Seite(n) gescannt, OCR via %s",
            document_id,
            step_config.name,
            analysis.scanned_pages,
            analysis.page_count,
            ai_config.model,
        )
    elif mime_type.startswith("image/"):
        pdf_bytes = paperless.download_archive(document_id)
        if not pdf_bytes.startswith(b"%PDF"):
            raise ServiceError(
                f"Dokument {document_id}: Bild-Original ohne PDF-Archivfassung in Paperless - "
                "kann nicht per OCR-Modell erkannt werden"
            )
        logger.info(
            "Dokument %s, Schritt %r: Bild-Original (%s), OCR der PDF-Archivfassung via %s",
            document_id,
            step_config.name,
            mime_type,
            ai_config.model,
        )
    else:
        logger.info(
            "Dokument %s, Schritt %r: Dateityp %r hat eigenen Text, kein OCR nötig",
            document_id,
            step_config.name,
            mime_type or "unbekannt",
        )
        return None

    fingerprint = _fingerprint(
        hashlib.sha256(pdf_bytes).hexdigest(), ai_config.provider, ai_config.model
    )
    cached = None if force else get_cached_step_result(conn, step_config.name, document_id, fingerprint)
    if cached is not None:
        new_text = _clean_ocr_text(cached["output_value"])
        logger.info(
            "Dokument %s, Schritt %r: nutze zwischengespeichertes OCR-Ergebnis",
            document_id,
            step_config.name,
        )
        if new_text != old_text and field_written_since(
            conn, document_id, step_config.target_field, cached["created_at"]
        ):
            logger.info(
                "Dokument %s, Schritt %r: OCR-Ergebnis wurde schon übernommen, der Text ist "
                "seitdem in Paperless geändert worden - wird nicht überschrieben "
                "(--force erkennt neu und überschreibt)",
                document_id,
                step_config.name,
            )
            return None
    else:
        ocr_client = build_ocr_client(ai_config, step_config.ocr_timeout)
        try:
            ai_result = ocr_client.ocr_pdf(pdf_bytes, f"{document_id}.pdf")
        finally:
            ocr_client.close()
        cost = _log_usage(document_id, step_config.name, ai_config, ai_result)
        save_step_result(
            conn,
            step_name=step_config.name,
            document_id=document_id,
            fingerprint=fingerprint,
            output_value=ai_result.text,
            provider=ai_config.provider,
            model=ai_config.model,
            prompt_name="-",
            prompt_version="-",
            prompt_tokens=ai_result.prompt_tokens,
            completion_tokens=ai_result.completion_tokens,
            estimated_cost_usd=cost,
        )
        new_text = _clean_ocr_text(ai_result.text)
        logger.info(
            "Dokument %s, Schritt %r: OCR abgeschlossen (%d Zeichen)",
            document_id,
            step_config.name,
            len(new_text),
        )

    if not new_text:
        logger.warning(
            "Dokument %s, Schritt %r: OCR-Ergebnis ist leer, wird NICHT übernommen",
            document_id,
            step_config.name,
        )
        return None
    if len(new_text) < len(old_text.strip()) * MIN_OCR_LENGTH_RATIO:
        logger.warning(
            "Dokument %s, Schritt %r: OCR-Ergebnis deutlich kürzer als vorhandener Text "
            "(%d < %d Zeichen, Seite evtl. als Bild statt Text erkannt), wird NICHT übernommen",
            document_id,
            step_config.name,
            len(new_text),
            len(old_text.strip()),
        )
        return None

    return _apply_value(
        step_config,
        paperless,
        conn,
        document_id,
        _text_summary(old_text),
        _text_summary(new_text),
        dry_run,
        write_value=new_text,
    )


def _process_add_tags_step(
    step_config: StepConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    dry_run: bool,
    force: bool = False,
) -> str | None:
    """Add the fixed, already existing tags named in step_config.add_tags
    (e.g. a "done" marker) to the document - no AI, no fingerprint/cache
    (it's a cheap, deterministic set union; adding an already-present tag
    is a no-op). Existing tags are never removed. A named tag missing
    from Paperless is an error, never auto-created; the (cached) tag list
    is re-fetched once before giving up, so a tag created just now in
    Paperless is found without waiting for the cache TTL."""
    tags = _get_candidates(conn, paperless, "tags", force)
    id_by_name = {tag["name"].strip().casefold(): tag["id"] for tag in tags}
    missing = [name for name in step_config.add_tags if name.strip().casefold() not in id_by_name]
    if missing and not force:
        tags = _get_candidates(conn, paperless, "tags", force=True)
        id_by_name = {tag["name"].strip().casefold(): tag["id"] for tag in tags}
        missing = [name for name in step_config.add_tags if name.strip().casefold() not in id_by_name]
    if missing:
        raise ValueError(
            f"Schritt {step_config.name!r}: Tag(s) {missing} existieren nicht in Paperless - "
            "bitte dort anlegen (ArchivLotse legt keine Tags selbst an)"
        )
    name_by_id = {tag["id"]: tag["name"] for tag in tags}

    document = paperless.get_document(document_id)
    current_ids = sorted(document.get(step_config.target_field) or [])
    new_ids = sorted(
        set(current_ids) | {id_by_name[name.strip().casefold()] for name in step_config.add_tags}
    )
    current_names = ", ".join(name_by_id.get(i, str(i)) for i in current_ids)
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


def _process_multi_choice_step(
    step_config: StepConfig,
    ai_config: AIConfig,
    paperless: PaperlessClient,
    conn: sqlite3.Connection,
    document_id: int,
    dry_run: bool,
    force: bool = False,
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

    candidates = _get_candidates(conn, paperless, step_config.candidates_endpoint, force)
    name_by_id = {candidate["id"]: candidate["name"] for candidate in candidates}
    id_by_normalized_name = {
        candidate["name"].strip().casefold(): candidate["id"] for candidate in candidates
    }

    document_type_name = _resolve_document_type_name(document, paperless, conn, force)
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

    cached = None if force else get_cached_step_result(conn, step_config.name, document_id, fingerprint)
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
