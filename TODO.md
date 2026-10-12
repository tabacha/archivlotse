# TODO

Offene Punkte aus der Zielarchitektur (siehe `CLAUDE.md`), die noch nicht
umgesetzt sind. Kein Design-Dokument, sondern eine Sammelstelle, damit
nichts verloren geht.

## Custom Fields (z. B. Kontonummer)

Paperless schreibt Custom Fields über eine andere Payload-Form
(`{"custom_fields": [{"field": <id>, "value": ...}]}`) als normale Felder
(`{"document_type": <id>}`). Das bisherige `update_document_field`/
`_apply_value` geht von einem flachen Top-Level-Key aus und kann dafür
nicht einfach wiederverwendet werden - braucht echten neuen Code, keine
reine Config-Erweiterung.

Ursprünglicher Anlass: Kontonummer nur ermitteln, wenn
`document_type == "Kontoauszug"` - der generische `condition:`-Mechanismus
dafür existiert in `steps.yaml` bereits und müsste nur wiederverwendet
werden.

## `document_date`-Step

Noch kein Konzept umgesetzt. Laut `doc/PROJEKTIDEE.md` kann das
maßgebliche Datum an beliebiger Stelle im Dokument stehen (z. B. neben
einer Unterschrift oder in einem Nachtrag) - die erste Seite reicht nicht
zwingend. Hängt deshalb teilweise am noch nicht gebauten
Long-Document-Handling (dreistufige Strategie: kurze Dokumente komplett,
mittlere per Abschnittsauswahl, lange per Passagensuche).

Braucht außerdem eine Plausibilitätsprüfung (Datum nicht in der Zukunft,
sinnvoller Bereich) nach demselben Prinzip wie die Validierung bei
Auswahl-Schritten (KI schlägt vor, Code prüft).

## `quality_check: ai_judge`

Aktuell nur als Platzhalter in `_process_ocr_step` vorgesehen - wirft
`NotImplementedError`, sobald ein Schritt `quality_check: ai_judge` statt
`heuristic` setzt. Soll die Grenzen der `wordfreq`-Heuristik abfedern
(siehe `ocr_quality.py`-Docstring: zahlenlastige, aber eigentlich gute
Dokumente, oder jargon-/eigennamenlastige Dokumente, die trotz guter
Qualität niedrig scoren). Braucht einen eigenen Prompt + ein Modell,
die den vorhandenen Text bewertet (gut/schlecht), analog zu den anderen
KI-Schritten.

## `archivlotse status`/`archivlotse audit`

Aktuell nur per direkter SQLite-Abfrage einsehbar, z. B.:

```bash
sqlite3 data/archivlotse.db "SELECT * FROM audit_log WHERE document_id = 6023;"
sqlite3 data/archivlotse.db "SELECT * FROM step_results WHERE document_id = 6023;"
```

- `archivlotse status <id>`: letzter Verarbeitungsstand eines Dokuments -
  welche Schritte wann mit welchem Ergebnis/Modell/Kosten liefen
  (`step_results`, gefiltert nach `document_id`).
- `archivlotse audit <id>`: tatsächliche Paperless-Änderungshistorie für
  ein einzelnes Dokument (`audit_log`, gefiltert nach `document_id`) -
  bisher nur global per SQL abfragbar, nicht über die CLI gefiltert.
