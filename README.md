# ArchivLotse – Erste Schritte

Diese Anleitung ist für den Einstieg gedacht: Du brauchst dafür keine
Vorkenntnisse in Python-Paketierung.

Aktueller Stand: ArchivLotse führt generische, in `config/steps.yaml`
definierte Verarbeitungsschritte aus – ein Schritt liest ein Paperless-Feld
(z. B. den OCR-Text), ermittelt daraus einen Wert und schreibt ihn
(optional) in ein anderes Paperless-Feld zurück. Es gibt vier Arten von
Schritten:

- **Freitext-Schritte** wie `title`: die KI erzeugt frei formulierten Text
  (z. B. einen Titel).
- **Auswahl-Schritte** wie `document_type`: die KI muss sich für genau
  einen von mehreren **bereits in Paperless vorhandenen** Werten
  entscheiden (hier: einen Dokumenttyp). Ein Vorschlag, der zu keinem
  vorhandenen Dokumenttyp passt, wird verworfen und **nicht** gespeichert.
- **Mehrfachauswahl-Schritte** wie `tags`: wie ein Auswahl-Schritt, aber
  die KI darf mehrere vorhandene Werte vorschlagen (ein Dokument kann
  mehrere Tags haben). Ungültige Einzelvorschläge werden nur einzeln
  verworfen; gültige werden zu den vorhandenen Werten **hinzugefügt**,
  nie entfernt.
- **OCR-Qualitätsschritt** `ocr_check`: kein KI-Aufruf, sondern eine
  Heuristik schätzt, ob der vorhandene Text brauchbar ist; falls nicht,
  wird das Originaldokument über eine lokale Docling-Serve-Instanz neu
  erkannt.

Mehrere Schritte lassen sich zu einer **Pipeline** verketten (Reihenfolge
in `pipeline:` in `config/steps.yaml`); `archivlotse process <id>` ohne
`--step` führt sie alle der Reihe nach aus, jeder Schritt sieht dabei die
(echten, nicht Dry-Run-) Änderungen der vorherigen Schritte. Kandidatenlisten
(Dokumenttypen, Tags, …) werden 2 Stunden lang zwischengespeichert, damit
nicht jeder Schritt sie erneut von Paperless laden muss.

Weitere Freitext-Schritte lassen sich rein über Config ergänzen (siehe
Abschnitt 3.3) – ohne Python-Code zu ändern; ein weiterer Auswahl- oder
Mehrfachauswahl-Schritt (z. B. für Korrespondenten) braucht zusätzlich
eine kleine Codeänderung (eine neue Zeile in `CANDIDATE_FETCHERS` in
`archivlotse/pipeline.py`). Alles Weitere aus der Projektidee
(`doc/PROJEKTIDEE.md`, z. B. Custom Fields) ist noch nicht gebaut.

## 1. Voraussetzungen

- Python 3.10 oder neuer. Prüfen mit:

  ```bash
  python3 --version
  ```

- Eine laufende Paperless-ngx-Instanz, erreichbar per http(s), mit einem
  API-Token (Paperless-Weboberfläche → Einstellungen/Profil → API-Token
  erzeugen).
- Mindestens ein KI-Backend: Ollama (lokal oder per Netzwerk erreichbar)
  und/oder einen Zugang zu Scaleway Model-as-a-Service (API-Key). Du kannst
  auch beides parallel einrichten und später je nach Aufgabe wählen.
