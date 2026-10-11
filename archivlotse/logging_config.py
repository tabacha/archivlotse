from __future__ import annotations

import logging
import os


def setup_logging() -> None:
    level_name = os.environ.get("ARCHIVLOTSE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
