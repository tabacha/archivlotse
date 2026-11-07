from __future__ import annotations

import textwrap

import httpx
import pytest

import archivlotse.cli as cli_module
import archivlotse.preflight as preflight_module
from archivlotse.ai.docling import DoclingClient
from archivlotse.ai.ollama import OllamaClient
from archivlotse.ai.scaleway import OpenAICompatibleClient
from archivlotse.errors import ServiceError

STEPS_YAML = """
pipeline: [ocr, correspondent, mark_done]
steps:
  ocr:
    source_field: content
    target_field: content
    ocr:
      model: docling
  correspondent:
    source_field: content
    target_field: correspondent
    prompt: a.j2
    model: cloud-a
    candidates_endpoint: correspondents
    prompt_step2: b.j2
    model_step2: cloud-b
  mark_done:
    target_field: tags
    add_tags: [fertig]
"""

AI_YAML = """
providers:
  doc: {type: docling, base_url: "http://docling.example"}
  cloud: {type: scaleway, base_url: "http://cloud.example/v1", secret_key: "k"}
models:
  docling: {provider: doc}
  cloud-a: {provider: cloud}
  cloud-b: {provider: cloud}
"""


@pytest.fixture
def configs(tmp_path):
    (tmp_path / "steps.yaml").write_text(textwrap.dedent(STEPS_YAML), encoding="utf-8")
    (tmp_path / "ai.yaml").write_text(textwrap.dedent(AI_YAML), encoding="utf-8")
    (tmp_path / "paperless.yaml").write_text(
        'url: "http://paperless.example"\ntoken: "t"\n', encoding="utf-8"
    )
    return {
        "steps_config_path": str(tmp_path / "steps.yaml"),
        "ai_config_path": str(tmp_path / "ai.yaml"),
        "paperless_config_path": str(tmp_path / "paperless.yaml"),
    }


@pytest.fixture
def services(monkeypatch):
    """Route all HTTP traffic to fake services; `down` holds the hosts that
    refuse connections, and the requested URLs are recorded."""

    class Services:
        down: set[str] = set()
        requests: list[str] = []

    state = Services()
    state.down = set()
    state.requests = []

    def handler(request):
        state.requests.append(str(request.url.copy_with(query=None)))
        if request.url.host in state.down:
            raise httpx.ConnectError("Connection refused", request=request)
        if request.url.host == "cloud.example":
            return httpx.Response(200, json={"data": [{"id": "cloud-a"}, {"id": "cloud-b"}]})
        return httpx.Response(200, json={"results": [], "status": "ok"})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return state


def test_passes_when_all_services_are_up_and_checks_each_needed_service(configs, services):
    preflight_module.check_services(["ocr", "correspondent", "mark_done"], **configs)

    assert set(services.requests) == {
        "http://paperless.example/api/profile/",
        "http://docling.example/health",
        "http://cloud.example/v1/models",
    }


def test_only_checks_services_of_the_given_steps(configs, services):
    services.down = {"docling.example"}

    # Docling ist aus, wird fuer diesen Schritt aber nicht gebraucht.
    preflight_module.check_services(["correspondent"], **configs)

    assert "http://docling.example/health" not in services.requests


def test_ai_free_step_needs_only_paperless(configs, services):
    preflight_module.check_services(["mark_done"], **configs)

    assert services.requests == ["http://paperless.example/api/profile/"]


def test_model_override_replaces_the_configured_model(configs, services):
    services.down = {"docling.example"}

    preflight_module.check_services(["ocr"], model="cloud-a", **configs)

    assert "http://docling.example/health" not in services.requests


def test_reports_all_unreachable_services_together(configs, services):
    services.down = {"docling.example", "paperless.example"}

    with pytest.raises(ServiceError) as exc_info:
        preflight_module.check_services(["ocr", "correspondent"], **configs)

    message = str(exc_info.value)
    assert "Start verweigert" in message
    assert "Paperless" in message
    assert "'docling'" in message
    assert "cloud-a" not in message


def test_unknown_model_in_config_is_reported(configs, services):
    with pytest.raises(ServiceError, match="Unbekanntes Modell 'gibtsnicht'"):
        preflight_module.check_services(["correspondent"], model="gibtsnicht", **configs)


def _client_with(client, response):
    client._client._transport = httpx.MockTransport(lambda request: response)
    return client


def test_openai_compatible_check_rejects_model_the_provider_does_not_offer():
    client = OpenAICompatibleClient(base_url="http://ai.example", model="m", secret_key="k")
    _client_with(client, httpx.Response(200, json={"data": [{"id": "anderes"}]}))
    with pytest.raises(ServiceError, match="'m'"):
        client.check()


def test_openai_compatible_check_raises_on_rejected_key():
    client = OpenAICompatibleClient(base_url="http://ai.example", model="m", secret_key="k")
    _client_with(client, httpx.Response(401, json={}))
    with pytest.raises(httpx.HTTPStatusError):
        client.check()


@pytest.mark.parametrize("name", ["m:4b", "m:4b:latest"])
def test_ollama_check_accepts_loaded_model(name):
    client = OllamaClient(base_url="http://ollama.example", model="m:4b")
    _client_with(client, httpx.Response(200, json={"models": [{"name": name}]}))
    client.check()


def test_ollama_check_rejects_model_that_is_not_loaded():
    client = OllamaClient(base_url="http://ollama.example", model="m")
    _client_with(client, httpx.Response(200, json={"models": [{"name": "anderes:latest"}]}))
    with pytest.raises(ServiceError, match="'m'"):
        client.check()


def test_docling_check_raises_on_error_status():
    client = _client_with(DoclingClient(base_url="http://docling.example"), httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        client.check()


def _cli_args(configs):
    return [
        "--paperless-config",
        configs["paperless_config_path"],
        "--ai-config",
        configs["ai_config_path"],
        "--steps-config",
        configs["steps_config_path"],
    ]


def test_cli_process_refuses_to_start_when_service_is_down(configs, services, monkeypatch):
    services.down = {"docling.example"}
    started = []
    monkeypatch.setattr(cli_module, "process_pipeline", lambda **kwargs: started.append(kwargs))

    assert cli_module.main(["process", "1", *_cli_args(configs)]) == 1
    assert started == []


def test_cli_process_checks_the_whole_pipeline_then_starts(configs, services, monkeypatch):
    started = []
    monkeypatch.setattr(cli_module, "process_pipeline", lambda **kwargs: started.append(kwargs))

    assert cli_module.main(["process", "1", *_cli_args(configs)]) == 0
    assert len(started) == 1
    assert "http://docling.example/health" in services.requests


def test_cli_single_step_checks_only_that_step(configs, services, monkeypatch):
    services.down = {"docling.example"}
    started = []
    monkeypatch.setattr(cli_module, "process_step", lambda **kwargs: started.append(kwargs))

    assert cli_module.main(["process", "1", "--step", "correspondent", *_cli_args(configs)]) == 0
    assert len(started) == 1
