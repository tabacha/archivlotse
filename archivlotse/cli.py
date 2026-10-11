from __future__ import annotations

import argparse
import sys

from .logging_config import setup_logging
from .title import process_title


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archivlotse")
    subparsers = parser.add_subparsers(dest="command", required=True)

    title_parser = subparsers.add_parser(
        "title", help="Dokumenttitel aus OCR-Text berechnen und in Paperless aktualisieren"
    )
    title_parser.add_argument("document_id", type=int)
    title_parser.add_argument(
        "--dry-run", action="store_true", help="Nur berechnen, nicht in Paperless schreiben"
    )
    title_parser.add_argument(
        "--ai-role",
        default=None,
        help="Modellrolle aus config/ai.yaml; überschreibt die in config/steps.yaml "
        "für den Schritt 'title' hinterlegte Rolle",
    )
    title_parser.add_argument("--paperless-config", default="config/paperless.yaml")
    title_parser.add_argument("--ai-config", default="config/ai.yaml")
    title_parser.add_argument("--steps-config", default="config/steps.yaml")
    title_parser.add_argument("--db", default="data/archivlotse.db")

    args = parser.parse_args(argv)
    setup_logging()

    if args.command == "title":
        process_title(
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
