from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import load_yaml


@dataclass
class StepConfig:
    name: str
    source_field: str
    target_field: str
    prompt: str | None = None
    model: str | None = None
    max_input_chars: int = 8000
    strip_quotes: bool = True
    max_output_length: int | None = None
    candidates_endpoint: str | None = None
    # Mehrfachauswahl statt genau eines Werts (z.B. Tags): die KI darf
    # mehrere, kommagetrennte Kandidaten vorschlagen; gueltige Vorschlaege
    # werden zu den vorhandenen Werten HINZUGEFUEGT statt sie zu ersetzen.
    multi: bool = False
    # Kandidaten (per Namen, z.B. Tag-Namen), die ein Mehrfachauswahl-
    # Schritt NIE loeschen darf, unabhaengig davon, was die KI vorschlaegt.
    # Schutz auf Code-/Config-Ebene, nicht nur per Prompt-Anweisung.
    protected_candidates: list[str] = field(default_factory=list)
    # Nur fuer OCR-Qualitaetsschritte (quality_check gesetzt):
    quality_check: str | None = None
    quality_threshold: float = 0.6
    min_chars_for_check: int = 200
    docling_base_url: str | None = None
    docling_force_ocr: bool = True
    # Schritt nur ausfuehren, wenn das genannte Feld (bzw. bei
    # "document_type" dessen aufgeloester Name) dem erwarteten Wert
    # entspricht, z.B. {"document_type": "Kontoauszug"} - oder einem von
    # mehreren erlaubten Werten, z.B. {"document_type": ["Rechnung", "Quittung"]}.
    condition: dict[str, str | list[str]] | None = None
    # Nur fuer Korrespondenten-Schritte (prompt_step2 gesetzt): zweistufige
    # Erkennung - KI (prompt/model) ermittelt einen wahrscheinlichen Namen
    # aus dem Text, RapidFuzz vergleicht ihn deterministisch mit den
    # vorhandenen Kandidaten (candidates_endpoint); nur bei mehrdeutigem
    # Ergebnis wird eine zweite KI-Abfrage (prompt_step2/model_step2) mit
    # den paar aehnlichsten Kandidaten gestellt. Schwellenwerte sind
    # bewusst Config, nicht Code - siehe Kommentar am correspondent-Schritt
    # in config/steps.yaml.
    prompt_step2: str | None = None
    model_step2: str | None = None
    # Score (0-100) UND Abstand zum zweitbesten Treffer, ab dem ein
    # Fuzzy-Treffer ohne zweite KI-Abfrage direkt uebernommen wird.
    fuzzy_assign_min_score: float = 95.0
    fuzzy_assign_min_gap: float = 8.0
    # Score, ab dem ein mehrdeutiger Treffer (Abstand zu klein) ueberhaupt
    # per zweiter KI-Abfrage aufgeloest wird, statt direkt als "kein
    # Treffer" zu gelten.
    fuzzy_review_min_score: float = 80.0
    # Wie viele der aehnlichsten Kandidaten der zweiten KI-Abfrage
    # hoechstens vorgelegt werden.
    fuzzy_candidate_limit: int = 5
    # Nur fuer Tag-Setz-Schritte (add_tags gesetzt): diese (in Paperless
    # bereits vorhandenen) Tags per Name an das Dokument anhaengen - kein
    # KI-Aufruf, keine Quelle noetig. Fehlende Tags werden NICHT angelegt.
    add_tags: list[str] = field(default_factory=list)
    # Innerhalb eines Pipeline-Laufs (process_pipeline) nur ausfuehren, wenn
    # kein frueherer Schritt dieses Laufs fehlgeschlagen ist - z.B. damit
    # ein "erledigt"-Tag nicht nach einem gescheiterten Schritt gesetzt wird.
    requires_previous_success: bool = False
    # Nur fuer Datums-Schritte (date_prompt_small gesetzt): Texte bis
    # date_small_max_chars Zeichen werden einmal komplett mit
    # date_prompt_small analysiert (Antwort: nur ein Datum); laengere
    # zuerst die ersten, bei zu niedrigem Score zusaetzlich die letzten
    # date_part_chars Zeichen mit `prompt` (Antwort: Datum|Score).
    # Ein Datum mit Score >= date_min_score wird uebernommen, ein
    # unsichereres nur, wenn target_field noch leer ist.
    date_prompt_small: str | None = None
    date_small_max_chars: int = 8000
    date_part_chars: int = 6000
    date_min_score: int = 85
    # Nur fuer Zusatzfeld-Schritte (custom_field gesetzt): Name eines in
    # Paperless bereits vorhandenen Zusatzfelds (Custom Field), das mit der
    # (je nach Datentyp des Felds validierten) KI-Antwort beschrieben wird.
    # Fehlende Zusatzfelder werden NICHT angelegt. target_field ist dann
    # immer "custom_fields".
    custom_field: str | None = None
    # Zusatzfeld-Schritte: ein bereits gesetzter Wert wird nur
    # ueberschrieben, wenn das ausdruecklich erlaubt ist (sonst wird der
    # Schritt vor dem KI-Aufruf uebersprungen).
    overwrite_existing: bool = False


