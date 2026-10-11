# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

The project concept lives in `doc/PROJEKTIDEE.md` (German). Implementation is a generic, config-driven pipeline engine (`archivlotse/pipeline.py: process_step`) with two step shapes:

- **Text steps** (e.g. `title`): read one Paperless field, run it through a prompt + AI role, write the cleaned-up free-text result to another Paperless field.
- **Choice steps** (e.g. `document_type`): same, but the AI must pick one value out of an existing Paperless list (fetched live via the Paperless API), and a suggestion that doesn't match an existing candidate is rejected rather than written. This is the first bit of the concept doc's "validate before writing" / "never invent new metadata objects" principle actually implemented.

Both shapes are entirely driven by `config/steps.yaml`, with no step-specific Python code for ordinary cases. Currently defined steps: `title` (OCR text → title, Ollama) and `document_type` (OCR text → document type, Scaleway `mistral-small-3.2-24b-instruct-2506`). Most of the pipeline described below (OCR improvement, correspondent/tag matching, custom fields, long-document handling, queueing) does not exist yet. Treat the rest of this file as the target architecture, not current state, and update this section as more steps/capabilities land.

## Commands

```bash
# Install (editable; also registers the `archivlotse` console script)
pip install -e ".[dev]"

# Run tests
pytest

# Run a single test
pytest tests/test_pipeline.py::test_process_step_is_idempotent_and_audits_only_on_change

# Run a config-defined step for a Paperless document (needs config/paperless.yaml + config/ai.yaml)
archivlotse process 6023 --step title --dry-run
archivlotse process 6023 --step title
archivlotse process 6023 --step title --ai-role precise  # override the role from config/steps.yaml
```

End users who don't want a dev install can still run the same thing as `python -m archivlotse.cli process ...` from the repo root without `pip install -e .` (only the three runtime deps need to be present). See `README.md` for the beginner-oriented setup walkthrough (venv, config, troubleshooting) — keep that in sync with CLI/config changes made here.

Local config: copy `config/paperless.example.yaml` → `config/paperless.yaml` and `config/ai.example.yaml` → `config/ai.yaml` (both gitignored — they hold secrets; values can reference env vars as `${VAR_NAME}`, expanded via `os.path.expandvars` before YAML parsing). `config/steps.yaml` holds no secrets and is committed as-is. SQLite state defaults to `data/archivlotse.db` (gitignored, created on first run; `archivlotse/db.py` auto-migrates an older schema in place on open, so existing local audit history survives schema changes).

## Implemented so far (`archivlotse/pipeline.py`)

Config layering, in the order it's read:

1. **`config/steps.yaml`** (`archivlotse/steps.py`, committed, no secrets) — per step: `source_field` (Paperless field read as input), `target_field` (Paperless field written with the result), which `.j2` prompt to render (receives the input as `{{ input_text }}`), which AI *role* to use, pre-/post-processing (`preprocessing.max_input_chars` truncates input before rendering; `postprocessing.strip_quotes`/`max_length` clean up the raw model output), and optionally `candidates_endpoint` (makes it a choice step; the prompt additionally receives `{{ candidate_names }}`). `archivlotse/pipeline.py` has no hardcoded knowledge of "title", "document_type", or any other field/step — it's all read from here.
2. **`config/ai.yaml`** (`archivlotse/config.py: load_ai_config`/`load_ai_roles`, gitignored) — two sections: `providers:` (connection details once per backend — for `scaleway`, `access_key_id` + `secret_key`, matching the labels in Scaleway's IAM console; only `secret_key` is actually used, as the Bearer token for the Generative APIs, `access_key_id` is carried through for reference only) and `roles:` (logical task name → `provider` + `model`). Multiple roles can share one provider/credentials. `--ai-role` on the CLI overrides the role named in `steps.yaml`.
3. **`config/paperless.yaml`** (gitignored) — Paperless `url` + `token`.

Processing (`process_step` in `archivlotse/pipeline.py`) dispatches on `step_config.candidates_endpoint`:

- **Text steps** (`_process_text_step`): fetch the document, render the prompt with the truncated input, call the AI, clean up the raw output (`_postprocess`), write it verbatim to `target_field`.
- **Choice steps** (`_process_choice_step`): same, but first fetches live candidates via `CANDIDATE_FETCHERS[step_config.candidates_endpoint]` (currently only `"document_types"` → `PaperlessClient.list_document_types()`, paginated). The prompt gets the candidate names; a cleaned-up AI answer is matched case/whitespace-insensitively against them. An answer equal to the `UNBEKANNT` sentinel, or one that matches no candidate, is **not** written to Paperless (logged as info/warning respectively, no audit entry) — this is the validation-before-write principle from the concept doc. A match writes the candidate's **id** to Paperless (`target_field` expects an FK id, not a name) but logs/audits the human-readable name.
- Both share: fetching the document and `PaperlessClient` (`GET`/`PATCH /api/documents/{id}/`, `Authorization: Token ...`); the resolved AI backend (`archivlotse/ai/`: `OllamaClient` for Ollama's `/api/generate`, `OpenAICompatibleClient` for Scaleway/other OpenAI-compatible chat-completions APIs); and `_apply_value()`, which compares old vs. new, skips on no-op or `--dry-run`, and otherwise calls `update_document_field` + `append_audit_log`.
- **Idempotency:** before calling the AI, computes a SHA-256 fingerprint over (truncated input text, prompt template content, provider, model, postprocessing settings, and — for choice steps — the sorted candidate names) and checks it against the `step_results` SQLite table (keyed by `step_name` + `document_id` + fingerprint); a hit reuses the stored value/suggestion and skips the AI call entirely. Changing any of those fingerprint inputs (incl. the Paperless document-types list for `document_type`) forces recomputation.
- **Audit log:** the separate `audit_log` SQLite table (`document_id`, `field_name`, `old_value`, `new_value`, `changed_at`) only ever gets a row when a field is actually written to Paperless — never on cache hits, no-op (value unchanged), rejected/unknown choice-step suggestions, or `--dry-run`.
- `--dry-run` runs the full pipeline (including the AI call and `step_results` caching) but skips the Paperless `PATCH` and the audit log entry.

Adding the next **text** step is pure config: a new `.j2` prompt in `archivlotse/prompts/`, a new entry under `steps:` in `config/steps.yaml`, and a matching role in `config/ai.yaml` if needed — no changes to `pipeline.py`. Adding another **choice** step (e.g. correspondent, tags) needs one small code change too: a new entry in `CANDIDATE_FETCHERS` (and, for a Paperless field that isn't a plain FK like `document_type` — e.g. `tags` is a many-to-many — `_process_choice_step`/`_apply_value` would need adjusting, since they currently assume a single scalar id). Steps needing multiple input fields, fuzzy/RapidFuzz candidate pre-selection, or multi-field output (e.g. future `document_date`, `invoice`/`bank_statement` extraction steps from the concept doc) will need more substantial changes to `pipeline.py`, not just config.

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
