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
    def __init__(self, document_types=None):
        self.document = {
            "id": 1,
            "title": "Alter Titel",
            "content": "Rechnung von ACME GmbH vom 01.01.2024",
        }
        self.update_calls: list[tuple[str, object]] = []
        self.document_types = document_types or []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get_document(self, document_id):
        return dict(self.document)

    def update_document_field(self, document_id, field, value):
        self.update_calls.append((field, value))
        self.document[field] = value

    def list_document_types(self):
        return list(self.document_types)


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


FAKE_CHOICE_STEP_CONFIG = StepConfig(
    name="document_type",
    prompt="document_type.j2",
    ai_role="document_type",
    source_field="content",
    target_field="document_type",
    candidates_endpoint="document_types",
)

DOCUMENT_TYPES = [
    {"id": 1, "name": "Rechnung"},
    {"id": 2, "name": "Kontoauszug"},
]


def _patch_choice_common(monkeypatch, fake_paperless, fake_ai):
    monkeypatch.setattr(
        pipeline_module,
        "load_paperless_config",
        lambda path: PaperlessConfig(url="http://paperless.example", token="t"),
    )
    monkeypatch.setattr(
        pipeline_module,
        "load_ai_config",
        lambda role, path: AIConfig(provider="scaleway", base_url="http://scaleway.example", model="m"),
    )
    monkeypatch.setattr(
        pipeline_module, "load_step_config", lambda name, path: FAKE_CHOICE_STEP_CONFIG
    )
    monkeypatch.setattr(pipeline_module, "PaperlessClient", lambda config: fake_paperless)
    monkeypatch.setattr(pipeline_module, "build_ai_client", lambda config: fake_ai)


def test_process_choice_step_writes_matched_id_and_audits_readable_names(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(document_types=DOCUMENT_TYPES)
    fake_ai = FakeAIClient(title="Rechnung")
    _patch_choice_common(monkeypatch, fake_paperless, fake_ai)

    db_path = str(tmp_path / "test.db")

    result = pipeline_module.process_step(step_name="document_type", document_id=1, db_path=db_path)

    assert result == "Rechnung"
    # Paperless erhaelt die ID, nicht den Namen.
    assert fake_paperless.update_calls == [("document_type", 1)]

    conn = pipeline_module.init_db(db_path)
    audit_rows = conn.execute("SELECT * FROM audit_log").fetchall()
    assert len(audit_rows) == 1
    assert audit_rows[0]["field_name"] == "document_type"
    assert audit_rows[0]["new_value"] == "Rechnung"


def test_process_choice_step_rejects_hallucinated_suggestion(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(document_types=DOCUMENT_TYPES)
    fake_ai = FakeAIClient(title="Erfundener Typ")
    _patch_choice_common(monkeypatch, fake_paperless, fake_ai)

    db_path = str(tmp_path / "test.db")

    result = pipeline_module.process_step(step_name="document_type", document_id=1, db_path=db_path)

    assert result is None
    assert fake_paperless.update_calls == []

    conn = pipeline_module.init_db(db_path)
    assert conn.execute("SELECT * FROM audit_log").fetchall() == []


def test_process_choice_step_treats_sentinel_as_no_match(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(document_types=DOCUMENT_TYPES)
    fake_ai = FakeAIClient(title="UNBEKANNT")
    _patch_choice_common(monkeypatch, fake_paperless, fake_ai)

    db_path = str(tmp_path / "test.db")

    result = pipeline_module.process_step(step_name="document_type", document_id=1, db_path=db_path)

    assert result is None
    assert fake_paperless.update_calls == []


def test_process_choice_step_is_idempotent(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(document_types=DOCUMENT_TYPES)
    fake_ai = FakeAIClient(title="Kontoauszug")
    _patch_choice_common(monkeypatch, fake_paperless, fake_ai)

    db_path = str(tmp_path / "test.db")

    pipeline_module.process_step(step_name="document_type", document_id=1, db_path=db_path)
    assert fake_ai.calls == 1

    pipeline_module.process_step(step_name="document_type", document_id=1, db_path=db_path)
    assert fake_ai.calls == 1  # zweiter Lauf nutzt den Cache
    assert fake_paperless.update_calls == [("document_type", 2)]


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
