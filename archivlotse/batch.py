from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import load_paperless_config, load_yaml
from .db import init_db
from .errors import log_failure
from .paperless import PaperlessClient
from .pipeline import _get_candidates, process_pipeline
from .steps import load_pipeline_order, load_step_config

logger = logging.getLogger(__name__)


@dataclass
class BatchJob:
    name: str
    # Dokument muss ALLE diese Tags tragen (per Name).
    tags: list[str] = field(default_factory=list)
    # Dokument darf KEINEN dieser Tags tragen (per Name).
    exclude_tags: list[str] = field(default_factory=list)
    # Dokumenttitel muss auf diesen regulaeren Ausdruck passen (re.search,
    # also ^...$ fuer einen Treffer auf den ganzen Titel selbst angeben).
    title_regex: re.Pattern | None = None
    # Nur diese Schritte (aus config/steps.yaml) statt der ganzen pipeline:.
    steps: list[str] | None = None
    limit: int | None = None


def load_batch_jobs(path: str | Path = "config/batch.yaml") -> list[BatchJob]:
    """Load all batch jobs from config/batch.yaml, in file order.

    Each job selects documents by tag names (`filter.tags`: all required,
    `filter.exclude_tags`: none allowed) and/or a title regex
    (`filter.title_regex`), and optionally restricts which steps run
    (`steps:`, default: the whole `pipeline:` from config/steps.yaml) and
    how many documents are processed (`limit:`). A job without any filter
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
        unknown = set(job_filter) - {"tags", "exclude_tags", "title_regex"}
        if unknown:
            raise ValueError(
                f"Batch-Job {name!r}: unbekannte Filter-Kriterien {sorted(unknown)} "
                "(erlaubt: tags, exclude_tags, title_regex)"
            )
        title_regex = job_filter.get("title_regex")
        try:
            compiled = re.compile(title_regex) if title_regex else None
        except re.error as exc:
            raise ValueError(f"Batch-Job {name!r}: ungültige title_regex {title_regex!r}: {exc}") from exc

        batch_job = BatchJob(
            name=name,
            tags=list(job_filter.get("tags") or []),
            exclude_tags=list(job_filter.get("exclude_tags") or []),
            title_regex=compiled,
            steps=list(job["steps"]) if job.get("steps") else None,
            limit=job.get("limit"),
        )
        if not batch_job.tags and not batch_job.title_regex:
            raise ValueError(
                f"Batch-Job {name!r}: braucht mindestens 'filter.tags' oder 'filter.title_regex' "
                "(ein Job ohne positives Kriterium würde das ganze Archiv verarbeiten)"
            )
        jobs.append(batch_job)
    return jobs


def _tag_ids_by_name(tags: list[dict]) -> dict[str, int]:
    return {tag["name"].strip().casefold(): tag["id"] for tag in tags}


def _resolve_job_tags(job: BatchJob, tags: list[dict]) -> tuple[set[int], set[int]]:
    """Resolve the job's tag names to Paperless ids. A required tag that
    doesn't exist is an error (the job could never match anything, most
    likely a typo); an excluded tag that doesn't exist (yet) simply
    excludes nothing - e.g. a "done" marker tag nobody has created so far."""
    ids = _tag_ids_by_name(tags)
    required: set[int] = set()
    for name in job.tags:
        tag_id = ids.get(name.strip().casefold())
        if tag_id is None:
            available = ", ".join(sorted(tag["name"] for tag in tags)) or "(keine)"
            raise ValueError(
                f"Batch-Job {job.name!r}: Tag {name!r} existiert nicht in Paperless. "
                f"Vorhanden: {available}"
            )
        required.add(tag_id)
    excluded: set[int] = set()
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
        excluded.add(tag_id)
    return required, excluded


def _matches(job: BatchJob, document: dict, required: set[int], excluded: set[int]) -> bool:
    document_tags = set(document.get("tags") or [])
    if not required <= document_tags:
        return False
    if document_tags & excluded:
        return False
    if job.title_regex is not None and not job.title_regex.search(document.get("title") or ""):
        return False
    return True


def select_documents(
    job: BatchJob, paperless: PaperlessClient, conn: sqlite3.Connection, force: bool = False
) -> tuple[list[int], set[int], set[int]]:
    """Return the ids (ascending) of all documents matching the job's
    filter, plus the resolved required/excluded tag ids for re-checking.

    Tag criteria are passed to Paperless as query filters; the title
    regex is applied locally, since Paperless's API has no regex filter.
    """
    tags = _get_candidates(conn, paperless, "tags", force)
    required, excluded = _resolve_job_tags(job, tags)
    documents = paperless.list_documents(tags_all=sorted(required), tags_none=sorted(excluded))
    document_ids = sorted(
        document["id"] for document in documents if _matches(job, document, required, excluded)
    )
    return document_ids, required, excluded


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
    so a typo in batch.yaml fails up front, not halfway through.

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
    for job in jobs:
        for step_name in job.steps or load_pipeline_order(steps_config_path):
            load_step_config(step_name, steps_config_path)

    paperless_config = load_paperless_config(paperless_config_path)
    conn = init_db(db_path)
    all_results: dict[str, dict[int, object]] = {}

    with PaperlessClient(paperless_config) as paperless:
        for job in jobs:
            document_ids, required, excluded = select_documents(job, paperless, conn, force)
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
                    if not _matches(job, paperless.get_document(document_id), required, excluded):
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
