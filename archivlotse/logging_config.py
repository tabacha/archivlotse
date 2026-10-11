from __future__ import annotations

import logging
import os


def setup_logging(verbose: bool = False) -> None:
    """Configure logging. With verbose=True, only archivlotse's own
    loggers go to DEBUG (showing the full prompt sent to the AI and its
    raw response) - the chatty httpx/httpcore connection internals stay
    at INFO, so --verbose highlights what you actually asked to see."""
    if verbose:
        base_level = logging.INFO
    else:
        level_name = os.environ.get("ARCHIVLOTSE_LOG_LEVEL", "INFO").upper()
        base_level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=base_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if verbose:
        logging.getLogger("archivlotse").setLevel(logging.DEBUG)
