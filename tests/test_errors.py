from __future__ import annotations

import httpx

from archivlotse.errors import ServiceError, describe_expected_error


def test_describes_http_status_with_body_and_without_query():
    request = httpx.Request("GET", "https://paperless.example/api/documents/?page_size=1000")
    response = httpx.Response(500, request=request, text="Internal\n  Server Error")
    exc = httpx.HTTPStatusError("500", request=request, response=response)

    assert describe_expected_error(exc) == (
        "HTTP 500 bei GET https://paperless.example/api/documents/: Internal Server Error"
    )


def test_describes_timeout_and_service_error():
    request = httpx.Request("POST", "http://127.0.0.1:5001/v1/convert/file")
    assert describe_expected_error(httpx.ReadTimeout("timed out", request=request)) == (
        "Zeitüberschreitung bei POST http://127.0.0.1:5001/v1/convert/file"
    )
    assert describe_expected_error(ServiceError("Docling kaputt")) == "Docling kaputt"


def test_unexpected_errors_are_not_described():
    assert describe_expected_error(KeyError("x")) is None
