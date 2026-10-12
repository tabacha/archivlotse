from __future__ import annotations

import textwrap

import pytest

import archivlotse.batch as batch_module
from archivlotse.config import PaperlessConfig

TAGS = [
    {"id": 1, "name": "Auto"},
    {"id": 3, "name": "TODO"},
    {"id": 9, "name": "archivlotse-done"},
]

STEPS_YAML = """
pipeline: [document_type, title]
steps:
  document_type:
    source_field: content
    target_field: document_type
    prompt: document_type.j2
    model: m
    candidates_endpoint: document_types
  title:
    source_field: content
    target_field: title
    prompt: title.j2
    model: m
"""

SCAN_TITLE_REGEX = r"^\d{4}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])_\d+$"


class FakePaperlessClient:
    """Mimics Paperless's tags__id__all/tags__id__none list filtering over
    an in-memory set of documents ({id: {"title": ..., "tags": [...]}})."""

    def __init__(self, documents, tags=TAGS):
        self.documents = documents
        self.tags = tags
        self.list_documents_calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def list_tags(self):
        return list(self.tags)

    def list_documents(self, tags_all=None, tags_none=None):
        self.list_documents_calls.append((tags_all, tags_none))
        # Absichtlich NICHT in ID-Reihenfolge, wie Paperless selbst.
        return [
            {"id": document_id, **document}
            for document_id, document in sorted(self.documents.items(), reverse=True)
            if set(tags_all or []) <= set(document["tags"])
            and not set(tags_none or []) & set(document["tags"])
        ]

    def get_document(self, document_id):
        return {"id": document_id, **self.documents[document_id]}


def _write_configs(tmp_path, batch_yaml: str) -> dict:
    (tmp_path / "batch.yaml").write_text(textwrap.dedent(batch_yaml), encoding="utf-8")
    (tmp_path / "steps.yaml").write_text(STEPS_YAML, encoding="utf-8")
    return {
        "batch_config_path": str(tmp_path / "batch.yaml"),
        "steps_config_path": str(tmp_path / "steps.yaml"),
        "db_path": str(tmp_path / "test.db"),
    }


def _patch(monkeypatch, fake_paperless, on_process=None):
    monkeypatch.setattr(
        batch_module,
        "load_paperless_config",
        lambda path: PaperlessConfig(url="http://paperless.example", token="t"),
    )
    monkeypatch.setattr(batch_module, "PaperlessClient", lambda config: fake_paperless)
    calls: list[tuple[int, list[str] | None]] = []

    def fake_process_pipeline(document_id, step_names=None, **kwargs):
        calls.append((document_id, step_names))
        if on_process:
            on_process(document_id)
        return f"ok:{document_id}"

    monkeypatch.setattr(batch_module, "process_pipeline", fake_process_pipeline)
    return calls


TODO_JOB = """
jobs:
  todo:
    filter:
      tags: [TODO]
      exclude_tags: [archivlotse-done]
"""


def test_batch_selects_by_tags_and_excludes_done_smallest_id_first(monkeypatch, tmp_path):
    fake = FakePaperlessClient(
        {
            30: {"title": "a", "tags": [3]},
            10: {"title": "b", "tags": [3, 1]},
            20: {"title": "c", "tags": [3, 9]},  # schon erledigt
            40: {"title": "d", "tags": [1]},  # kein TODO
        }
    )
    calls = _patch(monkeypatch, fake)

    results = batch_module.process_batch(**_write_configs(tmp_path, TODO_JOB))

    assert fake.list_documents_calls == [([3], [9])]
    # steps=None -> ganze pipeline: aus steps.yaml
    assert calls == [(10, None), (30, None)]
    assert results == {"todo": {10: "ok:10", 30: "ok:30"}}


def test_batch_title_regex_with_restricted_steps(monkeypatch, tmp_path):
    fake = FakePaperlessClient(
        {
            1: {"title": "20260928_124530", "tags": []},
            2: {"title": "Rechnung ACME", "tags": []},
            3: {"title": "20261399_1", "tags": []},  # kein gueltiges Datum
            4: {"title": "x20260928_1", "tags": []},
        }
    )
    calls = _patch(monkeypatch, fake)

    batch_module.process_batch(
        **_write_configs(
            tmp_path,
            f"""
            jobs:
              scan_titles:
                filter:
                  title_regex: '{SCAN_TITLE_REGEX}'
                steps: [title]
            """,
        )
    )

    assert calls == [(1, ["title"])]


