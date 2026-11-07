from __future__ import annotations

import datetime
import hmac
import json
import logging
import queue
import re
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .batch import BatchJob, load_batch_jobs, process_batch
from .errors import ServiceError, log_failure
from .pipeline import process_pipeline
from .preflight import check_services
from .steps import load_pipeline_order, load_step_config

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000
# Nach einem Standby/einer langen Pause werden hoechstens so viele
# verpasste Minuten nachgeholt (und jeder Job dabei hoechstens einmal).
MAX_CATCH_UP_MINUTES = 60
DOC_URL_ID = re.compile(r"/documents/(\d+)(?:/|$)")


@dataclass
class ServerSettings:
    webhook_token: str
    host: str = "0.0.0.0"
    port: int = 8080
    # Global fuer alles, was der Server anstoesst (Webhook und Cron); ein
    # Webhook kann zusaetzlich pro Aufruf dry_run anfordern, aber nie
    # einen Dry-Run-Server zum Schreiben bringen.
    dry_run: bool = False
    batch_config_path: str = "config/batch.yaml"
    paperless_config_path: str = "config/paperless.yaml"
    ai_config_path: str = "config/ai.yaml"
    steps_config_path: str = "config/steps.yaml"
    db_path: str = "data/archivlotse.db"


@dataclass
class Task:
    key: tuple
    description: str
    run: Callable[[], object]


@dataclass
class Worker:
    """Runs tasks strictly one after another in a single background thread
    - webhook documents and scheduled batch jobs alike, so two of them
    never work on Paperless/SQLite at the same time. A task whose key is
    already waiting in the queue (same document + steps, or same batch
    job) isn't queued twice. The queue lives in memory only: whatever is
    still waiting when the server stops is lost - a document from a
    webhook stays without `archivlotse-done`, so a cron'd `todo` job picks
    it up later."""

    tasks: queue.Queue = field(default_factory=queue.Queue)
    pending: set = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)
    current: str | None = None

    def submit(self, task: Task) -> bool:
        with self.lock:
            if task.key in self.pending:
                return False
            self.pending.add(task.key)
        self.tasks.put(task)
        return True

    def queued(self) -> int:
        with self.lock:
            return len(self.pending)

    def run_one(self, timeout: float | None = None) -> bool:
        """Run the next task (waiting up to `timeout`); False if none came."""
        try:
            task = self.tasks.get(timeout=timeout)
        except queue.Empty:
            return False
        with self.lock:
            self.pending.discard(task.key)
            self.current = task.description
        logger.info("Starte: %s", task.description)
        try:
            task.run()
        except Exception as exc:  # ein Task darf den Worker nie beenden
            log_failure(logger, f"{task.description} fehlgeschlagen", exc)
        else:
            logger.info("Fertig: %s", task.description)
        finally:
            with self.lock:
                self.current = None
            self.tasks.task_done()
        return True

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.run_one(timeout=1.0)


def document_task(
    settings: ServerSettings,
    document_id: int,
    steps: list[str] | None,
    dry_run: bool,
    force: bool,
) -> Task:
    dry_run = dry_run or settings.dry_run

    def run() -> None:
        step_names = steps or load_pipeline_order(settings.steps_config_path)
        try:
            check_services(
                step_names,
                paperless_config_path=settings.paperless_config_path,
                ai_config_path=settings.ai_config_path,
                steps_config_path=settings.steps_config_path,
            )
        except ServiceError as exc:
            logger.error("Dokument %s nicht verarbeitet: %s", document_id, exc)
            return
        process_pipeline(
            document_id=document_id,
            dry_run=dry_run,
            force=force,
            step_names=steps,
            paperless_config_path=settings.paperless_config_path,
            ai_config_path=settings.ai_config_path,
            steps_config_path=settings.steps_config_path,
            db_path=settings.db_path,
        )

    label = ", ".join(steps) if steps else "ganze pipeline"
    flags = "".join([" [dry-run]" if dry_run else "", " [force]" if force else ""])
    return Task(
        key=("document", document_id, tuple(steps or ()), dry_run, force),
        description=f"Webhook: Dokument {document_id} ({label}){flags}",
        run=run,
    )


def batch_task(settings: ServerSettings, job_name: str) -> Task:
    def run() -> None:
        process_batch(
            job_names=[job_name],
            dry_run=settings.dry_run,
            batch_config_path=settings.batch_config_path,
            paperless_config_path=settings.paperless_config_path,
            ai_config_path=settings.ai_config_path,
            steps_config_path=settings.steps_config_path,
            db_path=settings.db_path,
        )

    return Task(
        key=("batch", job_name),
        description=f"Cron: Batch-Job {job_name!r}" + (" [dry-run]" if settings.dry_run else ""),
        run=run,
    )


