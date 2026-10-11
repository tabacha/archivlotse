# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

The project concept lives in `doc/PROJEKTIDEE.md` (German). Implementation has started with a single vertical slice: Schritt 2/3 "Titel erzeugen" (compute a document title from Paperless OCR text and write it back). Most of the pipeline described below (OCR improvement, classification, custom fields, validation layer, queueing) does not exist yet — only `archivlotse title <id>` works so far. Treat the rest of this file as the target architecture, not current state, and update this section as more steps land.

## Commands

```bash
# Install
pip install -e ".[dev]"

# Run tests
pytest

# Run a single test
pytest tests/test_title.py::test_process_title_is_idempotent_and_audits_only_on_change

# Compute/update a title for a Paperless document (needs config/paperless.yaml + config/ai.yaml)
python -m archivlotse.cli title 6023 --dry-run
python -m archivlotse.cli title 6023
```

Local config: copy `config/paperless.example.yaml` → `config/paperless.yaml` and `config/ai.example.yaml` → `config/ai.yaml` (both gitignored; secrets can also come from env vars referenced as `${VAR_NAME}`, expanded via `os.path.expandvars` before YAML parsing). SQLite state defaults to `data/archivlotse.db` (gitignored, created on first run).

## Implemented so far (`archivlotse/title.py`)

