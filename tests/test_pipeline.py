from __future__ import annotations

import archivlotse.pipeline as pipeline_module
from archivlotse.config import AIConfig, PaperlessConfig
from archivlotse.steps import StepConfig

FAKE_STEP_CONFIG = StepConfig(
    name="title",
    prompt="title.j2",
    ai_role="title",
    source_field="content",
    target_field="title",
    max_input_chars=8000,
    strip_quotes=True,
    max_output_length=120,
)


class FakePaperlessClient:
    def __init__(self):
        self.document = {
            "id": 1,
            "title": "Alter Titel",
            "content": "Rechnung von ACME GmbH vom 01.01.2024",
        }
        self.update_calls: list[tuple[str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get_document(self, document_id):
        return dict(self.document)

    def update_document_field(self, document_id, field, value):
        self.update_calls.append((field, value))
        self.document[field] = value


class FakeAIClient:
    def __init__(self, title: str = "Neuer Titel"):
        self.title = title
        self.calls = 0

    def generate(self, prompt: str) -> str:
        self.calls += 1
        return self.title


def test_process_step_is_idempotent_and_audits_only_on_change(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient()
    fake_ai = FakeAIClient()

    monkeypatch.setattr(
        pipeline_module,
        "load_paperless_config",
        lambda path: PaperlessConfig(url="http://paperless.example", token="t"),
    )
    monkeypatch.setattr(
        pipeline_module,
        "load_ai_config",
        lambda role, path: AIConfig(provider="ollama", base_url="http://ollama.example", model="m"),
    )
    monkeypatch.setattr(pipeline_module, "load_step_config", lambda name, path: FAKE_STEP_CONFIG)
    monkeypatch.setattr(pipeline_module, "PaperlessClient", lambda config: fake_paperless)
    monkeypatch.setattr(pipeline_module, "build_ai_client", lambda config: fake_ai)

    db_path = str(tmp_path / "test.db")

    result1 = pipeline_module.process_step(step_name="title", document_id=1, db_path=db_path)
    assert result1 == "Neuer Titel"
    assert fake_ai.calls == 1
    assert fake_paperless.update_calls == [("title", "Neuer Titel")]

    # Zweiter Lauf: Eingabe, Prompt, Modell und Nachbearbeitung sind
    # unverändert -> kein erneuter KI-Aufruf, und da das Zielfeld in
    # Paperless bereits aktuell ist, auch kein erneutes Audit-Log.
    result2 = pipeline_module.process_step(step_name="title", document_id=1, db_path=db_path)
    assert result2 == "Neuer Titel"
    assert fake_ai.calls == 1
    assert fake_paperless.update_calls == [("title", "Neuer Titel")]

    conn = pipeline_module.init_db(db_path)

    audit_rows = conn.execute("SELECT * FROM audit_log").fetchall()
    assert len(audit_rows) == 1
    assert audit_rows[0]["field_name"] == "title"
    assert audit_rows[0]["old_value"] == "Alter Titel"
    assert audit_rows[0]["new_value"] == "Neuer Titel"

    result_rows = conn.execute("SELECT * FROM step_results").fetchall()
    assert len(result_rows) == 1
    assert result_rows[0]["step_name"] == "title"
    assert result_rows[0]["provider"] == "ollama"
    assert result_rows[0]["model"] == "m"


def test_process_step_recomputes_when_input_changes(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient()
    fake_ai = FakeAIClient(title="Titel A")

    monkeypatch.setattr(
        pipeline_module,
        "load_paperless_config",
        lambda path: PaperlessConfig(url="http://paperless.example", token="t"),
    )
    monkeypatch.setattr(
        pipeline_module,
        "load_ai_config",
        lambda role, path: AIConfig(provider="ollama", base_url="http://ollama.example", model="m"),
    )
    monkeypatch.setattr(pipeline_module, "load_step_config", lambda name, path: FAKE_STEP_CONFIG)
    monkeypatch.setattr(pipeline_module, "PaperlessClient", lambda config: fake_paperless)
    monkeypatch.setattr(pipeline_module, "build_ai_client", lambda config: fake_ai)

    db_path = str(tmp_path / "test.db")

    pipeline_module.process_step(step_name="title", document_id=1, db_path=db_path)
    assert fake_ai.calls == 1

    fake_paperless.document["content"] = "Ein ganz anderer Dokumentinhalt"
    fake_ai.title = "Titel B"

    result = pipeline_module.process_step(step_name="title", document_id=1, db_path=db_path)
    assert result == "Titel B"
    assert fake_ai.calls == 2


def test_postprocess_strips_quotes_and_enforces_max_length():
    step_config = StepConfig(
        name="title",
        prompt="title.j2",
        ai_role="title",
        source_field="content",
        target_field="title",
        strip_quotes=True,
        max_output_length=10,
    )

    result = pipeline_module._postprocess('  "Ein sehr langer Titel"  \n', step_config)

    assert result == "Ein sehr l"