def load_step_config(name: str, path: str | Path = "config/steps.yaml") -> StepConfig:
    """Load the pipeline definition for one processing step.

    Each step in config/steps.yaml fully describes one field-to-field AI
    call: which Paperless field to read (source_field), which Jinja2
    prompt and AI role (from config/ai.yaml) to use, simple pre-/post-
    processing rules (input truncation, output cleanup), and which
    Paperless field to write the result to (target_field). The Python
    pipeline code itself has no knowledge of what "title" or any other
    specific field/step means - that all lives here.

    If candidates_endpoint is set (e.g. "document_types"), the step picks
    one value out of an existing Paperless list instead of generating
    free text: the prompt is given the current candidate names, and the
    AI's answer is validated against that list before anything is written
    back - an answer that doesn't match an existing candidate is rejected
    rather than applied.

    If quality_check is set (e.g. "heuristic"), this is instead an OCR
    quality-check/improve step: no AI model/prompt is used at all. See
    archivlotse/ocr_quality.py and archivlotse/docling.py.

    If condition is set, the step is skipped entirely (no AI/Docling
    call) unless the document's current field value(s) match.

    protected_candidates (multi-choice steps only) lists candidate names
    that may never be removed, regardless of what the AI suggests - an
    enforced, config-level guarantee rather than relying on the prompt
    text alone.

    If add_tags is set, this is an AI-free tag-adding step: the named,
    already existing Paperless tags are added to target_field (which must
    be "tags"); no source_field/prompt/model needed. A named tag that
    doesn't exist in Paperless is an error, never auto-created.

    If custom_field is set, this is a custom-field step: like a text step,
    but the (cleaned-up) AI answer is validated against the named Paperless
    custom field's data type (e.g. a number for "float") and written into
    the document's custom_fields list; target_field defaults to (and must
    be) "custom_fields". A custom field missing from Paperless is an
    error, never auto-created. An already set value is left alone unless
    overwrite_existing is true.

    requires_previous_success makes process_pipeline skip the step if an
    earlier step in the same run raised (has no effect with --step).

    If prompt_step2 is set, this is a two-stage correspondent-style step:
    the primary prompt/model extracts a probable name from the text,
    RapidFuzz compares it deterministically against candidates_endpoint's
    list, and only an ambiguous fuzzy result (see fuzzy_*_min_* thresholds)
    triggers a second AI call (prompt_step2/model_step2) with the
    fuzzy_candidate_limit closest candidates to disambiguate. The decision
    rules are documented next to the thresholds in config/steps.yaml.
    """
    data = load_yaml(path)
    steps = data.get("steps", {})
    try:
        step = steps[name]
    except KeyError as exc:
        available = ", ".join(sorted(steps)) or "(keine)"
        raise ValueError(
            f"Unbekannter Verarbeitungsschritt {name!r} in {path}. "
            f"Verfügbare Schritte: {available}"
        ) from exc

    preprocessing = step.get("preprocessing", {}) or {}
    postprocessing = step.get("postprocessing", {}) or {}
    docling = step.get("docling", {}) or {}
    date = step.get("date", {}) or {}
    quality_check = step.get("quality_check")
    custom_field = step.get("custom_field")
    target_field = step.get("target_field") or ("custom_fields" if custom_field else None)
    if not target_field:
        raise ValueError(f"Schritt {name!r}: 'target_field' ist erforderlich")

    config = StepConfig(
        name=name,
        source_field=step.get("source_field", ""),
        target_field=target_field,
        prompt=step.get("prompt"),
        model=step.get("model"),
        max_input_chars=preprocessing.get("max_input_chars", 8000),
        strip_quotes=postprocessing.get("strip_quotes", True),
        max_output_length=postprocessing.get("max_length"),
        candidates_endpoint=step.get("candidates_endpoint"),
        multi=step.get("multi", False),
        protected_candidates=list(step.get("protected_candidates") or []),
        quality_check=quality_check,
        quality_threshold=step.get("quality_threshold", 0.6),
        min_chars_for_check=step.get("min_chars_for_check", 200),
        docling_base_url=docling.get("base_url"),
        docling_force_ocr=docling.get("force_ocr", True),
        condition=step.get("condition"),
        prompt_step2=step.get("prompt_step2"),
        model_step2=step.get("model_step2"),
        fuzzy_assign_min_score=step.get("fuzzy_assign_min_score", 95.0),
        fuzzy_assign_min_gap=step.get("fuzzy_assign_min_gap", 8.0),
        fuzzy_review_min_score=step.get("fuzzy_review_min_score", 80.0),
        fuzzy_candidate_limit=step.get("fuzzy_candidate_limit", 5),
        add_tags=list(step.get("add_tags") or []),
        requires_previous_success=step.get("requires_previous_success", False),
        date_prompt_small=date.get("prompt_small"),
        date_small_max_chars=date.get("small_max_chars", 8000),
        date_part_chars=date.get("part_chars", 6000),
        date_min_score=date.get("min_score", 85),
        custom_field=custom_field,
        overwrite_existing=step.get("overwrite_existing", False),
    )

    if config.add_tags:
        if config.target_field != "tags":
            raise ValueError(f"Schritt {name!r}: 'add_tags' benötigt target_field: tags")
        return config
    if not config.source_field:
        raise ValueError(f"Schritt {name!r}: 'source_field' ist erforderlich")

    if quality_check:
        if quality_check not in ("heuristic", "ai_judge"):
            raise ValueError(
                f"Schritt {name!r}: unbekannter quality_check {quality_check!r} "
                "(erwartet 'heuristic' oder 'ai_judge')"
            )
        if quality_check == "ai_judge" and (not config.prompt or not config.model):
            raise ValueError(
                f"Schritt {name!r}: quality_check 'ai_judge' benötigt 'prompt' und 'model'"
            )
        if quality_check == "heuristic" and not config.docling_base_url:
            raise ValueError(f"Schritt {name!r}: quality_check benötigt 'docling.base_url'")
    elif not config.prompt or not config.model:
        raise ValueError(
            f"Schritt {name!r}: 'prompt' und 'model' sind erforderlich (außer bei "
            "quality_check- und add_tags-Schritten)"
        )

    if config.custom_field and config.target_field != "custom_fields":
        raise ValueError(
            f"Schritt {name!r}: 'custom_field' benötigt target_field: custom_fields "
            "(oder target_field weglassen)"
        )

    if config.prompt_step2 and not config.candidates_endpoint:
        raise ValueError(
            f"Schritt {name!r}: 'prompt_step2' (zweistufige Korrespondenten-Erkennung) "
            "benötigt zusätzlich 'candidates_endpoint'"
        )
    if config.prompt_step2 and not config.model_step2:
        raise ValueError(
            f"Schritt {name!r}: 'prompt_step2' benötigt zusätzlich 'model_step2'"
        )

    return config


def load_pipeline_order(path: str | Path = "config/steps.yaml") -> list[str]:
    """Load the ordered list of step names under `pipeline:` in
    config/steps.yaml - the order `archivlotse process <id>` (without
    --step) runs them in."""
    data = load_yaml(path)
    pipeline = data.get("pipeline")
    if not pipeline:
        raise ValueError(
            f"Keine 'pipeline:'-Liste in {path} definiert - für 'archivlotse process <id>' "
            "ohne --step wird eine Schrittreihenfolge benötigt."
        )
    return list(pipeline)