- Fetches a document from Paperless (`archivlotse/paperless.py`, `GET`/`PATCH /api/documents/{id}/`, `Authorization: Token ...`).
- Renders `prompts/title.j2` with the (truncated to `MAX_OCR_CHARS`) OCR text and calls the configured AI provider (`archivlotse/ai/`: `OllamaClient` for Ollama's `/api/generate`, `OpenAICompatibleClient` for Scaleway/other OpenAI-compatible chat-completions APIs — selected via `AIConfig.provider` in `config/ai.yaml`).
- **Idempotency:** before calling the AI, computes a SHA-256 fingerprint over (truncated OCR text, prompt template content, provider, model) and checks it against the `title_results` SQLite table; a hit reuses the stored title and skips the AI call entirely. Any change to the OCR text, the prompt file, or the configured provider/model invalidates the cache and forces recomputation.
- **Audit log:** the separate `audit_log` SQLite table only ever gets a row when a title is actually written to Paperless (old title, new title, document ID, timestamp) — never on cache hits, no-op (title unchanged), or `--dry-run`.
- `--dry-run` runs the full pipeline (including the AI call and `title_results` caching) but skips the Paperless `PATCH` and the audit log entry.

When adding the next pipeline step, follow this same shape: one `.j2` prompt, one cache table keyed by an input fingerprint (text + prompt version + provider + model), and audit-log writes gated strictly on an actual Paperless field change.

## What ArchivLotse is

ArchivLotse is a configuration-driven AI assistant that post-processes documents in Paperless-ngx: improving classification, generating meaningful titles, and extracting metadata — without sending every available piece of information to a large language model for every document. Paperless-ngx remains the single source of truth; ArchivLotse never replaces it or duplicates the document archive.

Guiding principle: **as little AI as necessary, as much traceability as possible.** Use deterministic rule-based logic wherever rules are sufficient; use language models only where document content must be interpreted. The AI proposes, the software validates, Paperless stays authoritative.

## Core architecture

**Separation of code and business rules.** Python provides execution logic, API clients, and validation. All domain-specific behavior lives in two kinds of config, not in Python:

- **YAML files** define: available AI providers/models, pipeline steps and their order/conditions, document-type descriptions and selection criteria, tag meanings, correspondent name variants/matching rules, document-type-dependent custom fields, document-date determination rules, token/cost budgets and escalation rules, and validation/approval conditions. Existing tags, document types, correspondents, and custom fields are loaded from the Paperless API by ID — the YAML only adds business-level descriptions/rules on top, never duplicates IDs.
- **Jinja2 templates** (one per AI task, versioned): `classify.j2`, `title.j2`, `document_date.j2`, `correspondent.j2`, `tags.j2`, `bank_statement.j2`, `invoice.j2`, `custom_fields.j2`, etc. Each prompt receives only the document information, rules, and candidates relevant to that specific task — not the full document/metadata set.

**Multi-stage processing pipeline** (independently runnable steps), chosen per document rather than always run in full:

1. **Document check & OCR** — detect whether usable text/embedded text already exists, whether the PDF is scanned, page count, and signs of bad OCR. Docling/RapidOCR is invoked only when it would actually help. The original file is never modified; improved OCR text is written back to Paperless only after a quality check.
2. **Coarse classification** — a small, cheap model assigns a broad category (invoice, bank/credit-card statement, contract, government letter, certificate, insurance, general correspondence, unknown). This determines which further analyses run, and need not map 1:1 to a final Paperless document type.
3. **Targeted metadata extraction** — depends on the category from step 2 (e.g. bank statements get bank/correspondent/period/IBAN extraction; invoices get issuer/invoice number/date/amount/currency). Steps irrelevant to the detected type are skipped entirely.
4. **Validation & approval** — proposed tags/document types must actually exist; IBAN checksum validation; preserve leading zeros in account numbers/IDs; plausibility-check dates and amounts; verify a value is actually evidenced by the OCR text; detect contradictory/ambiguous results; never overwrite existing correct metadata. Uncertain proposals are flagged for manual review rather than auto-applied.

**Candidate pre-selection instead of bulk context.** For large metadata sets (e.g. 500+ correspondents), ArchivLotse does local candidate search first (exact match, known abbreviations/spelling variants, RapidFuzz fuzzy matching, known email domains/addresses) and only sends the LLM a short candidate list when the match is ambiguous — never the full list. Same principle for tags and document types. New tags/document types/correspondents are never created without authorization.

**Long-document handling** is a deliberate three-tier strategy, not "always send full text": short documents get full-text analysis; medium documents get section selection by headings/keywords/structure; long documents are first searched for relevant passages (stored with page/position references) which are only passed to the LLM on demand. This matters especially for document-date determination, since the governing date may appear anywhere in the document (e.g. next to a signature or in an addendum) — the pipeline must not assume the first page is sufficient.

**Pluggable AI providers via logical model roles**, not hardcoded provider calls. Config defines roles like `fast` (cheap classification/titles), `precise` (complex extraction/date decisions), `fallback` (second pass on unclear results). The pipeline picks a role based on document type, text length, task complexity, and validation outcome — not a fixed provider. Planned providers: Ollama (local), Scaleway Model-as-a-Service (EU cloud), and other OpenAI-compatible APIs via a common interface. Sensitive documents can be restricted to local-only processing; external providers should receive only the text excerpts/metadata actually needed.

**Audit logging and incremental processing.** Every processing step is logged: document ID, timestamp, pipeline/git version, step, model+config, prompt version, result, validation status, affected Paperless fields, old vs. proposed vs. actually-applied values, errors/retries, token usage, and estimated cost. This audit log is kept separate from n8n/AI-service logs. Each step also has its own status and a fingerprint of its relevant inputs, so that changing one template/rule (e.g. the document-date prompt) does not force re-running unrelated unaffected steps (e.g. OCR, classification) — this keeps reprocessing large archives cheap.

## Intended CLI shape

```bash
archivlotse process 6023 --dry-run
archivlotse process 6023 --step title
archivlotse process 6023 --step document_date
archivlotse process --tag ki-test --limit 10
archivlotse status 6023
archivlotse audit 6023
```

`--dry-run` is a first-class mode: runs all analyses/validations without writing any metadata back to Paperless. Production operation is meant to add a persistent queue, retries, and resumable jobs — this is explicitly Phase 3, not required for a prototype.

## Technical stack (planned)

| Component | Technology |
|---|---|
| Language | Python 3 |
| Configuration | YAML |
| Prompt templates | Jinja2 |
| Structured AI results | Pydantic |
| HTTP APIs | httpx |
| Fuzzy name matching | RapidFuzz |
| Local status/audit DB | SQLite |
| OCR | Docling Serve / RapidOCR |
| AI backends | Ollama + OpenAI-compatible APIs |
| Deployment | Docker Compose |
| Tests | pytest |

Keep dependencies minimal — no separate database server or message broker is intended (SQLite only), and no required web UI (CLI first; API/review UI is a later, optional addition).

## Explicit non-goals

Do not design toward: replacing Paperless-ngx as the DMS, permanently duplicating original documents, building a full separate document archive, creating new tags/document types without authorization, silently overwriting uncertain AI results, becoming a general-purpose agent framework, or requiring a full web application.
