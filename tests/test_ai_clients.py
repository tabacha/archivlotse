from __future__ import annotations

import json

import httpx
import pytest

from archivlotse.ai.docling import DoclingClient
from archivlotse.ai.factory import build_ocr_client
from archivlotse.ai.ollama import OllamaClient
from archivlotse.ai.scaleway import OpenAICompatibleClient
from archivlotse.config import AIConfig
from archivlotse.errors import ServiceError


def _capture(client, response_json):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_json)

    client._client._transport = httpx.MockTransport(handler)
    return requests


def test_openai_compatible_client_sends_temperature_zero():
    client = OpenAICompatibleClient(base_url="http://ai.example", model="m", secret_key="k")
    requests = _capture(client, {"choices": [{"message": {"content": "ok"}}], "usage": {}})
    assert client.generate("p").text == "ok"
    assert requests[0]["temperature"] == 0.0


def test_ollama_client_sends_temperature_zero():
    client = OllamaClient(base_url="http://ollama.example", model="m")
    requests = _capture(client, {"response": "ok"})
    assert client.generate("p").text == "ok"
    assert requests[0]["options"] == {"temperature": 0.0}


def test_openai_compatible_client_ocr_sends_pdf_as_file_content():
    client = OpenAICompatibleClient(base_url="http://ai.example", model="ocr", secret_key="k")
    requests = _capture(
        client,
        {"choices": [{"message": {"content": "Text"}, "finish_reason": "stop"}], "usage": {}},
    )
    assert client.ocr_pdf(b"%PDF", "1.pdf").text == "Text"
    content = requests[0]["messages"][0]["content"]
    assert content == [
        {"type": "file", "file": {"file_data": "data:application/pdf;base64,JVBERg==", "filename": "1.pdf"}}
    ]


def test_openai_compatible_client_ocr_rejects_truncated_result():
    client = OpenAICompatibleClient(base_url="http://ai.example", model="ocr", secret_key="k")
    _capture(client, {"choices": [{"message": {"content": "Te"}, "finish_reason": "length"}]})
    with pytest.raises(ServiceError):
        client.ocr_pdf(b"%PDF", "1.pdf")


def _docling_with_response(json_body: dict) -> tuple[DoclingClient, list[httpx.Request]]:
    client = DoclingClient(base_url="http://docling.example")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=json_body)

    client._client._transport = httpx.MockTransport(handler)
    return client, requests


def test_docling_client_ocr_forces_ocr_and_returns_text():
    client, requests = _docling_with_response(
        {"status": "success", "document": {"text_content": "Hallo Welt"}}
    )
    result = client.ocr_pdf(b"%PDF", "1.pdf")
    assert (result.text, result.prompt_tokens) == ("Hallo Welt", None)
    body = requests[0].content
    assert requests[0].url.path == "/v1/convert/file"
    assert b'name="force_ocr"\r\n\r\ntrue' in body
    assert b'name="to_formats"\r\n\r\ntext' in body


def test_docling_client_ocr_includes_error_details_on_failure():
    client, _ = _docling_with_response(
        {"status": "failure", "errors": [{"page_no": 1, "error_message": "decompression bomb"}]}
    )
    with pytest.raises(ServiceError, match="Seite 1: decompression bomb"):
        client.ocr_pdf(b"%PDF", "1.pdf")


def test_build_ocr_client_picks_client_by_provider():
    docling = AIConfig(provider="docling", base_url="http://d", model="docling")
    scaleway = AIConfig(provider="scaleway", base_url="http://s", model="ocr", secret_key="k")
    assert isinstance(build_ocr_client(docling, 600), DoclingClient)
    assert isinstance(build_ocr_client(scaleway, 600), OpenAICompatibleClient)
    with pytest.raises(ValueError):
        build_ocr_client(AIConfig(provider="ollama", base_url="http://o", model="m"), 600)
