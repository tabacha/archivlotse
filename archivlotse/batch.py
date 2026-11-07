from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import load_paperless_config, load_yaml
from .cron import CronSchedule
from .db import init_db
from .errors import log_failure
from .paperless import PaperlessClient
from .pipeline import _get_candidates, process_pipeline
from .preflight import check_services
from .steps import load_pipeline_order, load_step_config

logger = logging.getLogger(__name__)


@dataclass
class BatchJob:
    name: str
    # Dokument muss ALLE diese Tags tragen (per Name).
    tags: list[str] = field(default_factory=list)
    # Dokument darf KEINEN dieser Tags tragen (per Name).
    exclude_tags: list[str] = field(default_factory=list)
    # Dokumenttitel muss auf MINDESTENS EINEN dieser regulaeren Ausdruecke
    # passen (re.search, also ^...$ fuer einen Treffer auf den ganzen Titel
    # selbst angeben). Leer = kein Titel-Kriterium.
    title_regex: list[re.Pattern] = field(default_factory=list)
    # Dokumenttyp muss EINER dieser sein (per Name).
    document_types: list[str] = field(default_factory=list)
    # Diese Zusatzfelder (per Name) muessen ALLE leer sein: nicht am
    # Dokument, oder ohne Wert.
    missing_custom_fields: list[str] = field(default_factory=list)
    # Diese Zusatzfelder (per Name) muessen ALLE einen Wert haben.
    filled_custom_fields: list[str] = field(default_factory=list)
    # Nur diese Schritte (aus config/steps.yaml) statt der ganzen pipeline:.
    steps: list[str] | None = None
    limit: int | None = None
    # Nur fuer "archivlotse serve": wann der Job automatisch laeuft.
    # "archivlotse batch" ignoriert das Feld.
    cron: CronSchedule | None = None


def load_batch_jobs(path: str | Path = "config/batch.yaml") -> list[BatchJob]:
    """Load all batch jobs from config/batch.yaml, in file order.

    Each job selects documents by tag names (`filter.tags`: all required,
    `filter.exclude_tags`: none allowed), title regexes
    (`filter.title_regex`: one string or a list, any may match) and/or
    document types (`filter.document_types`: any may match), optionally
    narrowed to documents whose named custom fields are all empty
    (`filter.missing_custom_fields`) and/or all filled
    (`filter.filled_custom_fields`), and
    optionally restricts which steps run
    (`steps:`, default: the whole `pipeline:` from config/steps.yaml) and
    how many documents are processed (`limit:`), and when `archivlotse
    serve` runs it on its own (`cron:`, a 5-field cron expression). A job without any filter
    criterion is rejected rather than silently matching the whole archive.
    """
    data = load_yaml(path)
    jobs_data = data.get("jobs") or {}
    if not jobs_data:
        raise ValueError(f"Keine 'jobs:' in {path} definiert")

    jobs: list[BatchJob] = []
    for name, job in jobs_data.items():
        job = job or {}
        job_filter = job.get("filter") or {}
        allowed = {
            "tags",
            "exclude_tags",
            "title_regex",
            "document_types",
            "missing_custom_fields",
            "filled_custom_fields",
        }
        unknown = set(job_filter) - allowed
        if unknown:
            raise ValueError(
                f"Batch-Job {name!r}: unbekannte Filter-Kriterien {sorted(unknown)} "
                f"(erlaubt: {', '.join(sorted(allowed))})"
            )
        title_regex = job_filter.get("title_regex") or []
        patterns = [title_regex] if isinstance(title_regex, str) else title_regex
        try:
            compiled = [re.compile(pattern) for pattern in patterns if pattern]
        except (re.error, TypeError) as exc:
            raise ValueError(f"Batch-Job {name!r}: ungültige title_regex {title_regex!r}: {exc}") from exc
        try:
            cron = CronSchedule.parse(job["cron"]) if job.get("cron") else None
        except ValueError as exc:
            raise ValueError(f"Batch-Job {name!r}: {exc}") from exc

        batch_job = BatchJob(
            name=name,
            tags=list(job_filter.get("tags") or []),
            exclude_tags=list(job_filter.get("exclude_tags") or []),
            title_regex=compiled,
            document_types=_as_list(job_filter.get("document_types")),
            missing_custom_fields=_as_list(job_filter.get("missing_custom_fields")),
            filled_custom_fields=_as_list(job_filter.get("filled_custom_fields")),
            steps=list(job["steps"]) if job.get("steps") else None,
            limit=job.get("limit"),
            cron=cron,
        )
        if not batch_job.tags and not batch_job.title_regex and not batch_job.document_types:
            raise ValueError(
                f"Batch-Job {name!r}: braucht mindestens 'filter.tags', 'filter.title_regex' oder "
                "'filter.document_types' (ein Job ohne positives Kriterium würde das ganze "
                "Archiv verarbeiten)"
            )
        jobs.append(batch_job)
    return jobs


