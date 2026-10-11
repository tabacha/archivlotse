# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

The project concept lives in `doc/PROJEKTIDEE.md` (German). Implementation is a generic, config-driven pipeline engine (`archivlotse/pipeline.py: process_step`) with five step shapes:

- **Text steps** (e.g. `title`): read one Paperless field, run it through a prompt + AI role, write the cleaned-up free-text result to another Paperless field.
- **Choice steps** (e.g. `document_type`): same, but the AI must pick one value out of an existing Paperless list (fetched live via the Paperless API), and a suggestion that doesn't match an existing candidate is rejected rather than written. This is the first bit of the concept doc's "validate before writing" / "never invent new metadata objects" principle actually implemented.
- **Multi-choice steps** (e.g. `tags`, `candidates_endpoint` + `multi: true`): like a choice step, but for a many-to-many Paperless field (a list of ids, not a scalar FK). The AI may suggest several comma-separated candidates; each is validated independently (an invalid one is dropped with a warning, not fatal to the whole step), and the valid ones are **added** to the field's current values — existing values (e.g. manually-set tags) are never removed.
- **OCR quality step** (`ocr_check`): no AI role/prompt at all — a heuristic (`archivlotse/ocr_quality.py`) estimates whether the existing `content` is usable; if not, the original file is downloaded from Paperless and re-OCR'd via Docling Serve (`archivlotse/docling.py`), with the result written back only if it passes a basic sanity check. This is the first bit of the concept doc's "Schritt 1: Dokumentprüfung und OCR".
- **Correspondent step** (`correspondent`, step has `prompt_step2` set): two-stage recognition against Paperless's ~1000 correspondents, see `TODO_paperless_korrespondeten_pipeline.md` for the full design and `config/prompts/correspondent-step1.j2`/`correspondent-step2.j2`. Step1 (AI) extracts a probable name from the text; RapidFuzz then scores it deterministically against every existing correspondent (no AI); a confident, clearly-leading fuzzy match is assigned directly, an ambiguous one (several similarly-plausible candidates) triggers step2 (AI, given only the closest few candidates) to disambiguate using the full text, and a weak match is left alone. Not yet in `pipeline:` (needs threshold calibration against real documents first). A correspondent name step2 proposes that matches none of its candidates is logged as a new-correspondent proposal and never auto-created.

