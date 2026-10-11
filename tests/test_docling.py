from __future__ import annotations

import httpx
import pytest

from archivlotse.docling import DoclingClient


def _client_with_response(json_body: dict, status_code: int = 200) -> DoclingClient:
    client = DoclingClient(base_url="http://docling.example")
    transport = httpx.MockTransport(lambda request: httpx.Response(status_code, json=json_body))
    client._client = httpx.Client(base_url="http://docling.example", transport=transport)
    return client


def test_convert_to_text_returns_text_on_success():
    client = _client_with_response(
        {"status": "success", "document": {"text_content": "Hallo Welt"}}
    )

    assert client.convert_to_text("a.pdf", b"%PDF-fake%") == "Hallo Welt"


def test_convert_to_text_includes_error_details_on_failure():
    client = _client_with_response(
        {
            "status": "failure",
            "errors": [
                {
                    "page_no": 1,
                    "error_message": "Image size (316750500 pixels) exceeds limit of "
                    "178956970 pixels, could be decompression bomb DOS attack.",
                }
            ],
        }
    )

    with pytest.raises(RuntimeError) as exc_info:
        client.convert_to_text("a.pdf", b"%PDF-fake%")

    assert "decompression bomb" in str(exc_info.value)
    assert "Seite 1" in str(exc_info.value)


def test_convert_to_text_failure_without_errors_has_fallback_message():
    client = _client_with_response({"status": "failure"})

    with pytest.raises(RuntimeError) as exc_info:
        client.convert_to_text("a.pdf", b"%PDF-fake%")

    assert "keine Details" in str(exc_info.value)
