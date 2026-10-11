from __future__ import annotations

import archivlotse.pipeline as pipeline_module
from archivlotse.ai.base import AIResult
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
    def __init__(self, document_types=None, tags=None, original_bytes=b"%PDF-fake%"):
        self.document = {
            "id": 1,
            "title": "Alter Titel",
            "content": "Rechnung von ACME GmbH vom 01.01.2024",
        }
        self.update_calls: list[tuple[str, object]] = []
        self.document_types = document_types or []
        self.tags = tags or []
        self.original_bytes = original_bytes
        self.download_calls = 0
        self.list_document_types_calls = 0
        self.list_tags_calls = 0

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
        self.list_document_types_calls += 1
        return list(self.document_types)

    def list_tags(self):
        self.list_tags_calls += 1
        return list(self.tags)

    def download_original(self, document_id):
        self.download_calls += 1
        return self.original_bytes


class FakeAIClient:
    def __init__(self, title: str = "Neuer Titel", prompt_tokens: int = 10, completion_tokens: int = 5):
        self.title = title
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls = 0

    def generate(self, prompt: str) -> AIResult:
        self.calls += 1
        return AIResult(
            text=self.title, prompt_tokens=self.prompt_tokens, completion_tokens=self.completion_tokens
        )


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


def test_process_step_force_bypasses_cache(monkeypatch, tmp_path):
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

    pipeline_module.process_step(step_name="title", document_id=1, db_path=db_path)
    assert fake_ai.calls == 1

    # Unveraenderte Eingabe, aber --force -> trotzdem ein neuer KI-Aufruf,
    # kein Cache-Hit.
    pipeline_module.process_step(step_name="title", document_id=1, force=True, db_path=db_path)
    assert fake_ai.calls == 2

    conn = pipeline_module.init_db(db_path)
    # Beide Laeufe landen als eigene Zeilen in step_results (gleicher
    # Fingerprint, da force keine neue Eingabe simuliert, nur den Cache
    # umgeht - INSERT OR ... gibt es hier nicht, UNIQUE-Konflikt waere ein
    # Bug, pruefen wir also implizit mit).
    assert len(conn.execute("SELECT * FROM step_results").fetchall()) == 1


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


FAKE_CONDITIONAL_STEP_CONFIG = StepConfig(
    name="kontonummer",
    prompt="title.j2",
    ai_role="title",
    source_field="content",
    target_field="title",
    condition={"document_type": "Kontoauszug"},
)


