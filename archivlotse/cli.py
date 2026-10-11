from __future__ import annotations

import argparse
import sys

from .logging_config import setup_logging
from .pipeline import clear_cache, process_pipeline, process_step


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archivlotse")
    subparsers = parser.add_subparsers(dest="command")

    process_parser = subparsers.add_parser(
        "process",
        help="Einen Verarbeitungsschritt (oder, ohne --step, die ganze Pipeline) für ein "
        "Dokument ausführen",
    )
    process_parser.add_argument("document_id", type=int)
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
        "--ai-role",
        default=None,
        help="Modellrolle aus config/ai.yaml; überschreibt die für den Schritt in "
        "config/steps.yaml hinterlegte Rolle (nur zusammen mit --step)",
    )
    process_parser.add_argument("--paperless-config", default="config/paperless.yaml")
    process_parser.add_argument("--ai-config", default="config/ai.yaml")
    process_parser.add_argument("--steps-config", default="config/steps.yaml")
    process_parser.add_argument("--db", default="data/archivlotse.db")

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

    if args.command == "clear-cache":
        clear_cache(db_path=args.db)
    elif args.command == "process":
        if args.step is None:
            if args.ai_role is not None:
                parser.error("--ai-role kann nur zusammen mit --step benutzt werden")
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
                ai_role=args.ai_role,
                paperless_config_path=args.paperless_config,
                ai_config_path=args.ai_config,
                steps_config_path=args.steps_config,
                db_path=args.db,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