- Für den `ocr_check`-Schritt (optional): eine erreichbare
  [Docling Serve](https://github.com/docling-project/docling-serve)-Instanz.
  Ohne Docling kannst du die anderen Schritte trotzdem normal nutzen –
  `ocr_check` einfach aus `pipeline:` in `config/steps.yaml` entfernen.

## 2. Projektordner vorbereiten

Alle folgenden Befehle führst du im Hauptordner des Projekts aus (dort, wo
auch diese README liegt).

### 2.1 Virtuelle Umgebung anlegen (empfohlen)

Eine "virtuelle Umgebung" ist einfach ein eigener, isolierter Ordner für
die paar Python-Bibliotheken, die wir brauchen – damit du dein normales
Python-System nicht veränderst.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Danach steht am Anfang deiner Kommandozeile `(.venv)` – das zeigt, dass die
Umgebung aktiv ist. Das musst du in jeder neuen Terminal-Sitzung einmal
wiederholen (`source .venv/bin/activate`), bevor du ArchivLotse benutzt.

### 2.2 ArchivLotse installieren

```bash
pip install -e .
```

Das installiert die paar benötigten Bibliotheken (httpx, PyYAML, Jinja2)
und richtet den Befehl `archivlotse` in deiner virtuellen Umgebung ein.
Das `-e` ("editable") bedeutet: es wird nichts kopiert, der Befehl greift
direkt auf den Code in diesem Ordner zu – Änderungen am Code wirken sich
sofort aus, ohne erneute Installation.

Danach steht dir in dieser (aktivierten) virtuellen Umgebung der Befehl
`archivlotse` zur Verfügung:

```bash
archivlotse --help
```

## 3. Konfiguration einrichten

ArchivLotse liest Konfiguration aus `config/`:

- `config/paperless.yaml` und `config/ai.yaml` enthalten Zugangsdaten
  (Token, API-Keys) und werden daher aus Beispieldateien kopiert – die
  echten Dateien sind in Git ignoriert, landen also nicht versehentlich
  in einem Commit.
- `config/steps.yaml` und `config/prompts/*.j2` enthalten **keine**
  Zugangsdaten, nur fachliche Pipeline-Regeln und die eigentlichen
  KI-Prompts – beides ist bereits im Projekt vorhanden und mit Git
  versioniert, lässt sich also anpassen, ohne den Python-Code oder ein
  Docker-Image neu zu bauen.

```bash
cp config/paperless.example.yaml config/paperless.yaml
cp config/ai.example.yaml config/ai.yaml
```

### 3.1 `config/paperless.yaml`

```yaml
url: "https://paperless.example.com"
token: "dein-api-token"
```

- `url`: die Adresse deiner Paperless-Instanz (ohne `/api` am Ende).
- `token`: dein API-Token aus Paperless.

### 3.2 `config/ai.yaml`

Diese Datei hat zwei Abschnitte:

- **`providers:`** – hier trägst du einmal pro KI-Backend die
  Zugangsdaten ein (z. B. ein lokales Ollama und einen Cloud-Dienst wie
  Scaleway). Die Zugangsdaten stehen hier nur **einmal** pro Provider.
- **`models:`** – hier trägst du jedes benutzte Modell (Schlüssel = der
  Modellname, wie ihn der Provider erwartet) mit seinem Provider ein.
  Mehrere Modelle können sich denselben Provider (und damit dieselben
  Zugangsdaten) teilen, ohne sie zu wiederholen. Welcher Schritt welches
  Modell benutzt, steht direkt in `config/steps.yaml` (`model:`).

Beispiel: ein Modell läuft lokal über Ollama, ein anderes in der Cloud
über Scaleway – die Scaleway-Zugangsdaten stehen dabei nur einmal:

```yaml
providers:
  ollama_lokal:
    type: ollama
    base_url: "http://localhost:11434" # oder http(s)://dein-ollama-host:11434

  scaleway:
    type: scaleway
    # WICHTIG: Project-ID gehoert als Pfadsegment in die URL, nicht nur
    # ".../v1" - sonst meldet Scaleway "403 insufficient permissions".
    # Die komplette URL zeigt dir die Scaleway-Konsole als Code-Beispiel
    # direkt pro Modell unter "Generative APIs".
    base_url: "https://api.scaleway.ai/<deine-project-id>/v1"
    # API-Schluessel erzeugen/einsehen: https://console.scaleway.com/iam/api-keys
    # Dort stehen "Access Key" und "Secret Key" genau so beschriftet.
    access_key_id: "SCWxxxxxxxxxxxxxxxxx"
    secret_key: "${SCALEWAY_SECRET_KEY}"

models:
  "qwen3:4b-instruct": # Name des Modells, das in Ollama geladen ist
    provider: ollama_lokal

  mistral-small-3.2-24b-instruct-2506: # guenstig, reicht fuer Klassifizierung
    provider: scaleway
    price_per_1m_input: 0.15
    price_per_1m_output: 0.35

  llama-3.3-70b-instruct:
    provider: scaleway
    price_per_1m_input: 0.90
    price_per_1m_output: 0.90
```

Du brauchst nur die Provider und Modelle zu pflegen, die in
`config/steps.yaml` tatsächlich verwendet werden. Welches Modell ein
Schritt benutzt, lässt sich auch beim Aufruf überschreiben (siehe unten,
`--model`).

Gibt es dasselbe Modell bei zwei Providern, bekommt einer der Einträge
einen eigenen Schlüssel und den echten Modellnamen per `model:`:

```yaml
models:
  mistral-small-lokal:
    provider: ollama_lokal
    model: "mistral-small3.2:24b"
```

**Kosten pro KI-Aufruf:** trägst du bei einem Modell `price_per_1m_input`
und `price_per_1m_output` ein (US-Dollar je 1 Million Tokens, siehe
Scaleways Preisliste), schätzt ArchivLotse die Kosten jedes Aufrufs
anhand der tatsächlich verbrauchten Tokens und loggt sie
(`Tokenverbrauch prompt=... completion=..., geschätzte Kosten: $...`)
sowie in `step_results` (`prompt_tokens`, `completion_tokens`,
`estimated_cost_usd`). Für `ollama`-Modelle ist das nicht nötig – lokale
Anfragen werden immer als `$0` gezählt. Ohne hinterlegten Preis wird nur
der Tokenverbrauch geloggt, die Kosten als „unbekannt" markiert.

Statt Zugangsdaten direkt in die Datei zu schreiben, kannst du auch
Umgebungsvariablen benutzen, z. B. `token: "${PAPERLESS_TOKEN}"` oder
`secret_key: "${SCALEWAY_SECRET_KEY}"` – dann wird beim Start der Wert
aus der jeweiligen Umgebungsvariable eingesetzt, falls diese gesetzt ist.

Für die Scaleway Generative APIs wird nur der **Secret Key** tatsächlich
für die Anmeldung gebraucht (als Bearer-Token); die **Access Key ID**
steht nur zur eigenen Zuordnung mit im Provider-Eintrag und wird sonst
nirgends verwendet.

### 3.3 `config/steps.yaml`

Diese Datei beschreibt jeden Verarbeitungsschritt vollständig – der
Python-Code selbst weiß nichts über "Titel" oder irgendein anderes Feld,
das steht ausschließlich hier:

```yaml
pipeline:
  - ocr_check
  - document_type
  - correspondent
  - document_date
  - geldbetrag
  - tags
  - title
  - mark_done

steps:
  title:
    source_field: content # Paperless-Feld, das als Eingabetext gelesen wird
    target_field: title # Paperless-Feld, das mit dem Ergebnis beschrieben wird

    prompt: title.j2 # Datei in config/prompts/
    model: "qwen3:4b-instruct" # Modell aus config/ai.yaml (models:)

    preprocessing:
      max_input_chars: 8000 # Eingabetext wird vorher auf so viele Zeichen gekuerzt

    postprocessing:
      strip_quotes: true # Anfuehrungszeichen am Rand entfernen
      max_length: 120 # zur Sicherheit auf max. so viele Zeichen kuerzen
```

`pipeline:` legt fest, in welcher Reihenfolge `archivlotse process <id>`
(ohne `--step`) die Schritte ausführt – siehe Abschnitt 4.

Für den mitgelieferten Titel-Schritt musst du hier normalerweise nichts
ändern – nur wenn du z. B. einen anderen Prompt ausprobieren oder die
maximale Titellänge anpassen möchtest.

**Einen neuen Freitext-Schritt hinzufügen** geht rein über Config, ohne
Python-Code zu ändern: eine `.j2`-Prompt-Datei unter `config/prompts/`
anlegen, einen neuen Eintrag unter `steps:` ergänzen (mit `source_field`,
`target_field`, `prompt`, `model`), ihn in `pipeline:` einsortieren und
das Modell ggf. unter `models:` in `config/ai.yaml` ergänzen. Der Prompt bekommt:

- `{{ input_text }}`: den (ggf. gekürzten) Inhalt von `source_field`.
- `{{ document_type_name }}`: den Namen des aktuell gesetzten
  Dokumenttyps (oder leer, falls keiner gesetzt ist) – damit kann ein
  Prompt sein Verhalten je nach Dokumenttyp anpassen, z. B. in
  `title.j2`:

  ```jinja
  {% if document_type_name == "Kontoauszug" %}
  - Dies ist ein Kontoauszug. Nenne den Buchungszeitraum im Titel.
  {% endif %}
  ```

**Schritt nur bedingt ausführen:** ein optionales `condition:` überspringt
den gesamten Schritt (kein KI-Aufruf, nichts geschrieben), wenn das
aktuelle Dokument nicht passt, z. B. nur für Kontoauszüge:

```yaml
    condition:
      document_type: Kontoauszug
```

Statt eines einzelnen Werts geht auch eine Liste erlaubter Werte, z. B.
`document_type: [Rechnung, Quittung]`.

Aktuell wird nur `document_type` als Bedingungsfeld aufgelöst (vom
Namen, nicht der ID); andere Feldnamen werden direkt mit dem
Paperless-Rohwert verglichen.

**Auswahl-Schritt `document_type`:** hier muss die KI sich für einen der
tatsächlich in Paperless vorhandenen Dokumenttypen entscheiden, statt
freien Text zu erzeugen:

```yaml
  document_type:
    source_field: content
    target_field: document_type

    prompt: document_type.j2
    model: "mistral-small-3.2-24b-instruct-2506"

    # Laedt die vorhandenen Dokumenttypen ueber die Paperless-API und gibt
    # sie dem Prompt als Kandidatenliste mit. Ein Vorschlag, der zu keinem
    # davon passt, wird verworfen statt gespeichert.
    candidates_endpoint: document_types

    preprocessing:
      max_input_chars: 8000

    postprocessing:
      strip_quotes: true
      max_length: 200
```

Der Prompt (`config/prompts/document_type.j2`) bekommt zusätzlich zu
`{{ input_text }}` die Liste `{{ candidate_names }}` (alle vorhandenen
Dokumenttypnamen) und ist angewiesen, exakt `UNBEKANNT` zu antworten, wenn
keiner passt. Geschrieben wird in Paperless die **ID** des passenden
Dokumenttyps (`document_type` erwartet in der API eine ID, keinen Namen);
im Audit-Log stehen aber die lesbaren Namen, nicht die IDs.

**Mehrfachauswahl-Schritt `tags`:** wie `document_type`, aber mit `multi:
true` – die KI darf mehrere, durch Komma getrennte Tags vorschlagen:

```yaml
  tags:
    source_field: content
    target_field: tags

    prompt: tags.j2
    model: "qwen3.8-27b"

    candidates_endpoint: tags
    multi: true # Mehrfachauswahl statt genau eines Werts

    preprocessing:
      max_input_chars: 8000

    postprocessing:
      strip_quotes: true
      max_length: 500
```

Unterschiede zum einfachen Auswahl-Schritt:

- Der Prompt (`config/prompts/tags.j2`) bekommt zusätzlich
  `{{ current_tag_names }}` (die aktuell am Dokument gesetzten Tags) und
  antwortet in **zwei Zeilen**: `Hinzufügen: <Liste oder KEINE>` und
  `Löschen: <Liste oder KEINE>` – die KI kann also auch vorschlagen,
  einen vorhandenen, nicht mehr passenden Tag zu entfernen.
- Jeder einzelne Vorschlag (in beiden Zeilen) wird für sich geprüft – ein
  ungültiger Einzelvorschlag wird nur ignoriert (mit Log-Warnung), nicht
  der ganze Schritt verworfen.
- Das Ergebnis ist `(vorhandene Tags ∪ Hinzufügen) − Löschen`. Steht ein
  Tag in beiden Listen, gewinnt **Löschen** (mit Log-Warnung).
- Ältere/einfachere Antworten (nur eine kommagetrennte Liste, ohne die
  beiden Label-Zeilen) werden weiterhin als reine Hinzufügen-Liste
  interpretiert – falls das Modell das Format trotz Anweisung ignoriert.

**OCR-Qualitätsschritt `ocr_check`:** kein `prompt`/`model` nötig, da
kein KI-Aufruf stattfindet:

```yaml
  ocr_check:
    source_field: content
    target_field: content # gleiches Feld: aktualisiert content, falls besser

    quality_check: heuristic # bisher einzige Option (ai_judge geplant)
    quality_threshold: 0.50
    min_chars_for_check: 200 # kuerzere Texte werden nicht geprueft

    docling:
      base_url: "http://127.0.0.1:5001"
      force_ocr: true
```

Ablauf: Das vorhandene `content`-Feld wird mit einer Heuristik bewertet
(Anteil der Wörter, die in einer deutschen Wortfrequenzliten als
einigermaßen gebräuchlich gelten – siehe `archivlotse/ocr_quality.py`).
Liegt der Score unter `quality_threshold`, wird das Originaldokument von
Paperless heruntergeladen, über Docling Serve mit `force_ocr` neu erkannt,
und das Ergebnis geschrieben – sofern es nicht verdächtig viel kürzer ist
als der vorhandene Text (einfache Notbremse).

**Wichtige Einschränkung, bitte lesen:** Die Heuristik ist bewusst simpel
und wurde an echten Dokumenten kalibriert, hat aber nachweisliche
Schwächen: zahlenlastige Dokumente (Kontoauszüge, Formulare) werden
tendenziell niedriger bewertet als sie es "verdienen", und beim Vergleich
"alter vs. neuer Text" liefert sie keine verlässliche Aussage, welcher
Text besser ist (ein fachsprachlich korrekter Text kann einen niedrigeren
Score bekommen als sein eigenes verrauschtes Original). Deshalb wird der
Score **nicht** benutzt, um zu entscheiden, ob das Docling-Ergebnis
übernommen wird – nur, um zu entscheiden, ob überhaupt ein Re-OCR-Versuch
lohnt. Bei Bedarf `quality_threshold` anpassen oder stichprobenartig mit
`--dry-run --verbose` nachprüfen.

**Datums-Schritt `document_date`:** bestimmt das Erstellungsdatum
(Paperless-Feld `created`) aus dem OCR-Text, mit zwei Prompts:

- Text bis `small_max_chars` (8.000) Zeichen: einmal komplett mit
  `create-date-small.j2` analysieren; ein gültiges Datum wird übernommen.
- Längere Texte: zuerst die ersten `part_chars` (6.000) Zeichen mit
  `create-date-part.j2` (Antwort `TT.MM.JJJJ|Score`). Bei Score ≥
  `min_score` (85) wird das Datum übernommen und nicht weiter gesucht,
  sonst zusätzlich die letzten 6.000 Zeichen; das gültige Datum mit dem
  höchsten Score gewinnt.
- Liegt der beste Score unter `min_score`, wird das Datum nur gesetzt,
  wenn das Feld noch leer ist – ein vorhandenes Datum wird nie durch eine
  unsichere Schätzung überschrieben.

Jede Antwort wird im Code geprüft: exakt im erwarteten Format, gültiges
Kalenderdatum, nicht in der Zukunft, nicht vor 1900. Alles andere zählt
als "kein Datum gefunden".

```yaml
  document_date:
    source_field: content
    target_field: created
    prompt: create-date-part.j2 # fuer Abschnitte langer Dokumente
    model: "mistral-small-3.2-24b-instruct-2506"
    date:
      prompt_small: create-date-small.j2 # fuer kurze Dokumente
      small_max_chars: 8000
      part_chars: 6000
      min_score: 85
```

**Zusatzfeld-Schritt `geldbetrag`:** beschreibt ein Paperless-Zusatzfeld
(Custom Field) – hier `Geldbetrag`, und nur bei Rechnungen und Quittungen.
Das Zusatzfeld muss in Paperless **bereits existieren** (ArchivLotse legt
keine an). Die KI-Antwort wird gegen den Datentyp des Felds geprüft
(unterstützt: `string`, `longtext`, `integer`, `float`, `monetary`) – bei
`Geldbetrag` (float) wird also nur eine Zahl geschrieben; `KEIN-WERT` oder
eine Antwort im falschen Format wird verworfen. Andere Zusatzfelder des
Dokuments bleiben unverändert. Ein bereits gesetzter Wert wird nur mit
`overwrite_existing: true` überschrieben, sonst wird der Schritt vor dem
KI-Aufruf übersprungen.

```yaml
  geldbetrag:
    source_field: content
    custom_field: Geldbetrag # Name des Zusatzfelds in Paperless
    condition:
      document_type: [Rechnung, Quittung]
    prompt: geldbetrag.j2 # bekommt zusaetzlich {{ field_name }}
    model: "mistral-small-3.2-24b-instruct-2506"
    overwrite_existing: false
```

Für ein weiteres Zusatzfeld (z. B. `Rechnungsnummer`) reicht ein neuer
Prompt plus ein solcher Eintrag in `steps:`/`pipeline:`.

**Tag-Setz-Schritt `mark_done`:** ebenfalls ohne KI – hängt feste,
**in Paperless bereits vorhandene** Tags an das Dokument an (vorhandene
Tags bleiben erhalten). Fehlt ein Tag in Paperless, bricht der Schritt
mit einer Fehlermeldung ab; ArchivLotse legt ihn **nicht** selbst an –
`archivlotse-done` also einmal von Hand in Paperless anlegen.

```yaml
  mark_done:
    target_field: tags
    add_tags:
      - archivlotse-done
    # nur ausfuehren, wenn kein frueherer Schritt dieses Laufs
    # fehlgeschlagen ist (gilt fuer die ganze pipeline: und Batch-Jobs,
    # nicht fuer "--step mark_done")
    requires_previous_success: true
```

`mark_done` steht als letzter Schritt in `pipeline:`. Zusammen mit
`exclude_tags: [archivlotse-done]` im Batch-Job `todo` (siehe
`config/batch.yaml`) wird so jedes Dokument nur einmal komplett
durchlaufen.

## 4. Erster Testlauf (Dry-Run)

Im Dry-Run-Modus wird alles berechnet, aber **nichts** in Paperless
verändert – ideal zum Ausprobieren. Ersetze `6023` durch die ID eines
Dokuments aus deiner Paperless-Instanz (steht in der Paperless-URL, wenn
du ein Dokument öffnest).

```bash
archivlotse process 6023 --step title --dry-run
archivlotse process 6023 --step document_type --dry-run
archivlotse process 6023 --dry-run  # ganze Pipeline (siehe pipeline: in steps.yaml)
```

Die Ausgabe zeigt unter anderem, welcher Titel bzw. Dokumenttyp
vorgeschlagen wird und ob er sich vom aktuellen Wert unterscheidet. Es
wird aber nichts in Paperless gespeichert.

Ohne `--step` werden alle Schritte aus `pipeline:` der Reihe nach
ausgeführt. Im **Dry-Run** fetcht trotzdem jeder Schritt das Dokument neu
aus Paperless – da im Dry-Run aber nichts geschrieben wird, sieht z. B.
der `title`-Schritt dort noch den *alten* Dokumenttyp, nicht den vom
vorherigen `document_type`-Schritt gerade vorgeschlagenen. Im echten Lauf
(ohne `--dry-run`) sieht jeder Schritt dagegen die tatsächlich
gespeicherten Änderungen der vorherigen Schritte.

## 5. Echter Lauf

Ohne `--dry-run` wird der neue Titel – falls er sich geändert hat –
tatsächlich in Paperless gespeichert:

```bash
archivlotse process 6023 --step title
archivlotse process 6023  # ganze Pipeline
```

Führst du den Befehl für dasselbe Dokument ein zweites Mal aus, ohne dass
sich der Eingabetext, der Prompt oder das Modell geändert haben, wird
**keine** erneute KI-Anfrage gestellt – ArchivLotse nutzt das gespeicherte
Ergebnis aus der letzten Berechnung.

Mit `--model <name>` kannst du ein anderes Modell aus `config/ai.yaml`
(`models:`) für diesen Lauf benutzen, z. B.
`archivlotse process 6023 --step title --model llama-3.3-70b-instruct`.

### Erneut berechnen trotz Cache (`--force`)

Manchmal willst du ein Ergebnis absichtlich neu berechnen lassen, obwohl
sich am Eingabetext nichts geändert hat – z. B. weil du einen Prompt nur
kosmetisch angepasst hast (was die Zwischenspeicherung nicht als
"geändert" erkennt) oder einfach prüfen willst, ob die KI heute anders
antwortet. `--force` ignoriert dafür **alle** Zwischenspeicher (KI-Ergebnisse
und Kandidatenlisten) und löst auch dann eine neue KI-Anfrage aus, wenn
eine gecachte Antwort vorläge:

```bash
archivlotse process 6023 --step title --force
archivlotse process 6023 --force  # ganze Pipeline
```

`--force` überschreibt **nicht** eigentliche Business-Regeln wie die
`condition:` eines Schritts oder die OCR-Qualitätsschwelle – die
entscheiden weiterhin ganz normal, ob ein Schritt überhaupt etwas tut.

### Zwischenspeicher komplett leeren (`clear-cache`)

Hast du z. B. deine Paperless-Tags umstrukturiert und möchtest, dass
ArchivLotse beim nächsten Lauf garantiert mit frischen Kandidatenlisten
und ohne alte KI-Ergebnisse arbeitet, kannst du statt `--force` pro
Dokument einmalig den kompletten Cache leeren:

```bash
archivlotse clear-cache
```

Das leert `step_results` und `candidate_cache` vollständig. Das `audit_log`
(die dauerhafte Historie tatsächlicher Paperless-Änderungen) bleibt
unangetastet.

### Mehrere Dokumente auf einmal verarbeiten (Batch)

`archivlotse process` bearbeitet immer genau ein Dokument. Für viele
Dokumente gibt es `archivlotse batch`, das die Jobs aus
`config/batch.yaml` ausführt – nacheinander, nicht parallel, jeweils
kleinste Dokument-ID zuerst:

```yaml
jobs:
  # Alles mit Tag TODO, aber ohne Tag archivlotse-done: ganze Pipeline
  todo:
    filter:
      tags: [TODO]                    # muss ALLE diese Tags tragen
      exclude_tags: [archivlotse-done] # darf KEINEN dieser Tags tragen

  # Titel wie 20260928_124530 (Scanner-Dateiname): nur neuen Titel berechnen
  scan_titles:
    filter:
      title_regex: '^\d{4}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])_\d+$'
    steps: [title]                    # optional, Standard: ganze pipeline:
    # limit: 50                       # optional: höchstens so viele pro Lauf
```

Jeder Job braucht mindestens `tags` oder `title_regex` (damit nicht aus
Versehen das ganze Archiv verarbeitet wird). Die Regex in einfache
Anführungszeichen setzen, sonst verändert YAML die Backslashes. Ein Tag in
`exclude_tags`, den es in Paperless noch nicht gibt, schließt einfach
nichts aus (mit Warnung im Log); ein Tag in `tags`, den es nicht gibt,
ist dagegen ein Fehler.

```bash
archivlotse batch --dry-run             # alle Jobs der Reihe nach, nur testen
archivlotse batch                       # alle Jobs
archivlotse batch scan_titles           # nur dieser Job
archivlotse batch todo --limit 10       # höchstens 10 Dokumente (überschreibt limit:)
```

Direkt vor jedem Dokument wird der Filter erneut geprüft: passt ein
Dokument inzwischen nicht mehr (z. B. Tag entfernt, oder Titel schon von
einem früheren Job im selben Lauf neu gesetzt), wird es übersprungen.
Scheitert die Verarbeitung eines einzelnen Dokuments, wird das geloggt und
mit dem nächsten weitergemacht. ArchivLotse setzt oder entfernt dabei
selbst **keine** Tags (auch kein `archivlotse-done`) – ob und wie du
erledigte Dokumente kennzeichnest, bleibt dir überlassen.

## 6. Wo landen die Daten?

Beim ersten Lauf legt ArchivLotse automatisch eine Datei
`data/archivlotse.db` an (eine SQLite-Datenbank, also eine einzelne Datei –
keine separate Serverinstallation nötig). Darin stehen drei Tabellen:

- `step_results`: jedes berechnete Ergebnis (für jeden Schritt), inkl.
  verwendetem Modell und Prompt – dient als Cache, damit nichts doppelt
  berechnet wird. Enthält außerdem `prompt_tokens`, `completion_tokens`
  und `estimated_cost_usd` pro KI-Aufruf (siehe Abschnitt 7).
- `audit_log`: nur Einträge zu tatsächlich in Paperless geänderten
  Feldern (Dokument-ID, Feldname, alter Wert, neuer Wert, Zeitpunkt).
- `candidate_cache`: zwischengespeicherte Kandidatenlisten (Dokumenttypen,
  Tags) für Auswahl-/Mehrfachauswahl-Schritte, 2 Stunden gültig. Ändert
  sich ein Tag/Dokumenttyp in Paperless, kann es bis zu 2 Stunden dauern,
  bis ArchivLotse das bemerkt – zum sofortigen Erzwingen einfach die Zeile
  löschen: `sqlite3 data/archivlotse.db "DELETE FROM candidate_cache;"`.

Reinschauen kannst du z. B. mit dem `sqlite3`-Kommandozeilenwerkzeug
(meist schon installiert):

```bash
sqlite3 data/archivlotse.db "SELECT * FROM audit_log;"
sqlite3 data/archivlotse.db "SELECT * FROM step_results;"
sqlite3 data/archivlotse.db "SELECT endpoint, fetched_at FROM candidate_cache;"
```

## 7. Mehr Logging bei Problemen

Standardmäßig wird auf Stufe `INFO` geloggt. Mit `-v`/`--verbose` siehst
du zusätzlich den **vollständigen Prompt**, der an die KI geschickt wird
(inkl. OCR-Text und – beim `document_type`-Schritt – der Kandidatenliste),
sowie die rohe, noch nicht nachbearbeitete KI-Antwort:

```bash
archivlotse process 6023 --step document_type --dry-run --verbose
```

`-v` hebt gezielt nur ArchivLotses eigenes Logging auf `DEBUG` an – die
sehr geschwätzigen Verbindungsdetails von httpx/httpcore bleiben dabei
auf `INFO`. Für wirklich alles (inkl. HTTP-Verbindungsdetails) gibt es
zusätzlich die Umgebungsvariable:

```bash
ARCHIVLOTSE_LOG_LEVEL=DEBUG archivlotse process 6023 --step title --dry-run
```

Ist ein Dienst nicht erreichbar (Ollama, Docling, Scaleway, Paperless),
läuft in eine Zeitüberschreitung oder antwortet mit einem HTTP-Fehler,
erscheint das als **eine** Fehlerzeile mit der betroffenen URL, z. B.
`Dienst nicht erreichbar (POST http://127.0.0.1:11434/api/generate): [Errno 111] Connection refused`.
Den vollständigen Stacktrace dazu gibt es nur mit `-v`. Nur unerwartete
Fehler (also vermutlich Programmfehler) werden immer mit Stacktrace geloggt.

## 8. Typische Fehler

- `FileNotFoundError: config/paperless.yaml` → Schritt 3 noch nicht
  durchgeführt (Dateien kopieren und ausfüllen).
- `401 Unauthorized` von Paperless → Token in `config/paperless.yaml`
  falsch oder abgelaufen.
- Verbindungsfehler zu Ollama → `base_url` des jeweiligen Providers in
  `config/ai.yaml` prüfen, erreichbar per `curl http://dein-host:11434`.
- `Unbekanntes Modell ...` → das mit `--model` angegebene (oder das in
  `config/steps.yaml` beim Schritt eingetragene) Modell fehlt unter
  `models:` in `config/ai.yaml`.
- `... benutzt noch das alte 'roles:'-Format` → `config/ai.yaml` von
  `roles:` auf `models:` umstellen (siehe `config/ai.example.yaml`).
- `Modell ... verweist auf unbekannten Provider ...` → der unter `models:`
  angegebene `provider:`-Name taucht nicht unter `providers:` auf (Tippfehler?).
- `ValueError: Scaleway provider requires 'secret_key'` → `secret_key`
  beim jeweiligen Scaleway-Provider in `config/ai.yaml` ausfüllen.
- `403 Forbidden` / `"insufficient permissions to access the resource"`
  von Scaleway → zwei mögliche Ursachen:
  1. In `base_url` fehlt die Project-ID im Pfad
     (`https://api.scaleway.ai/v1` statt
     `https://api.scaleway.ai/<project-id>/v1`). Die korrekte URL inkl.
     Project-ID steht in der Scaleway-Konsole im Code-Beispiel pro Modell.
  2. Der API-Key hat keine passende IAM-Policy. Unter
     https://console.scaleway.com/iam/policies prüfen, ob der
     User/die Application, zu der der Key gehört, eine Policy mit
     Permission-Set `GenerativeApisModelAccess` (oder
     `GenerativeApisFullAccess`/`AllProductsFullAccess`) für das
     betreffende Project hat.
- `Unbekannter Verarbeitungsschritt 'title' in config/steps.yaml` →
  `config/steps.yaml` fehlt oder wurde versehentlich verändert/gelöscht.
- Log-Zeile „KI-Vorschlag ... entspricht keinem vorhandenen Kandidaten,
  wird verworfen" beim `document_type`-Schritt → kein Fehler, sondern die
  eingebaute Validierung: die KI hat etwas vorgeschlagen, das nicht
  exakt einem vorhandenen Dokumenttyp entspricht, und der Vorschlag wurde
  bewusst **nicht** übernommen.
- `Schritt ... quality_check 'ai_judge' ist noch nicht implementiert` →
  bisher ist nur `quality_check: heuristic` für `ocr_check` gebaut.
- Verbindungsfehler zu Docling bei `ocr_check` → `docling.base_url` in
  `config/steps.yaml` prüfen, erreichbar per
  `curl http://127.0.0.1:5001/health`. Ohne Docling kannst du `ocr_check`
  einfach aus `pipeline:` entfernen und die anderen Schritte weiter per
  `--step` einzeln nutzen.

## 9. Tests ausführen (optional, für Entwickler)

Das ist nicht nötig, um ArchivLotse zu benutzen – nur falls du am Code
mitarbeiten willst:

```bash
pip install -e ".[dev]"
pytest
```
