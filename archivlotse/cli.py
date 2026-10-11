from __future__ import annotations

import argparse
import sys

from .logging_config import setup_logging
from .pipeline import process_step


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archivlotse")
    subparsers = parser.add_subparsers(dest="command")

    process_parser = subparsers.add_parser(
        "process",
        help="Einen in config/steps.yaml definierten Verarbeitungsschritt für ein Dokument ausführen",
    )
    process_parser.add_argument("document_id", type=int)
    process_parser.add_argument(
        "--step",
        required=True,
        help="Name des Schritts aus config/steps.yaml, z.B. 'title'",
    )
    process_parser.add_argument(
        "--dry-run", action="store_true", help="Nur berechnen, nicht in Paperless schreiben"
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
        "config/steps.yaml hinterlegte Rolle",
    )
    process_parser.add_argument("--paperless-config", default="config/paperless.yaml")
    process_parser.add_argument("--ai-config", default="config/ai.yaml")
    process_parser.add_argument("--steps-config", default="config/steps.yaml")
    process_parser.add_argument("--db", default="data/archivlotse.db")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    setup_logging(verbose=getattr(args, "verbose", False))

    if args.command == "process":
        process_step(
            step_name=args.step,
            document_id=args.document_id,
            dry_run=args.dry_run,
            ai_role=args.ai_role,
            paperless_config_path=args.paperless_config,
            ai_config_path=args.ai_config,
            steps_config_path=args.steps_config,
            db_path=args.db,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