def test_batch_skips_document_that_no_longer_matches(monkeypatch, tmp_path):
    documents = {
        10: {"title": "a", "tags": [3]},
        20: {"title": "b", "tags": [3]},
        30: {"title": "c", "tags": [3]},
    }
    fake = FakePaperlessClient(documents)

    def mark_20_done(document_id):
        # Waehrend Dokument 10 verarbeitet wird, markiert jemand 20 als erledigt.
        if document_id == 10:
            documents[20]["tags"] = [3, 9]

    calls = _patch(monkeypatch, fake, on_process=mark_20_done)

    results = batch_module.process_batch(**_write_configs(tmp_path, TODO_JOB))

    assert [document_id for document_id, _ in calls] == [10, 30]
    assert results["todo"][20] is None


def test_batch_runs_jobs_in_order_or_only_named_ones(monkeypatch, tmp_path):
    fake = FakePaperlessClient(
        {1: {"title": "20260101_1", "tags": [3]}, 2: {"title": "x", "tags": [3]}}
    )
    calls = _patch(monkeypatch, fake)
    configs = _write_configs(
        tmp_path,
        f"""
        jobs:
          todo:
            filter:
              tags: [TODO]
          scan_titles:
            filter:
              title_regex: '{SCAN_TITLE_REGEX}'
            steps: [title]
        """,
    )

    batch_module.process_batch(**configs)
    assert calls == [(1, None), (2, None), (1, ["title"])]

    calls.clear()
    batch_module.process_batch(job_names=["scan_titles"], **configs)
    assert calls == [(1, ["title"])]


def test_batch_limit_from_job_and_cli_override(monkeypatch, tmp_path):
    fake = FakePaperlessClient({i: {"title": "t", "tags": [3]} for i in (10, 20, 30)})
    calls = _patch(monkeypatch, fake)
    configs = _write_configs(tmp_path, TODO_JOB + "    limit: 2\n")

    batch_module.process_batch(**configs)
    assert [document_id for document_id, _ in calls] == [10, 20]

    calls.clear()
    batch_module.process_batch(limit=1, **configs)
    assert [document_id for document_id, _ in calls] == [10]


def test_batch_continues_after_one_document_fails(monkeypatch, tmp_path):
    fake = FakePaperlessClient({i: {"title": "t", "tags": [3]} for i in (10, 20, 30)})

    def boom(document_id):
        if document_id == 20:
            raise RuntimeError("boom")

    calls = _patch(monkeypatch, fake, on_process=boom)

    results = batch_module.process_batch(**_write_configs(tmp_path, TODO_JOB))

    assert [document_id for document_id, _ in calls] == [10, 20, 30]
    assert results["todo"] == {10: "ok:10", 20: None, 30: "ok:30"}


def test_batch_missing_exclude_tag_excludes_nothing(monkeypatch, tmp_path):
    fake = FakePaperlessClient({10: {"title": "t", "tags": [3]}}, tags=TAGS[:2])
    calls = _patch(monkeypatch, fake)

    batch_module.process_batch(**_write_configs(tmp_path, TODO_JOB))

    assert fake.list_documents_calls == [([3], [])]
    assert calls == [(10, None)]


def test_batch_unknown_required_tag_raises(monkeypatch, tmp_path):
    fake = FakePaperlessClient({})
    _patch(monkeypatch, fake)

    with pytest.raises(ValueError, match="Gibtsnicht"):
        batch_module.process_batch(
            **_write_configs(tmp_path, "jobs:\n  x:\n    filter:\n      tags: [Gibtsnicht]\n")
        )


@pytest.mark.parametrize(
    "batch_yaml, message",
    [
        ("jobs:\n  x:\n    filter:\n      exclude_tags: [TODO]\n", "mindestens"),
        ("jobs:\n  x:\n    filter:\n      title_regex: '('\n", "ungültige title_regex"),
        ("jobs:\n  x:\n    filter:\n      tag: [TODO]\n", "unbekannte Filter"),
        ("jobs:\n  x:\n    filter:\n      tags: [TODO]\n    steps: [nope]\n", "nope"),
    ],
)
def test_batch_invalid_config_fails_before_processing(monkeypatch, tmp_path, batch_yaml, message):
    fake = FakePaperlessClient({10: {"title": "t", "tags": [3]}})
    calls = _patch(monkeypatch, fake)

    with pytest.raises(ValueError, match=message):
        batch_module.process_batch(**_write_configs(tmp_path, batch_yaml))
    assert calls == []
    assert fake.list_documents_calls == []