def test_process_step_skips_when_condition_not_met(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(document_types=DOCUMENT_TYPES)
    fake_paperless.document["document_type"] = 1  # "Rechnung", nicht "Kontoauszug"
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
    monkeypatch.setattr(
        pipeline_module, "load_step_config", lambda name, path: FAKE_CONDITIONAL_STEP_CONFIG
    )
    monkeypatch.setattr(pipeline_module, "PaperlessClient", lambda config: fake_paperless)
    monkeypatch.setattr(pipeline_module, "build_ai_client", lambda config: fake_ai)

    result = pipeline_module.process_step(
        step_name="kontonummer", document_id=1, db_path=str(tmp_path / "test.db")
    )

    assert result is None
    assert fake_ai.calls == 0
    assert fake_paperless.update_calls == []


def test_process_step_runs_when_condition_met(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(document_types=DOCUMENT_TYPES)
    fake_paperless.document["document_type"] = 2  # "Kontoauszug"
    fake_ai = FakeAIClient(title="Kontoauszug-Titel")

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
    monkeypatch.setattr(
        pipeline_module, "load_step_config", lambda name, path: FAKE_CONDITIONAL_STEP_CONFIG
    )
    monkeypatch.setattr(pipeline_module, "PaperlessClient", lambda config: fake_paperless)
    monkeypatch.setattr(pipeline_module, "build_ai_client", lambda config: fake_ai)

    result = pipeline_module.process_step(
        step_name="kontonummer", document_id=1, db_path=str(tmp_path / "test.db")
    )

    assert result == "Kontoauszug-Titel"
    assert fake_ai.calls == 1


FAKE_OCR_STEP_CONFIG = StepConfig(
    name="ocr_check",
    prompt=None,
    ai_role=None,
    source_field="content",
    target_field="content",
    quality_check="heuristic",
    quality_threshold=0.5,
    min_chars_for_check=10,
    docling_base_url="http://docling.example",
)


class FakeDoclingClient:
    def __init__(self, text="Dies ist ein ordentlicher deutscher Satz mit Wörtern.", **kwargs):
        self.text = text
        self.calls = 0

    def convert_to_text(self, filename, content, force_ocr=True):
        self.calls += 1
        return self.text

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def _patch_ocr_common(monkeypatch, fake_paperless, fake_docling_factory):
    monkeypatch.setattr(
        pipeline_module,
        "load_paperless_config",
        lambda path: PaperlessConfig(url="http://paperless.example", token="t"),
    )
    monkeypatch.setattr(
        pipeline_module, "load_step_config", lambda name, path: FAKE_OCR_STEP_CONFIG
    )
    monkeypatch.setattr(pipeline_module, "PaperlessClient", lambda config: fake_paperless)
    monkeypatch.setattr(pipeline_module, "DoclingClient", fake_docling_factory)


def test_ocr_step_skips_reocr_when_quality_already_good(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient()
    fake_paperless.document["content"] = (
        "Dies ist ein ganz normaler, gut lesbarer deutscher Text mit vielen "
        "gewöhnlichen Wörtern und Sätzen, der keine Verbesserung braucht."
    )
    docling_calls = []
    _patch_ocr_common(
        monkeypatch, fake_paperless, lambda **kw: docling_calls.append(kw) or FakeDoclingClient()
    )

    result = pipeline_module.process_step(
        step_name="ocr_check", document_id=1, db_path=str(tmp_path / "test.db")
    )

    assert result is None
    assert docling_calls == []  # kein Docling-Aufruf noetig
    assert fake_paperless.download_calls == 0
    assert fake_paperless.update_calls == []


def test_ocr_step_reocrs_and_applies_when_quality_bad(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient()
    fake_paperless.document["content"] = "xq zrv kpt wmb flo " * 20  # Kauderwelsch, kein Wort>=3 bekannt
    fake_docling = FakeDoclingClient(
        text=(
            "Dies ist ein ordentlicher deutscher Satz mit vielen Wörtern, "
            "der nach der erneuten Erkennung viel besser lesbar ist."
        )
    )
    _patch_ocr_common(monkeypatch, fake_paperless, lambda **kw: fake_docling)

    result = pipeline_module.process_step(
        step_name="ocr_check", document_id=1, db_path=str(tmp_path / "test.db")
    )

    assert result is not None
    assert fake_docling.calls == 1
    assert fake_paperless.download_calls == 1
    assert fake_paperless.update_calls[0][0] == "content"
    assert fake_paperless.update_calls[0][1] == fake_docling.text


def test_ocr_step_rejects_docling_result_that_is_far_shorter(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient()
    fake_paperless.document["content"] = "xq zrv kpt wmb flo " * 20
    fake_docling = FakeDoclingClient(text="zu kurz")
    _patch_ocr_common(monkeypatch, fake_paperless, lambda **kw: fake_docling)

    result = pipeline_module.process_step(
        step_name="ocr_check", document_id=1, db_path=str(tmp_path / "test.db")
    )

    assert result is None
    assert fake_paperless.update_calls == []


def test_process_pipeline_runs_all_steps_in_order_and_survives_a_failure(monkeypatch, tmp_path):
    call_order = []

    def fake_process_step(step_name, document_id, **kwargs):
        call_order.append(step_name)
        if step_name == "broken":
            raise RuntimeError("boom")
        return f"ok:{step_name}"

    monkeypatch.setattr(pipeline_module, "load_pipeline_order", lambda path: ["a", "broken", "b"])
    monkeypatch.setattr(pipeline_module, "process_step", fake_process_step)

    results = pipeline_module.process_pipeline(document_id=1, db_path=str(tmp_path / "test.db"))

    assert call_order == ["a", "broken", "b"]
    assert results == {"a": "ok:a", "broken": None, "b": "ok:b"}


FAKE_MULTI_CHOICE_STEP_CONFIG = StepConfig(
    name="tags",
    prompt="tags.j2",
    ai_role="tags",
    source_field="content",
    target_field="tags",
    candidates_endpoint="tags",
    multi=True,
)

TAGS = [
    {"id": 1, "name": "Auto"},
    {"id": 2, "name": "Rechnung"},
    {"id": 3, "name": "TODO"},
]


def _patch_multi_choice_common(monkeypatch, fake_paperless, fake_ai):
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
        pipeline_module, "load_step_config", lambda name, path: FAKE_MULTI_CHOICE_STEP_CONFIG
    )
    monkeypatch.setattr(pipeline_module, "PaperlessClient", lambda config: fake_paperless)
    monkeypatch.setattr(pipeline_module, "build_ai_client", lambda config: fake_ai)


def test_multi_choice_step_adds_valid_tags_and_drops_invalid_ones(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(tags=TAGS)
    fake_paperless.document["tags"] = [3]  # bereits "TODO"
    fake_ai = FakeAIClient(title="Auto, Rechnung, Erfundener Tag")
    _patch_multi_choice_common(monkeypatch, fake_paperless, fake_ai)

    result = pipeline_module.process_step(
        step_name="tags", document_id=1, db_path=str(tmp_path / "test.db")
    )

    assert result is not None
    assert fake_paperless.update_calls == [("tags", [1, 2, 3])]  # Auto, Rechnung, TODO

    conn = pipeline_module.init_db(str(tmp_path / "test.db"))
    audit_rows = conn.execute("SELECT * FROM audit_log").fetchall()
    assert len(audit_rows) == 1
    assert audit_rows[0]["field_name"] == "tags"


def test_multi_choice_step_keeps_existing_tags_when_none_match(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(tags=TAGS)
    fake_paperless.document["tags"] = [3]  # "TODO"
    fake_ai = FakeAIClient(title="KEINE")
    _patch_multi_choice_common(monkeypatch, fake_paperless, fake_ai)

    result = pipeline_module.process_step(
        step_name="tags", document_id=1, db_path=str(tmp_path / "test.db")
    )

    assert result is not None
    assert fake_paperless.update_calls == []  # unveraendert, "TODO" bleibt


def test_multi_choice_step_is_idempotent(monkeypatch, tmp_path):
    # --dry-run, damit sich die "aktuell gesetzten Tags" zwischen den
    # Laeufen nicht aendern (die gehoeren zum Fingerprint, siehe
    # test_multi_choice_step_recomputes_when_current_tags_change).
    fake_paperless = FakePaperlessClient(tags=TAGS)
    fake_ai = FakeAIClient(title="Auto")
    _patch_multi_choice_common(monkeypatch, fake_paperless, fake_ai)

    db_path = str(tmp_path / "test.db")
    pipeline_module.process_step(step_name="tags", document_id=1, dry_run=True, db_path=db_path)
    assert fake_ai.calls == 1

    pipeline_module.process_step(step_name="tags", document_id=1, dry_run=True, db_path=db_path)
    assert fake_ai.calls == 1  # zweiter Lauf nutzt den Cache


def test_multi_choice_step_recomputes_when_current_tags_change(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(tags=TAGS)
    fake_ai = FakeAIClient(title="Hinzufügen: Auto\nLöschen: KEINE")
    _patch_multi_choice_common(monkeypatch, fake_paperless, fake_ai)

    db_path = str(tmp_path / "test.db")
    pipeline_module.process_step(step_name="tags", document_id=1, db_path=db_path)
    assert fake_ai.calls == 1
    assert fake_paperless.document["tags"] == [1]  # "Auto" tatsaechlich gesetzt

    # Die aktuellen Tags haben sich durch den ersten Lauf geaendert (sind
    # Teil des Prompts/Fingerprints) -> neuer KI-Aufruf ist korrekt, kein
    # Cache-Hit.
    pipeline_module.process_step(step_name="tags", document_id=1, db_path=db_path)
    assert fake_ai.calls == 2


def test_candidate_lists_are_cached_across_calls(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(tags=TAGS)
    fake_ai = FakeAIClient(title="Auto")
    _patch_multi_choice_common(monkeypatch, fake_paperless, fake_ai)

    db_path = str(tmp_path / "test.db")
    pipeline_module.process_step(step_name="tags", document_id=1, db_path=db_path)
    pipeline_module.process_step(step_name="tags", document_id=1, db_path=db_path)

    # Zwei komplette Prozessdurchlaeufe, aber die Tag-Liste wird nur einmal
    # tatsaechlich von Paperless geholt (Cache greift beim zweiten Mal).
    assert fake_paperless.list_tags_calls == 1


def test_force_bypasses_candidate_cache_too(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(tags=TAGS)
    fake_ai = FakeAIClient(title="Auto")
    _patch_multi_choice_common(monkeypatch, fake_paperless, fake_ai)

    db_path = str(tmp_path / "test.db")
    pipeline_module.process_step(step_name="tags", document_id=1, force=True, db_path=db_path)
    pipeline_module.process_step(step_name="tags", document_id=1, force=True, db_path=db_path)

    # --force holt die Tag-Liste bei jedem Lauf neu, nicht nur beim ersten.
    assert fake_paperless.list_tags_calls == 2


def test_multi_choice_step_adds_and_deletes_in_one_answer(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(tags=TAGS)
    fake_paperless.document["tags"] = [2, 3]  # "Rechnung", "TODO"
    fake_ai = FakeAIClient(title="Hinzufügen: Auto\nLöschen: Rechnung")
    _patch_multi_choice_common(monkeypatch, fake_paperless, fake_ai)

    pipeline_module.process_step(step_name="tags", document_id=1, db_path=str(tmp_path / "test.db"))

    # Rechnung entfernt, Auto hinzugefuegt, TODO unveraendert erhalten.
    assert fake_paperless.update_calls == [("tags", [1, 3])]


def test_multi_choice_step_delete_wins_on_add_delete_conflict(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(tags=TAGS)
    fake_paperless.document["tags"] = [3]  # "TODO"
    fake_ai = FakeAIClient(title="Hinzufügen: Auto\nLöschen: Auto")
    _patch_multi_choice_common(monkeypatch, fake_paperless, fake_ai)

    pipeline_module.process_step(step_name="tags", document_id=1, db_path=str(tmp_path / "test.db"))

    # "Auto" steht in beiden Listen -> wird nicht gesetzt (Loeschen gewinnt),
    # bestehendes "TODO" bleibt unveraendert -> insgesamt keine Aenderung.
    assert fake_paperless.update_calls == []


def test_multi_choice_step_never_deletes_protected_candidates(monkeypatch, tmp_path):
    protected_config = StepConfig(
        name="tags",
        prompt="tags.j2",
        ai_role="tags",
        source_field="content",
        target_field="tags",
        candidates_endpoint="tags",
        multi=True,
        protected_candidates=["TODO"],
    )
    fake_paperless = FakePaperlessClient(tags=TAGS)
    fake_paperless.document["tags"] = [3]  # "TODO"
    fake_ai = FakeAIClient(title="Hinzufügen: Auto\nLöschen: TODO")
    _patch_multi_choice_common(monkeypatch, fake_paperless, fake_ai)
    monkeypatch.setattr(pipeline_module, "load_step_config", lambda name, path: protected_config)

    pipeline_module.process_step(step_name="tags", document_id=1, db_path=str(tmp_path / "test.db"))

    # "TODO" ist geschuetzt -> Loeschvorschlag wird ignoriert, bleibt
    # erhalten; "Auto" wird trotzdem hinzugefuegt.
    assert fake_paperless.update_calls == [("tags", [1, 3])]


def test_multi_choice_step_passes_current_tags_to_prompt(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient(tags=TAGS)
    fake_paperless.document["tags"] = [3]  # "TODO"
    fake_ai = FakeAIClient(title="KEINE")
    _patch_multi_choice_common(monkeypatch, fake_paperless, fake_ai)

    captured = {}
    original_render = pipeline_module.render_prompt

    def capturing_render(name, **kwargs):
        captured.update(kwargs)
        return original_render(name, **kwargs)

    monkeypatch.setattr(pipeline_module, "render_prompt", capturing_render)

    pipeline_module.process_step(step_name="tags", document_id=1, db_path=str(tmp_path / "test.db"))

    assert captured["current_tag_names"] == ["TODO"]
    assert captured["candidate_names"] == ["Auto", "Rechnung", "TODO"]


def test_estimate_cost_is_zero_for_ollama_regardless_of_pricing():
    config = AIConfig(provider="ollama", base_url="http://x", model="m")
    assert pipeline_module._estimate_cost(config, 1000, 1000) == 0.0


def test_estimate_cost_returns_none_without_configured_pricing():
    config = AIConfig(provider="scaleway", base_url="http://x", model="m")
    assert pipeline_module._estimate_cost(config, 1000, 1000) is None


def test_estimate_cost_computes_from_configured_pricing():
    config = AIConfig(
        provider="scaleway",
        base_url="http://x",
        model="m",
        price_per_1m_input=1.0,
        price_per_1m_output=2.0,
    )
    # 500_000 Tokens Input (0.5 * $1) + 250_000 Tokens Output (0.25 * $2)
    cost = pipeline_module._estimate_cost(config, 500_000, 250_000)
    assert cost == 1.0


def test_step_results_store_token_usage_and_cost(monkeypatch, tmp_path):
    fake_paperless = FakePaperlessClient()
    fake_ai = FakeAIClient(title="Neuer Titel", prompt_tokens=123, completion_tokens=45)

    monkeypatch.setattr(
        pipeline_module,
        "load_paperless_config",
        lambda path: PaperlessConfig(url="http://paperless.example", token="t"),
    )
    monkeypatch.setattr(
        pipeline_module,
        "load_ai_config",
        lambda role, path: AIConfig(
            provider="scaleway",
            base_url="http://scaleway.example",
            model="m",
            price_per_1m_input=1.0,
            price_per_1m_output=2.0,
        ),
    )
    monkeypatch.setattr(pipeline_module, "load_step_config", lambda name, path: FAKE_STEP_CONFIG)
    monkeypatch.setattr(pipeline_module, "PaperlessClient", lambda config: fake_paperless)
    monkeypatch.setattr(pipeline_module, "build_ai_client", lambda config: fake_ai)

    db_path = str(tmp_path / "test.db")
    pipeline_module.process_step(step_name="title", document_id=1, db_path=db_path)

    conn = pipeline_module.init_db(db_path)
    row = conn.execute("SELECT * FROM step_results").fetchone()
    assert row["prompt_tokens"] == 123
    assert row["completion_tokens"] == 45
    assert row["estimated_cost_usd"] == 123 / 1_000_000 * 1.0 + 45 / 1_000_000 * 2.0
