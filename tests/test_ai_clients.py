from __future__ import annotations

import json

import httpx

from archivlotse.ai.ollama import OllamaClient
from archivlotse.ai.scaleway import OpenAICompatibleClient


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
