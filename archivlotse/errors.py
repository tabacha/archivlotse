from __future__ import annotations

import logging

import httpx


class ServiceError(RuntimeError):
    """An external service (e.g. Docling) answered, but reported a failure.
    The message is meant to be self-explanatory - logged as one line, no
    traceback."""


def _request_label(exc: httpx.HTTPError) -> str:
    try:
        request = exc.request
    except RuntimeError:  # httpx raises if no request is attached
        return "(unbekannte URL)"
    url = request.url.copy_with(query=None)
    return f"{request.method} {url}"


def describe_expected_error(exc: BaseException) -> str | None:
    """A one-line description for an *expected* failure - an unreachable
    service, a timeout, an HTTP error status, a service-reported error -
    or None for anything else (likely a bug, which deserves a full
    traceback)."""
    if isinstance(exc, httpx.HTTPStatusError):
        body = " ".join(exc.response.text.split())[:300]
        message = f"HTTP {exc.response.status_code} bei {_request_label(exc)}"
        return f"{message}: {body}" if body else message
    if isinstance(exc, httpx.TimeoutException):
        return f"Zeitüberschreitung bei {_request_label(exc)}"
    if isinstance(exc, httpx.ConnectError):
        return f"Dienst nicht erreichbar ({_request_label(exc)}): {exc}"
    if isinstance(exc, httpx.HTTPError):
        return f"{type(exc).__name__} bei {_request_label(exc)}: {exc}"
    if isinstance(exc, ServiceError):
        return str(exc)
    return None


def log_failure(logger: logging.Logger, context: str, exc: BaseException) -> None:
    """Log `exc` for `context` (e.g. "Dokument 5, Schritt 'title'"): expected
    failures as a single ERROR line (full traceback only at DEBUG, i.e. with
    --verbose), unexpected ones with their traceback."""
    description = describe_expected_error(exc)
    if description is None:
        logger.error("%s: unerwarteter Fehler", context, exc_info=exc)
        return
    logger.error("%s: %s", context, description)
    logger.debug("%s: Details", context, exc_info=exc)
