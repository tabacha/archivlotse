from __future__ import annotations

import datetime
import json
import textwrap
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

import archivlotse.server as server_module
from archivlotse.batch import load_batch_jobs
from archivlotse.cron import CronSchedule
from archivlotse.server import (
    Scheduler,
    ServerSettings,
    WebhookError,
    Worker,
    make_handler,
    parse_webhook_request,
)

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

TOKEN = "geheim"


# --- Cron -----------------------------------------------------------------


def test_cron_parses_lists_ranges_steps_and_aliases():
    schedule = CronSchedule.parse("*/15 8-18/2 * * 1-5")
    assert schedule.minutes == {0, 15, 30, 45}
    assert schedule.hours == {8, 10, 12, 14, 16, 18}
    assert schedule.weekdays == {1, 2, 3, 4, 5}
    assert CronSchedule.parse("@daily").minutes == {0}
    assert CronSchedule.parse("5/20 * * * *").minutes == {5, 25, 45}


def test_cron_matches_local_time_and_weekday_numbering():
    schedule = CronSchedule.parse("30 3 * * 0")  # Sonntag 3:30
    assert schedule.matches(datetime.datetime(2026, 11, 8, 3, 30))  # ein Sonntag
    assert not schedule.matches(datetime.datetime(2026, 11, 9, 3, 30))  # Montag
    assert CronSchedule.parse("30 3 * * 7").matches(datetime.datetime(2026, 11, 8, 3, 30))


def test_cron_day_and_weekday_restricted_means_either():
    schedule = CronSchedule.parse("0 0 1 * 1")  # am 1. ODER montags
    assert schedule.matches(datetime.datetime(2026, 11, 1, 0, 0))  # Sonntag, der 1.
    assert schedule.matches(datetime.datetime(2026, 11, 9, 0, 0))  # Montag
    assert not schedule.matches(datetime.datetime(2026, 11, 10, 0, 0))


@pytest.mark.parametrize("expression", ["* * * *", "60 * * * *", "* * 0 * *", "*/0 * * * *", "a * * * *"])
def test_cron_rejects_invalid_expressions(expression):
    with pytest.raises(ValueError):
        CronSchedule.parse(expression)


def test_batch_job_cron_is_loaded_and_validated(tmp_path):
    path = tmp_path / "batch.yaml"
    path.write_text(
        textwrap.dedent(
            """
            jobs:
              a:
                filter: {tags: [TODO]}
                cron: "0 3 * * *"
              b:
                filter: {tags: [TODO]}
            """
        ),
        encoding="utf-8",
    )
    jobs = load_batch_jobs(path)
    assert jobs[0].cron.hours == {3}
    assert jobs[1].cron is None

    path.write_text("jobs:\n  a:\n    filter: {tags: [TODO]}\n    cron: '99 * * * *'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Batch-Job 'a'"):
        load_batch_jobs(path)


# --- Scheduler / Worker ---------------------------------------------------


def _settings(tmp_path, **kwargs) -> ServerSettings:
    steps = tmp_path / "steps.yaml"
    steps.write_text(STEPS_YAML, encoding="utf-8")
    return ServerSettings(webhook_token=TOKEN, steps_config_path=str(steps), db_path=str(tmp_path / "t.db"), **kwargs)