def _as_list(value) -> list[str]:
    if not value:
        return []
    return [value] if isinstance(value, str) else list(value)


def _ids_by_name(items: list[dict]) -> dict[str, int]:
    return {item["name"].strip().casefold(): item["id"] for item in items}


def _resolve_required(job: BatchJob, names: list[str], items: list[dict], kind: str) -> set[int]:
    """Names that must exist in Paperless - an unknown one means the job
    could never match (most likely a typo), so it's an error."""
    ids = _ids_by_name(items)
    resolved: set[int] = set()
    for name in names:
        item_id = ids.get(name.strip().casefold())
        if item_id is None:
            available = ", ".join(sorted(item["name"] for item in items)) or "(keine)"
            raise ValueError(
                f"Batch-Job {job.name!r}: {kind} {name!r} existiert nicht in Paperless. "
                f"Vorhanden: {available}"
            )
        resolved.add(item_id)
    return resolved


@dataclass
class ResolvedFilter:
    """A job's filter with all names resolved to Paperless ids."""

    required_tags: set[int] = field(default_factory=set)
    excluded_tags: set[int] = field(default_factory=set)
    document_types: set[int] = field(default_factory=set)
    missing_custom_fields: set[int] = field(default_factory=set)
    filled_custom_fields: set[int] = field(default_factory=set)


def _resolve_filter(
    job: BatchJob, paperless: PaperlessClient, conn: sqlite3.Connection, force: bool = False
) -> ResolvedFilter:
    """Resolve the job's names to Paperless ids. Required tags, document
    types and custom fields must exist; an excluded tag that doesn't exist
    (yet) simply excludes nothing - e.g. a "done" marker tag nobody has
    created so far. Candidate lists are only fetched if the job uses them."""
    resolved = ResolvedFilter()
    if job.tags or job.exclude_tags:
        tags = _get_candidates(conn, paperless, "tags", force)
        resolved.required_tags = _resolve_required(job, job.tags, tags, "Tag")
        ids = _ids_by_name(tags)
        for name in job.exclude_tags:
            tag_id = ids.get(name.strip().casefold())
            if tag_id is None:
                logger.warning(
                    "Batch-Job %r: auszuschließender Tag %r existiert nicht in Paperless - "
                    "schließt daher nichts aus",
                    job.name,
                    name,
                )
                continue
            resolved.excluded_tags.add(tag_id)
    if job.document_types:
        resolved.document_types = _resolve_required(
            job, job.document_types, _get_candidates(conn, paperless, "document_types", force), "Dokumenttyp"
        )
    if job.missing_custom_fields or job.filled_custom_fields:
        custom_fields = _get_candidates(conn, paperless, "custom_fields", force)
        resolved.missing_custom_fields = _resolve_required(
            job, job.missing_custom_fields, custom_fields, "Zusatzfeld"
        )
        resolved.filled_custom_fields = _resolve_required(
            job, job.filled_custom_fields, custom_fields, "Zusatzfeld"
        )
    return resolved


def _matches(job: BatchJob, document: dict, resolved: ResolvedFilter) -> bool:
    document_tags = set(document.get("tags") or [])
    if not resolved.required_tags <= document_tags:
        return False
    if document_tags & resolved.excluded_tags:
        return False
    if resolved.document_types and document.get("document_type") not in resolved.document_types:
        return False
    if resolved.missing_custom_fields or resolved.filled_custom_fields:
        filled = {
            entry.get("field")
            for entry in document.get("custom_fields") or []
            if entry.get("value") not in (None, "")
        }
        if filled & resolved.missing_custom_fields:
            return False
        if not resolved.filled_custom_fields <= filled:
            return False
    title = document.get("title") or ""
    if job.title_regex and not any(pattern.search(title) for pattern in job.title_regex):
        return False
    return True