class Scheduler:
    """Queues every batch job with a `cron:` whose schedule matches the
    current minute (local time). The schedules are read once at server
    start - restart the server after changing `cron:` in batch.yaml (the
    jobs' filters/steps are re-read on every run anyway)."""

    def __init__(self, settings: ServerSettings, worker: Worker, jobs: list[BatchJob]):
        self.settings = settings
        self.worker = worker
        self.jobs = [job for job in jobs if job.cron is not None]
        self.last_minute: datetime.datetime | None = None

    def tick(self, now: datetime.datetime) -> list[str]:
        """Queue the jobs due in any minute since the last tick, up to and
        including `now`'s minute. Returns the names of the jobs queued."""
        minute = now.replace(second=0, microsecond=0)
        if self.last_minute is None:
            minutes = [minute]
        else:
            start = max(self.last_minute, minute - datetime.timedelta(minutes=MAX_CATCH_UP_MINUTES))
            count = int((minute - start).total_seconds() // 60)
            minutes = [start + datetime.timedelta(minutes=i) for i in range(1, count + 1)]
        if minutes:
            self.last_minute = minute
        queued: list[str] = []
        for job in self.jobs:
            if not any(job.cron.matches(moment) for moment in minutes):
                continue
            if self.worker.submit(batch_task(self.settings, job.name)):
                queued.append(job.name)
            else:
                logger.info("Cron: Batch-Job %r wartet noch vom letzten Mal - nicht doppelt eingereiht", job.name)
        return queued

    def run_forever(self, stop: threading.Event) -> None:
        if not self.jobs:
            logger.info("Keine Batch-Jobs mit 'cron:' - Zeitplan ist leer")
            return
        for job in self.jobs:
            logger.info("Cron: Batch-Job %r nach Zeitplan %r", job.name, job.cron.expression)
        while not stop.is_set():
            self.tick(datetime.datetime.now())
            now = datetime.datetime.now()
            stop.wait(60 - now.second - now.microsecond / 1_000_000 + 0.5)


class WebhookError(ValueError):
    """Invalid webhook request - answered with HTTP 400."""


def _first(params: dict, *names: str):
    for name in names:
        value = params.get(name)
        if isinstance(value, list):
            value = value[0] if value else None
        if value not in (None, ""):
            return value
    return None


def _as_bool(value, name: str) -> bool:
    if value is None or isinstance(value, bool):
        return bool(value)
    text = str(value).strip().casefold()
    if text in ("1", "true", "yes", "ja", "on"):
        return True
    if text in ("0", "false", "no", "nein", "off", ""):
        return False
    raise WebhookError(f"{name}: kein Wahrheitswert: {value!r}")


def parse_webhook_request(params: dict, steps_config_path: str) -> tuple[int, list[str] | None, bool, bool]:
    """Extract (document_id, steps, dry_run, force) from the merged query
    string/body parameters.

    The document comes from `document_id`/`doc_id`/`id`, or is parsed out
    of `doc_url` (`.../documents/123/...`) - that's what Paperless's
    workflow webhook placeholders offer. Steps come from `steps` (list or
    comma-separated) or `step`; none means the whole `pipeline:`. Every
    step name is checked against steps.yaml before anything is queued."""
    raw_id = _first(params, "document_id", "doc_id", "id")
    if raw_id is None:
        doc_url = _first(params, "doc_url")
        match = DOC_URL_ID.search(str(doc_url)) if doc_url else None
        if match is None:
            raise WebhookError("document_id (oder doc_id / doc_url) fehlt")
        raw_id = match.group(1)
    try:
        document_id = int(str(raw_id).strip())
    except ValueError as exc:
        raise WebhookError(f"document_id ist keine Zahl: {raw_id!r}") from exc
    if document_id <= 0:
        raise WebhookError(f"document_id muss positiv sein: {document_id}")

    raw_steps = params.get("steps")
    if raw_steps in (None, "", []):
        raw_steps = params.get("step")
    if isinstance(raw_steps, str):
        raw_steps = [raw_steps]
    steps: list[str] = []
    for entry in raw_steps or []:
        steps.extend(part.strip() for part in str(entry).split(",") if part.strip())
    for step_name in steps:
        try:
            load_step_config(step_name, steps_config_path)
        except ValueError as exc:
            raise WebhookError(str(exc)) from exc

    dry_run = _as_bool(_first(params, "dry_run", "dry-run"), "dry_run")
    force = _as_bool(_first(params, "force"), "force")
    return document_id, steps or None, dry_run, force


def _read_params(handler: BaseHTTPRequestHandler) -> dict:
    url = urlparse(handler.path)
    params: dict = {key: values for key, values in parse_qs(url.query).items()}
    length = int(handler.headers.get("Content-Length") or 0)
    if length > MAX_BODY_BYTES:
        raise WebhookError("Request-Body zu groß")
    body = handler.rfile.read(length) if length else b""
    if not body.strip():
        return params
    content_type = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    # Nach Inhalt statt nur Content-Type: "curl -d '{...}'" schickt JSON
    # als Formular-Daten deklariert.
    if content_type == "application/x-www-form-urlencoded" and not body.lstrip().startswith(b"{"):
        params.update(parse_qs(body.decode("utf-8")))
        return params
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise WebhookError(f"Body ist weder JSON noch Formular-Daten: {exc}") from exc
    if not isinstance(data, dict):
        raise WebhookError("JSON-Body muss ein Objekt sein")
    params.update(data)
    return params


def make_handler(settings: ServerSettings, worker: Worker) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ArchivLotse"

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            token = self.headers.get("X-Archivlotse-Token") or ""
            auth = self.headers.get("Authorization") or ""
            if not token and auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            return bool(token) and hmac.compare_digest(
                token.encode("utf-8"), settings.webhook_token.encode("utf-8")
            )

        def do_GET(self) -> None:
            if urlparse(self.path).path != "/health":
                self._send(404, {"error": "unbekannter Pfad"})
                return
            self._send(200, {"status": "ok", "queued": worker.queued(), "running": worker.current})

        def do_POST(self) -> None:
            if urlparse(self.path).path not in ("/process", "/webhook"):
                self._send(404, {"error": "unbekannter Pfad"})
                return
            if not self._authorized():
                logger.warning("Webhook von %s abgelehnt: fehlender/falscher Token", self.client_address[0])
                self._send(401, {"error": "fehlender oder falscher Token"})
                return
            try:
                params = _read_params(self)
                document_id, steps, dry_run, force = parse_webhook_request(
                    params, settings.steps_config_path
                )
            except WebhookError as exc:
                logger.warning("Webhook abgelehnt: %s", exc)
                self._send(400, {"error": str(exc)})
                return
            task = document_task(settings, document_id, steps, dry_run, force)
            queued = worker.submit(task)
            logger.info("%s %s", task.description, "eingereiht" if queued else "wartet bereits")
            self._send(
                202,
                {
                    "document_id": document_id,
                    "steps": steps or "pipeline",
                    "dry_run": dry_run or settings.dry_run,
                    "force": force,
                    "queued": queued,
                },
            )

        def log_message(self, format: str, *args) -> None:
            logger.debug("HTTP %s: " + format, self.client_address[0], *args)

    return Handler


def serve(settings: ServerSettings) -> None:
    """Run `archivlotse serve` until SIGTERM/SIGINT: an HTTP server for
    webhooks, a scheduler for the batch jobs' `cron:` and one worker that
    processes everything they queue, one at a time."""
    if not settings.webhook_token:
        raise ValueError(
            "Kein Webhook-Token gesetzt (Umgebungsvariable ARCHIVLOTSE_WEBHOOK_TOKEN oder "
            "--webhook-token) - ohne Token startet der Server nicht"
        )
    jobs = load_batch_jobs(settings.batch_config_path)
    # Config-Fehler (unbekannte Schritte) sofort melden, nicht erst beim
    # ersten Webhook/Cron-Lauf.
    step_names: dict[str, None] = dict.fromkeys(load_pipeline_order(settings.steps_config_path))
    for job in jobs:
        for step_name in job.steps or []:
            load_step_config(step_name, settings.steps_config_path)
            step_names[step_name] = None
    try:
        check_services(
            list(step_names),
            paperless_config_path=settings.paperless_config_path,
            ai_config_path=settings.ai_config_path,
            steps_config_path=settings.steps_config_path,
        )
    except ServiceError as exc:
        # Kein Abbruch: jeder Webhook/Cron-Lauf prueft selbst noch einmal,
        # ein spaeter gestarteter Dienst wird dann einfach mitbenutzt.
        logger.warning("%s - Server startet trotzdem, jeder Lauf prüft erneut", exc)

    stop = threading.Event()
    worker = Worker()
    scheduler = Scheduler(settings, worker, jobs)
    httpd = ThreadingHTTPServer((settings.host, settings.port), make_handler(settings, worker))
    httpd.daemon_threads = True

    def request_stop(signum, frame) -> None:
        logger.info("Signal %s empfangen - Server wird beendet", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    threads = [
        threading.Thread(target=httpd.serve_forever, name="http", daemon=True),
        threading.Thread(target=worker.run_forever, args=(stop,), name="worker", daemon=True),
        threading.Thread(target=scheduler.run_forever, args=(stop,), name="scheduler", daemon=True),
    ]
    for thread in threads:
        thread.start()
    logger.info(
        "ArchivLotse-Server läuft auf %s:%d%s (POST /process, GET /health)",
        settings.host,
        settings.port,
        " [dry-run]" if settings.dry_run else "",
    )
    try:
        while not stop.wait(1.0):
            pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        if worker.current:
            logger.warning("Abbruch während: %s", worker.current)