def test_scheduler_queues_due_jobs_once_and_catches_up_missed_minutes(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(server_module, "process_batch", lambda job_names, **kw: ran.append((job_names, kw["dry_run"])))
    batch = tmp_path / "batch.yaml"
    batch.write_text(
        "jobs:\n"
        "  nachts: {filter: {tags: [TODO]}, cron: '0 3 * * *'}\n"
        "  oft: {filter: {tags: [TODO]}, cron: '*/5 * * * *'}\n"
        "  nie: {filter: {tags: [TODO]}}\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path, dry_run=True)
    worker = Worker()
    scheduler = Scheduler(settings, worker, load_batch_jobs(batch))

    assert scheduler.tick(datetime.datetime(2026, 11, 7, 2, 58, 1)) == []
    # Tick fuer 3:00 verpasst (z.B. Standby) - wird beim naechsten nachgeholt.
    assert scheduler.tick(datetime.datetime(2026, 11, 7, 3, 1, 0)) == ["nachts", "oft"]
    # Dieselbe Minute erneut: nichts doppelt.
    assert scheduler.tick(datetime.datetime(2026, 11, 7, 3, 1, 30)) == []
    # "oft" wartet noch in der Queue -> nicht ein zweites Mal eingereiht.
    assert scheduler.tick(datetime.datetime(2026, 11, 7, 3, 5, 0)) == []

    while worker.run_one(timeout=0):
        pass
    assert ran == [(["nachts"], True), (["oft"], True)]


def test_worker_survives_failing_task_and_dedupes_pending():
    worker = Worker()
    calls = []

    def boom():
        raise RuntimeError("kaputt")

    assert worker.submit(server_module.Task(("x",), "x", boom))
    assert not worker.submit(server_module.Task(("x",), "x", boom))
    assert worker.submit(server_module.Task(("y",), "y", lambda: calls.append("y")))
    assert worker.run_one(timeout=0) and worker.run_one(timeout=0)
    assert calls == ["y"]
    assert not worker.run_one(timeout=0)
    # nach dem Lauf darf derselbe Key wieder eingereiht werden
    assert worker.submit(server_module.Task(("x",), "x", boom))


# --- Webhook-Parsing ------------------------------------------------------


def test_parse_webhook_request_variants(tmp_path):
    steps = _settings(tmp_path).steps_config_path
    assert parse_webhook_request({"document_id": 12}, steps) == (12, None, False, False)
    assert parse_webhook_request({"doc_id": ["7"], "steps": ["title,document_type"]}, steps) == (
        7, ["title", "document_type"], False, False
    )
    assert parse_webhook_request(
        {"doc_url": "https://paperless.example.com/documents/4711/details", "step": "title", "dry_run": "true"},
        steps,
    ) == (4711, ["title"], True, False)
    assert parse_webhook_request({"id": 3, "steps": ["title"], "force": True}, steps) == (3, ["title"], False, True)

    with pytest.raises(WebhookError, match="fehlt"):
        parse_webhook_request({}, steps)
    with pytest.raises(WebhookError, match="keine Zahl"):
        parse_webhook_request({"document_id": "abc"}, steps)
    with pytest.raises(WebhookError, match="Unbekannter Verarbeitungsschritt"):
        parse_webhook_request({"document_id": 1, "steps": ["gibtsnicht"]}, steps)
    with pytest.raises(WebhookError, match="Wahrheitswert"):
        parse_webhook_request({"document_id": 1, "dry_run": "vielleicht"}, steps)


# --- HTTP -----------------------------------------------------------------


@pytest.fixture
def running_server(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(server_module, "check_services", lambda *a, **kw: None)
    monkeypatch.setattr(
        server_module,
        "process_pipeline",
        lambda document_id, dry_run, force, step_names, **kw: calls.append((document_id, step_names, dry_run, force)),
    )
    settings = _settings(tmp_path)
    worker = Worker()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(settings, worker))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, worker, calls
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_webhook_requires_token(running_server):
    base, worker, _ = running_server
    assert httpx.post(f"{base}/process", json={"document_id": 1}).status_code == 401
    assert httpx.post(f"{base}/process", json={"document_id": 1}, headers={"Authorization": "Bearer falsch"}).status_code == 401
    assert worker.queued() == 0


def test_webhook_queues_document_and_worker_runs_it(running_server):
    base, worker, calls = running_server
    response = httpx.post(
        f"{base}/process", json={"document_id": 5, "steps": ["title"]}, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 202
    assert response.json() == {"document_id": 5, "steps": ["title"], "dry_run": False, "force": False, "queued": True}

    # gleicher Auftrag nochmal, solange er noch wartet -> nicht doppelt
    again = httpx.post(
        f"{base}/process?document_id=5&steps=title", headers={"X-Archivlotse-Token": TOKEN}
    )
    assert again.status_code == 202 and again.json()["queued"] is False

    # Formular-Body (Paperless-Webhook "use_params")
    form = httpx.post(
        f"{base}/webhook",
        data={"doc_url": "http://p/documents/9/", "dry_run": "1"},
        headers={"X-Archivlotse-Token": TOKEN},
    )
    assert form.status_code == 202 and form.json()["dry_run"] is True

    assert httpx.get(f"{base}/health").json()["queued"] == 2
    while worker.run_one(timeout=0):
        pass
    assert calls == [(5, ["title"], False, False), (9, None, True, False)]


def test_webhook_rejects_bad_requests(running_server):
    base, worker, _ = running_server
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert httpx.post(f"{base}/process", json={"steps": ["title"]}, headers=headers).status_code == 400
    assert httpx.post(f"{base}/process", json={"document_id": 1, "steps": ["nope"]}, headers=headers).status_code == 400
    bad_json = httpx.post(f"{base}/process", content=b"{kaputt", headers={**headers, "Content-Type": "application/json"})
    assert bad_json.status_code == 400 and "JSON" in bad_json.json()["error"]
    assert httpx.post(f"{base}/anders", json={}, headers=headers).status_code == 404
    assert worker.queued() == 0


def test_server_dry_run_cannot_be_overridden_by_webhook(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(server_module, "check_services", lambda *a, **kw: None)
    monkeypatch.setattr(server_module, "process_pipeline", lambda **kw: calls.append(kw["dry_run"]))
    task = server_module.document_task(_settings(tmp_path, dry_run=True), 1, None, dry_run=False, force=False)
    task.run()
    assert calls == [True]


def test_document_task_skips_when_services_down(tmp_path, monkeypatch):
    calls = []

    def down(*a, **kw):
        raise server_module.ServiceError("Docling weg")

    monkeypatch.setattr(server_module, "check_services", down)
    monkeypatch.setattr(server_module, "process_pipeline", lambda **kw: calls.append(kw))
    server_module.document_task(_settings(tmp_path), 1, ["title"], False, False).run()
    assert calls == []


def test_serve_refuses_without_token(tmp_path):
    with pytest.raises(ValueError, match="Webhook-Token"):
        server_module.serve(ServerSettings(webhook_token=""))