All shapes are driven by `config/steps.yaml`, with no step-specific Python code for ordinary cases. A `pipeline:` list in the same file defines the step order; `archivlotse process <id>` (no `--step`) runs them all via `process_pipeline`, with a failing step logged and skipped rather than aborting the rest. Text/choice/multi-choice/correspondent steps also support an optional `condition:` (skip the step unless the document's current field — currently only `document_type`, resolved by name — matches), and every prompt render additionally receives `{{ document_type_name }}`, so e.g. `title.j2` can branch its instructions by document type. Candidate lists (document types, tags, correspondents) are cached in SQLite for `CANDIDATE_CACHE_TTL_SECONDS` (2h) across separate CLI invocations, via `_get_candidates()` — `CANDIDATE_FETCHERS` entries should always be called through that, never directly, or caching is silently bypassed.

Currently defined steps/roles: `ocr_check` (heuristic, Docling at `http://127.0.0.1:5001`), `document_type` (OCR text → document type, Scaleway `mistral-small-3.2-24b-instruct-2506`), `tags` (OCR text → tags, same Scaleway model), `correspondent` (OCR text → correspondent, two-stage: Scaleway `mistral-small-3.2-24b-instruct-2506` for step1 + RapidFuzz, Scaleway `llama-3.3-70b-instruct` for step2 — defined but not yet in `pipeline:`, pending threshold calibration), `title` (OCR text → title, Ollama, document-type-aware). Most of the rest of the concept doc (custom fields, long-document handling, queueing, `quality_check: ai_judge`) does not exist yet. Treat the rest of this file as the target architecture, not current state, and update this section as more steps/capabilities land.

## Commands

```bash
# Install (editable; also registers the `archivlotse` console script)
pip install -e ".[dev]"

# Run tests
pytest

# Run a single test
pytest tests/test_pipeline.py::test_process_step_is_idempotent_and_audits_only_on_change

# Run one config-defined step for a Paperless document (needs config/paperless.yaml + config/ai.yaml)
archivlotse process 6023 --step title --dry-run
archivlotse process 6023 --step title
archivlotse process 6023 --step title --ai-role precise  # override the role from config/steps.yaml
archivlotse process 6023 --step document_type --dry-run --verbose  # also logs the full prompt + raw AI answer at DEBUG
archivlotse process 6023 --step title --force  # bypass step/candidate caches, recompute even if input unchanged

# Run the whole pipeline (config/steps.yaml: pipeline:) for one document
archivlotse process 6023 --dry-run
archivlotse process 6023

# Batch mode: run for every document tagged "TODO", one after another
# (does NOT add/remove the tag itself - that's a separate decision)
archivlotse process --tag TODO --dry-run
archivlotse process --tag TODO --step document_type --limit 10

# Wipe all cached step results + candidate lists (NOT the audit log)
archivlotse clear-cache
```

End users who don't want a dev install can still run the same thing as `python -m archivlotse.cli process ...` from the repo root without `pip install -e .` (only the runtime deps need to be present). See `README.md` for the beginner-oriented setup walkthrough (venv, config, troubleshooting) — keep that in sync with CLI/config changes made here.

Local config: copy `config/paperless.example.yaml` → `config/paperless.yaml` and `config/ai.example.yaml` → `config/ai.yaml` (both gitignored — they hold secrets; values can reference env vars as `${VAR_NAME}`, expanded via `os.path.expandvars` before YAML parsing). `config/steps.yaml` and `config/prompts/*.j2` hold no secrets and are committed as-is — deliberately kept OUTSIDE the `archivlotse` Python package and resolved relative to the current working directory (default `"config/prompts"`, see `archivlotse/prompts.py`), same as the other config files, so prompts/step rules can be edited in a deployment (e.g. a mounted volume in Docker) without rebuilding/reinstalling the package. SQLite state defaults to `data/archivlotse.db` (gitignored, created on first run; `archivlotse/db.py` auto-migrates an older schema in place on open, so existing local audit history survives schema changes).

## Implemented so far (`archivlotse/pipeline.py`)

Config layering, in the order it's read:

1. **`config/steps.yaml`** (`archivlotse/steps.py`, committed, no secrets) — a top-level `pipeline:` (ordered step names) plus `steps:` entries, each with: `source_field`/`target_field` (Paperless fields read/written), `condition` (optional skip rule), and either (a) `prompt` + `ai_role` + pre-/post-processing (`preprocessing.max_input_chars`, `postprocessing.strip_quotes`/`max_length`) for text/choice/multi-choice steps, optionally with `candidates_endpoint` (makes it a choice step; the prompt additionally receives `{{ candidate_names }}`) and `multi: true` (makes it a multi-choice step, for a many-to-many field like `tags`), (b) `quality_check` + `quality_threshold` + `min_chars_for_check` + `docling: {base_url, force_ocr}` for the OCR step, or (c) `prompt_step2` + `ai_role_step2` + `fuzzy_assign_min_score`/`fuzzy_assign_min_gap`/`fuzzy_review_min_score`/`fuzzy_candidate_limit` for the correspondent step. `load_step_config` validates which fields are required per shape. `archivlotse/pipeline.py` has no hardcoded knowledge of "title", "document_type", "tags", or any other field/step — it's all read from here.
2. **`config/ai.yaml`** (`archivlotse/config.py: load_ai_config`/`load_ai_roles`, gitignored) — two sections: `providers:` (connection details once per backend — for `scaleway`, `access_key_id` + `secret_key`, matching the labels in Scaleway's IAM console; only `secret_key` is actually used, as the Bearer token for the Generative APIs, `access_key_id` is carried through for reference only) and `roles:` (logical task name → `provider` + `model`). Multiple roles can share one provider/credentials. `--ai-role` on the CLI overrides the role named in `steps.yaml` (single-step mode only).
3. **`config/paperless.yaml`** (gitignored) — Paperless `url` + `token`.

Processing (`process_step` in `archivlotse/pipeline.py`) dispatches on `step_config.quality_check`, then `step_config.candidates_endpoint and step_config.multi`, then `step_config.candidates_endpoint`:

- **Text steps** (`_process_text_step`): fetch the document, resolve `document_type_name` (`_resolve_document_type_name`, via the cached `_get_candidates(conn, paperless, "document_types")`), skip if `_condition_met()` fails, render the prompt (`input_text` + `document_type_name`), call the AI, clean up the raw output (`_postprocess`), write it verbatim to `target_field`.
- **Choice steps** (`_process_choice_step`): same condition/document-type-name handling (reusing the already-fetched candidates list when the step itself *is* `document_types`, to avoid a duplicate lookup), then fetches candidates via `_get_candidates(conn, paperless, step_config.candidates_endpoint)` (cached; underlying fetchers in `CANDIDATE_FETCHERS`: `"document_types"` → `list_document_types()`, `"tags"` → `list_tags()`, both paginated). The prompt gets the candidate names; a cleaned-up AI answer is matched case/whitespace-insensitively against them. An answer equal to the `UNBEKANNT` sentinel, or one that matches no candidate, is **not** written to Paperless (logged as info/warning respectively, no audit entry). A match writes the candidate's **id** to Paperless (`target_field` expects an FK id, not a name) but logs/audits the human-readable name.
- **Multi-choice steps** (`_process_multi_choice_step`): same condition/document-type-name/candidate-fetch handling, plus the prompt also gets `{{ current_tag_names }}` (the field's current values, by name) so the AI can reconsider them, not just add new ones. The (line-preserving) cleaned answer (`_clean_multiline`, NOT `_postprocess` — that collapses newlines, which would merge the two labeled lines into one) is parsed by `_parse_add_delete_lines()` into an add-part and a delete-part from `"Hinzufügen: ..."`/`"Löschen: ..."` lines (falling back to treating the whole answer as the add-list if neither label is found, for backward compat with a plain comma-list answer); each comma-separated entry in both parts is validated independently against the candidates (case/whitespace-insensitive, `NONE_SENTINEL` "KEINE" meaning an empty list) — an unmatched entry is logged as a warning and dropped, not fatal. The final value written is `sorted((set(current_ids) | add_ids) - delete_ids)` — existing values are kept unless explicitly deleted; if a candidate is in both lists, delete wins (logged). Since current tag state is now part of the prompt, it's also part of the fingerprint (`current_tags_signature`) — a step run that itself changes the field (e.g. adds a tag) correctly invalidates the cache for the next run, since the prompt input genuinely changed. The audit log gets comma-joined human-readable names for both old and new (`_apply_value`'s `write_value` is the raw id list, the display `old_value`/`new_value` are the names).
- **OCR step** (`_process_ocr_step`): no condition/document-type-name handling (not needed). Scores the current `source_field` text with `ocr_quality_score()`; if ≥ `quality_threshold`, returns immediately (no download, no Docling call — cheap). Otherwise downloads the original file (`PaperlessClient.download_original`), converts it via `DoclingClient.convert_to_text(..., force_ocr=...)` (`POST /v1/convert/file`, `to_formats=text`), and writes the result **unconditionally accepting Docling's output** as long as it passes `MIN_OCR_LENGTH_RATIO` (not drastically shorter than the original) — it deliberately does **not** compare old vs. new quality scores to decide (see `ocr_quality.py` docstring and the calibration note below for why that comparison is unreliable). `old_value`/`new_value` passed to the audit log are short `"[N Zeichen, Score X.XX]"` summaries, not the full text (`_apply_value`'s `write_value` vs. display-`new_value` split, originally added for choice steps' id/name split, is reused here for raw-text/summary, and again for multi-choice's id-list/name-list split).
- All shapes share `_apply_value()`: compares old vs. new, skips on no-op or `--dry-run`, otherwise calls `update_document_field` (type hint now `int | str | list[int]`) + `append_audit_log`.
- **Idempotency:** before calling the AI/Docling, computes a SHA-256 fingerprint (`_fingerprint(*parts)`, variadic) over the relevant inputs (truncated text, prompt version, provider/model, postprocessing settings, candidate list for choice/multi-choice steps, or Docling config for the OCR step) and checks it against the `step_results` SQLite table (keyed by `step_name` + `document_id` + fingerprint); a hit skips the AI/Docling call entirely.
- **Candidate caching:** `_get_candidates()` checks `candidate_cache` (SQLite: `endpoint` PK, JSON `data`, `fetched_at`) via `db.get_cached_candidates`/`save_candidates` before calling a `CANDIDATE_FETCHERS` entry; a hit within `CANDIDATE_CACHE_TTL_SECONDS` (2h) skips the Paperless call entirely — verified live that a second `archivlotse process` run makes zero `/api/tags/`or `/api/document_types/` requests. To force a refresh sooner, delete rows from `candidate_cache` directly (no CLI flag yet).
- **Audit log:** the separate `audit_log` SQLite table (`document_id`, `field_name`, `old_value`, `new_value`, `changed_at`) only ever gets a row when a field is actually written to Paperless — never on cache hits, no-op, rejected choice suggestions, skipped conditions, a "good enough" OCR check, or `--dry-run`.
- **Cost tracking:** `archivlotse/ai/base.py: AIResult(text, prompt_tokens, completion_tokens)` is what both `OllamaClient.generate()` (from Ollama's `prompt_eval_count`/`eval_count`) and `OpenAICompatibleClient.generate()` (from the OpenAI-style `usage.prompt_tokens`/`usage.completion_tokens`) return now, instead of a bare string. `_estimate_cost(ai_config, prompt_tokens, completion_tokens)` returns `0.0` unconditionally for `provider == "ollama"` (local = free), `None` if usage data or `AIConfig.price_per_1m_input`/`price_per_1m_output` (from the role in `config/ai.yaml`) aren't available, else the computed USD estimate. `_log_usage()` logs it and every non-OCR step passes it to `save_step_result(..., prompt_tokens=, completion_tokens=, estimated_cost_usd=)`, persisted in `step_results` (migrated in place for older DBs via `_migrate_step_results_usage_columns`). Not implemented: aggregating/reporting total cost (e.g. per document or per month) — only per-call, queryable via `SELECT SUM(estimated_cost_usd) FROM step_results`.
- `--dry-run` runs the full step (including any AI/Docling call and `step_results` caching) but skips the Paperless `PATCH` and the audit log entry.
- `--force` (threaded through `process_pipeline`/`process_step` into each `_process_*_step` function) makes every cache lookup (`get_cached_step_result`, `_get_candidates`) a guaranteed miss, forcing a fresh AI/Docling/Paperless call even if the fingerprint is unchanged. It deliberately does **not** bypass business-rule skips (a step's `condition:`, or the OCR step's quality-threshold gate) — those aren't caching. `save_step_result` is an upsert (`INSERT ... ON CONFLICT ... DO UPDATE`) specifically so a `--force` recompute with an unchanged fingerprint overwrites the existing row instead of violating the `UNIQUE (step_name, document_id, fingerprint)` constraint.
- `archivlotse clear-cache` (`archivlotse/db.py: clear_caches`) empties `step_results` and `candidate_cache` entirely, in one shot (no per-step/per-document granularity) — for when Paperless metadata (e.g. tags) was reorganized and stale cached suggestions shouldn't linger until their TTL/fingerprint naturally expires. Never touches `audit_log`.

`process_pipeline` (new) loads `pipeline:` via `load_pipeline_order` and calls `process_step` for each name in order, catching and logging (not re-raising) any exception per step so e.g. an unreachable Docling doesn't block `document_type`/`title`. Each step still does its own fresh `paperless.get_document()`, so a later step sees an earlier step's actual Paperless write — except in `--dry-run`, where nothing is written, so later steps keep seeing pre-pipeline state (documented caveat, not a bug).

`process_batch_by_tag` (`archivlotse process --tag <name> [--limit N] [--step ...]`) resolves the tag name to its Paperless id via the cached `_get_candidates(conn, paperless, "tags")`, fetches every document with that tag (`PaperlessClient.list_documents_by_tag_id`), sorts them by id ascending (smallest first — Paperless's own order isn't id order), then calls `process_pipeline` (or `process_step` if `--step` is given) for each one in turn. Immediately before each document, its tags are re-fetched fresh (`paperless.get_document`) and checked against the tag id — a document that lost the tag since the initial list was fetched (e.g. someone untagged it mid-batch) is skipped, not processed anyway. A document that raises is logged and skipped, same isolation principle as a failing step within one document's pipeline — one bad document must not abort the whole batch. Deliberately does **not** add or remove the tag itself on any document (e.g. a "done, remove TODO" step) — that's a separate decision, not assumed here.

**OCR heuristic calibration note** (see `ocr_quality.py` for the full docstring): tested against real documents, a plain "fraction of alphabetic tokens length≥3" heuristic, a longest-good-run heuristic, and a vowel-presence heuristic all failed to reliably separate genuinely garbled OCR from fine-but-numeric-heavy documents (bank statements) or fine-but-noisy-header documents (barcode/address-block lines). The shipped heuristic uses `wordfreq.zipf_frequency(word, "de") >= 4.0` as the "is this a real word" test, which calibrated reasonably (known-bad document ~0.47, known-good documents 0.74–0.92) — but a **re-OCR'd, jargon/proper-noun-heavy document can still score lower than its own garbled original** (verified live against a vehicle registration certificate), so old-vs-new score comparison is not used for the accept/reject decision, only the initial "is the existing text good enough to skip re-OCR" gate.

Adding the next **text**, **choice**, or **multi-choice** step for `document_types`/`tags`/`correspondents` is pure config: a new `.j2` prompt in `config/prompts/`, a new entry under `steps:` (and `pipeline:`) in `config/steps.yaml`, and a matching role in `config/ai.yaml` if needed — no changes to `pipeline.py`. A choice/multi-choice step against a *different* Paperless list needs one small code change: a new entry in `CANDIDATE_FETCHERS` (and a `PaperlessClient.list_x()` method if one doesn't exist yet). A **custom field** (e.g. `Kontonummer`, discussed but not built) needs real new code: Paperless writes custom fields via a different payload shape (`{"custom_fields": [{"field": id, "value": ...}]}`), not a flat top-level key, so it can't reuse `update_document_field`/`_apply_value` as-is. Steps needing multiple input fields or multi-field output (e.g. future `document_date`, `invoice`/`bank_statement` extraction steps from the concept doc) will need more substantial changes to `pipeline.py`, not just config, following the pattern of the correspondent step (`_process_correspondent_step`), which is itself a fifth, fully bespoke shape rather than config on top of the generic four.

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
archivlotse process 6023 --dry-run          # implemented: whole pipeline
archivlotse process 6023 --step title       # implemented: single step
archivlotse process 6023 --step document_date  # not implemented (no such step yet)
archivlotse process --tag TODO --limit 10      # implemented: batch mode, see process_batch_by_tag
archivlotse status 6023                         # not implemented
archivlotse audit 6023                          # not implemented (query data/archivlotse.db directly for now)
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
| OCR quality heuristic | wordfreq (implemented) |
| Local status/audit DB | SQLite |
| OCR | Docling Serve (implemented) / RapidOCR |
| AI backends | Ollama + OpenAI-compatible APIs |
| Deployment | Docker Compose |
| Tests | pytest |

Keep dependencies minimal — no separate database server or message broker is intended (SQLite only), and no required web UI (CLI first; API/review UI is a later, optional addition).

## Explicit non-goals

Do not design toward: replacing Paperless-ngx as the DMS, permanently duplicating original documents, building a full separate document archive, creating new tags/document types without authorization, silently overwriting uncertain AI results, becoming a general-purpose agent framework, or requiring a full web application.
