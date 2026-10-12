from __future__ import annotations

import argparse
import logging
import sys

import httpx

from .batch import process_batch
from .errors import ServiceError, describe_expected_error
from .logging_config import setup_logging
from .pipeline import clear_cache, process_pipeline, process_step


logger = logging.getLogger("archivlotse.cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archivlotse")
    subparsers = parser.add_subparsers(dest="command")

    process_parser = subparsers.add_parser(
        "process",
        help="Einen Verarbeitungsschritt (oder, ohne --step, die ganze Pipeline) für ein "
        "einzelnes Dokument ausführen",
    )
    process_parser.add_argument("document_id", type=int, help="Paperless-Dokument-ID")
    process_parser.add_argument(
        "--step",
        default=None,
        help="Name eines einzelnen Schritts aus config/steps.yaml, z.B. 'title'. "
        "Ohne --step werden alle Schritte aus der 'pipeline:'-Liste in "
        "config/steps.yaml der Reihe nach ausgeführt.",
    )
    process_parser.add_argument(
        "--dry-run", action="store_true", help="Nur berechnen, nicht in Paperless schreiben"
    )
    process_parser.add_argument(
        "--force",
        action="store_true",
        help="Zwischengespeicherte Ergebnisse (KI-Antworten, Kandidatenlisten) ignorieren und "
        "alles neu berechnen, auch wenn sich Eingabe/Kandidaten nicht geändert haben",
    )
    process_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Ausführliches Logging, inkl. des vollständigen an die KI gesendeten Prompts "
        "und der rohen Antwort",
    )
    process_parser.add_argument(
        "--model",
        default=None,
        help="Modell aus config/ai.yaml (models:); überschreibt das für den Schritt in "
        "config/steps.yaml hinterlegte Modell (nur zusammen mit --step)",
    )
    process_parser.add_argument("--paperless-config", default="config/paperless.yaml")
    process_parser.add_argument("--ai-config", default="config/ai.yaml")
    process_parser.add_argument("--steps-config", default="config/steps.yaml")
    process_parser.add_argument("--db", default="data/archivlotse.db")

    batch_parser = subparsers.add_parser(
        "batch",
        help="Die Batch-Jobs aus config/batch.yaml ausführen (alle passenden Dokumente "
        "nacheinander, kleinste ID zuerst)",
    )
    batch_parser.add_argument(
        "jobs",
        nargs="*",
        help="Namen der Jobs aus config/batch.yaml; ohne Angabe laufen alle Jobs der Reihe nach",
    )
    batch_parser.add_argument(
        "--dry-run", action="store_true", help="Nur berechnen, nicht in Paperless schreiben"
    )
    batch_parser.add_argument(
        "--force",
        action="store_true",
        help="Zwischengespeicherte Ergebnisse ignorieren und alles neu berechnen",
    )
    batch_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Höchstens so viele Dokumente pro Job (überschreibt 'limit:' aus batch.yaml)",
    )
    batch_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Ausführliches Logging (inkl. Prompts)"
    )
    batch_parser.add_argument("--batch-config", default="config/batch.yaml")
    batch_parser.add_argument("--paperless-config", default="config/paperless.yaml")
    batch_parser.add_argument("--ai-config", default="config/ai.yaml")
    batch_parser.add_argument("--steps-config", default="config/steps.yaml")
    batch_parser.add_argument("--db", default="data/archivlotse.db")

    clear_cache_parser = subparsers.add_parser(
        "clear-cache",
        help="Zwischengespeicherte KI-Ergebnisse und Kandidatenlisten komplett löschen "
        "(Audit-Log bleibt erhalten)",
    )
    clear_cache_parser.add_argument(
        "-v", "--verbose", action="store_true", help="Ausführliches Logging"
    )
    clear_cache_parser.add_argument("--db", default="data/archivlotse.db")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    setup_logging(verbose=getattr(args, "verbose", False))

    try:
        if args.command == "clear-cache":
            clear_cache(db_path=args.db)
        elif args.command == "batch":
            process_batch(
                job_names=args.jobs or None,
                dry_run=args.dry_run,
                force=args.force,
                limit=args.limit,
                batch_config_path=args.batch_config,
                paperless_config_path=args.paperless_config,
                ai_config_path=args.ai_config,
                steps_config_path=args.steps_config,
                db_path=args.db,
            )
        elif args.command == "process":
            if args.model is not None and args.step is None:
                parser.error("--model kann nur zusammen mit --step benutzt werden")

            if args.step is None:
                process_pipeline(
                    document_id=args.document_id,
                    dry_run=args.dry_run,
                    force=args.force,
                    paperless_config_path=args.paperless_config,
                    ai_config_path=args.ai_config,
                    steps_config_path=args.steps_config,
                    db_path=args.db,
                )
            else:
                process_step(
                    step_name=args.step,
                    document_id=args.document_id,
                    dry_run=args.dry_run,
                    force=args.force,
                    model=args.model,
                    paperless_config_path=args.paperless_config,
                    ai_config_path=args.ai_config,
                    steps_config_path=args.steps_config,
                    db_path=args.db,
                )
    except (httpx.HTTPError, ServiceError, ValueError, FileNotFoundError) as exc:
        # Erwartbare Fehler (Dienst nicht erreichbar, Konfigurationsfehler, ...)
        # als eine Zeile statt Traceback - Details mit --verbose.
        logger.error("Abbruch: %s", describe_expected_error(exc) or exc)
        logger.debug("Details", exc_info=exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