def select_documents(
    job: BatchJob, paperless: PaperlessClient, conn: sqlite3.Connection, force: bool = False
) -> tuple[list[int], ResolvedFilter]:
    """Return the ids (ascending) of all documents matching the job's
    filter, plus the resolved filter for re-checking.

    Tag and document-type criteria are passed to Paperless as query
    filters; the title regex and the custom-field checks are applied
    locally (Paperless's API has no regex filter, and "field missing or
    without value" is simpler to check reliably here than in its
    version-dependent custom_field_query syntax).
    """
    resolved = _resolve_filter(job, paperless, conn, force)
    documents = paperless.list_documents(
        tags_all=sorted(resolved.required_tags),
        tags_none=sorted(resolved.excluded_tags),
        document_types=sorted(resolved.document_types),
    )
    document_ids = sorted(
        document["id"] for document in documents if _matches(job, document, resolved)
    )
    return document_ids, resolved


def process_batch(
    job_names: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    limit: int | None = None,
    batch_config_path: str = "config/batch.yaml",
    paperless_config_path: str = "config/paperless.yaml",
    ai_config_path: str = "config/ai.yaml",
    steps_config_path: str = "config/steps.yaml",
    db_path: str = "data/archivlotse.db",
) -> dict[str, dict[int, object]]:
    """Run the batch jobs from config/batch.yaml (all of them in file
    order, or just `job_names`), each over its matching documents,
    smallest document id first.

    All job and step names are validated before anything is processed,
    so a typo in batch.yaml fails up front, not halfway through. Likewise,
    nothing starts unless every service those steps need is reachable
    (see check_services).

    Immediately before processing each document, it is re-fetched fresh
    and re-checked against the job's filter - the document list was
    fetched once up front, but a long batch takes a while, and the
    document may no longer match by now (tag removed by someone else, or
    title already rewritten by an earlier job in the same run). Such a
    document is skipped, not processed anyway.

    A document that raises is logged and skipped - one bad document must
    not stop the rest of the batch. This never adds/removes any tag
    itself (e.g. marking documents as done) - that's a separate decision.

    `limit` (from the CLI) overrides each job's own `limit:`.
    """
    jobs = load_batch_jobs(batch_config_path)
    if job_names:
        by_name = {job.name: job for job in jobs}
        missing = [name for name in job_names if name not in by_name]
        if missing:
            raise ValueError(
                f"Unbekannte Batch-Jobs {missing} in {batch_config_path}. "
                f"Verfügbar: {', '.join(by_name)}"
            )
        jobs = [by_name[name] for name in job_names]
    step_names: dict[str, None] = {}
    for job in jobs:
        for step_name in job.steps or load_pipeline_order(steps_config_path):
            load_step_config(step_name, steps_config_path)
            step_names[step_name] = None
    check_services(
        list(step_names),
        paperless_config_path=paperless_config_path,
        ai_config_path=ai_config_path,
        steps_config_path=steps_config_path,
    )

    paperless_config = load_paperless_config(paperless_config_path)
    conn = init_db(db_path)
    all_results: dict[str, dict[int, object]] = {}

    with PaperlessClient(paperless_config) as paperless:
        for job in jobs:
            document_ids, resolved = select_documents(job, paperless, conn, force)
            job_limit = limit if limit is not None else job.limit
            total_matches = len(document_ids)
            if job_limit is not None:
                document_ids = document_ids[:job_limit]
            logger.info(
                "Batch-Job %r: %d passende(s) Dokument(e), verarbeite %d (Schritte: %s)",
                job.name,
                total_matches,
                len(document_ids),
                ", ".join(job.steps) if job.steps else "ganze pipeline",
            )

            results: dict[int, object] = {}
            for index, document_id in enumerate(document_ids, start=1):
                logger.info(
                    "Batch-Job %r: Dokument %s (%d/%d)", job.name, document_id, index, len(document_ids)
                )
                try:
                    if not _matches(job, paperless.get_document(document_id), resolved):
                        logger.info(
                            "Batch-Job %r: Dokument %s passt inzwischen nicht mehr zum Filter - "
                            "übersprungen",
                            job.name,
                            document_id,
                        )
                        results[document_id] = None
                        continue
                    results[document_id] = process_pipeline(
                        document_id=document_id,
                        dry_run=dry_run,
                        force=force,
                        step_names=job.steps,
                        paperless_config_path=paperless_config_path,
                        ai_config_path=ai_config_path,
                        steps_config_path=steps_config_path,
                        db_path=db_path,
                    )
                except Exception as exc:
                    log_failure(
                        logger,
                        f"Batch-Job {job.name!r}: Dokument {document_id} fehlgeschlagen (weiter "
                        "mit nächstem Dokument)",
                        exc,
                    )
                    results[document_id] = None

            logger.info("Batch-Job %r abgeschlossen: %d Dokument(e)", job.name, len(results))
            all_results[job.name] = results
    return all_results
