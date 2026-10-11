# ArchivLotse – Erste Schritte

Diese Anleitung ist für den Einstieg gedacht: Du brauchst dafür keine
Vorkenntnisse in Python-Paketierung.

Aktueller Stand: ArchivLotse führt generische, in `config/steps.yaml`
definierte Verarbeitungsschritte aus – ein Schritt liest ein Paperless-Feld
(z. B. den OCR-Text), lässt ein KI-Modell daraus einen Wert erzeugen und
schreibt das Ergebnis (optional) in ein anderes Paperless-Feld zurück. Es
gibt zwei Arten von Schritten:

- **Freitext-Schritte** wie `title`: die KI erzeugt frei formulierten Text
  (z. B. einen Titel).
- **Auswahl-Schritte** wie `document_type`: die KI muss sich für genau
  einen von mehreren **bereits in Paperless vorhandenen** Werten
  entscheiden (hier: einen Dokumenttyp). Ein Vorschlag, der zu keinem
  vorhandenen Dokumenttyp passt, wird verworfen und **nicht** gespeichert.

Weitere Freitext-Schritte lassen sich rein über Config ergänzen (siehe
Abschnitt 3.3) – ohne Python-Code zu ändern; ein weiterer Auswahl-Schritt
(z. B. für Korrespondenten oder Tags) braucht zusätzlich eine kleine
Codeänderung (eine neue Zeile in `CANDIDATE_FETCHERS` in
`archivlotse/pipeline.py`). Alles Weitere aus der Projektidee
(`doc/PROJEKTIDEE.md`, z. B. OCR-Verbesserung, Custom Fields) ist noch
nicht gebaut.

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

ArchivLotse liest drei Konfigurationsdateien:

- `config/paperless.yaml` und `config/ai.yaml` enthalten Zugangsdaten
  (Token, API-Keys) und werden daher aus Beispieldateien kopiert – die
  echten Dateien sind in Git ignoriert, landen also nicht versehentlich
  in einem Commit.
- `config/steps.yaml` enthält **keine** Zugangsdaten, nur fachliche
  Pipeline-Regeln (welcher Prompt, welche Rolle, Vor-/Nachbearbeitung) –
  diese Datei ist bereits im Projekt vorhanden und mit Git versioniert.

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
- **`roles:`** – hier legst du pro Aufgabe ("Rolle") fest, welcher
  Provider und welches Modell benutzt wird. Der Titel-Schritt benutzt die
  Rolle `title`. Mehrere Rollen können sich denselben Provider (und damit
  dieselben Zugangsdaten) teilen, ohne sie zu wiederholen.

Beispiel: ein Modell läuft lokal über Ollama, ein anderes in der Cloud
über Scaleway – die Scaleway-Zugangsdaten stehen dabei nur einmal:

```yaml
providers:
  ollama_lokal:
    type: ollama
    base_url: "http://localhost:11434" # oder http(s)://dein-ollama-host:11434

  scaleway:
    type: scaleway
    base_url: "https://api.scaleway.ai/v1"
    # API-Schluessel erzeugen/einsehen: https://console.scaleway.com/iam/api-keys
    # Dort stehen "Access Key" und "Secret Key" genau so beschriftet.
    access_key_id: "SCWxxxxxxxxxxxxxxxxx"
    secret_key: "${SCALEWAY_SECRET_KEY}"

roles:
  title:
    provider: ollama_lokal
    model: "llama3.1" # Name des Modells, das in Ollama geladen ist

  document_type:
    provider: scaleway
    model: "mistral-small-3.2-24b-instruct-2506" # guenstig, reicht fuer Klassifizierung

  precise:
    provider: scaleway
    model: "llama-3.3-70b-instruct" # wird z.B. mit --ai-role precise genutzt
```

Du brauchst nur die Provider zu pflegen, die du tatsächlich in `roles:`
verwendest. Welche Rolle der Titel-Schritt benutzt, lässt sich auch beim
Aufruf überschreiben (siehe unten, `--ai-role`) – Standard ist `title`.

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
steps:
  title:
    source_field: content # Paperless-Feld, das als Eingabetext gelesen wird
    target_field: title # Paperless-Feld, das mit dem Ergebnis beschrieben wird

    prompt: title.j2 # Datei in archivlotse/prompts/
    ai_role: title # Rolle aus config/ai.yaml

    preprocessing:
      max_input_chars: 8000 # Eingabetext wird vorher auf so viele Zeichen gekuerzt

    postprocessing:
      strip_quotes: true # Anfuehrungszeichen am Rand entfernen
      max_length: 120 # zur Sicherheit auf max. so viele Zeichen kuerzen
```

Für den mitgelieferten Titel-Schritt musst du hier normalerweise nichts
ändern – nur wenn du z. B. einen anderen Prompt ausprobieren oder die
maximale Titellänge anpassen möchtest.

**Einen neuen Freitext-Schritt hinzufügen** geht rein über Config, ohne
Python-Code zu ändern: eine `.j2`-Prompt-Datei unter `archivlotse/prompts/`
anlegen, einen neuen Eintrag unter `steps:` ergänzen (mit `source_field`,
`target_field`, `prompt`, `ai_role`) und bei Bedarf eine passende Rolle in
`config/ai.yaml`. Der Prompt bekommt den (ggf. gekürzten) Inhalt von
`source_field` als Variable `{{ input_text }}`.

**Auswahl-Schritt `document_type`:** hier muss die KI sich für einen der
tatsächlich in Paperless vorhandenen Dokumenttypen entscheiden, statt
freien Text zu erzeugen:

```yaml
  document_type:
    source_field: content
    target_field: document_type

    prompt: document_type.j2
    ai_role: document_type

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

Der Prompt (`archivlotse/prompts/document_type.j2`) bekommt zusätzlich zu
`{{ input_text }}` die Liste `{{ candidate_names }}` (alle vorhandenen
Dokumenttypnamen) und ist angewiesen, exakt `UNBEKANNT` zu antworten, wenn
keiner passt. Geschrieben wird in Paperless die **ID** des passenden
Dokumenttyps (`document_type` erwartet in der API eine ID, keinen Namen);
im Audit-Log stehen aber die lesbaren Namen, nicht die IDs.

## 4. Erster Testlauf (Dry-Run)

Im Dry-Run-Modus wird alles berechnet, aber **nichts** in Paperless
verändert – ideal zum Ausprobieren. Ersetze `6023` durch die ID eines
Dokuments aus deiner Paperless-Instanz (steht in der Paperless-URL, wenn
du ein Dokument öffnest).

```bash
archivlotse process 6023 --step title --dry-run
archivlotse process 6023 --step document_type --dry-run
```

Die Ausgabe zeigt unter anderem, welcher Titel bzw. Dokumenttyp
vorgeschlagen wird und ob er sich vom aktuellen Wert unterscheidet. Es
wird aber nichts in Paperless gespeichert.

## 5. Echter Lauf

Ohne `--dry-run` wird der neue Titel – falls er sich geändert hat –
tatsächlich in Paperless gespeichert:

```bash
archivlotse process 6023 --step title
```

Führst du den Befehl für dasselbe Dokument ein zweites Mal aus, ohne dass
sich der Eingabetext, der Prompt oder das Modell geändert haben, wird
**keine** erneute KI-Anfrage gestellt – ArchivLotse nutzt das gespeicherte
Ergebnis aus der letzten Berechnung.

Mit `--ai-role <name>` kannst du eine andere Rolle aus `config/ai.yaml`
für diesen Lauf benutzen, z. B. `archivlotse process 6023 --step title --ai-role precise`.

## 6. Wo landen die Daten?

Beim ersten Lauf legt ArchivLotse automatisch eine Datei
`data/archivlotse.db` an (eine SQLite-Datenbank, also eine einzelne Datei –
keine separate Serverinstallation nötig). Darin stehen zwei Tabellen:

- `step_results`: jedes berechnete Ergebnis (für jeden Schritt), inkl.
  verwendetem Modell und Prompt – dient als Cache, damit nichts doppelt
  berechnet wird.
- `audit_log`: nur Einträge zu tatsächlich in Paperless geänderten
  Feldern (Dokument-ID, Feldname, alter Wert, neuer Wert, Zeitpunkt).

Reinschauen kannst du z. B. mit dem `sqlite3`-Kommandozeilenwerkzeug
(meist schon installiert):

```bash
sqlite3 data/archivlotse.db "SELECT * FROM audit_log;"
sqlite3 data/archivlotse.db "SELECT * FROM step_results;"
```

## 7. Mehr Logging bei Problemen

Standardmäßig wird auf Stufe `INFO` geloggt. Für mehr Details:

```bash
ARCHIVLOTSE_LOG_LEVEL=DEBUG archivlotse process 6023 --step title --dry-run
```

## 8. Typische Fehler

- `FileNotFoundError: config/paperless.yaml` → Schritt 3 noch nicht
  durchgeführt (Dateien kopieren und ausfüllen).
- `401 Unauthorized` von Paperless → Token in `config/paperless.yaml`
  falsch oder abgelaufen.
- Verbindungsfehler zu Ollama → `base_url` des jeweiligen Providers in
  `config/ai.yaml` prüfen, erreichbar per `curl http://dein-host:11434`.
- `Unbekannte Modellrolle ...` → die mit `--ai-role` angegebene (oder die
  Standard-Rolle `title`) fehlt unter `roles:` in `config/ai.yaml`.
- `Rolle ... verweist auf unbekannten Provider ...` → der unter `roles:`
  angegebene `provider:`-Name taucht nicht unter `providers:` auf (Tippfehler?).
- `ValueError: Scaleway provider requires 'secret_key'` → `secret_key`
  beim jeweiligen Scaleway-Provider in `config/ai.yaml` ausfüllen.
- `Unbekannter Verarbeitungsschritt 'title' in config/steps.yaml` →
  `config/steps.yaml` fehlt oder wurde versehentlich verändert/gelöscht.
- Log-Zeile „KI-Vorschlag ... entspricht keinem vorhandenen Kandidaten,
  wird verworfen" beim `document_type`-Schritt → kein Fehler, sondern die
  eingebaute Validierung: die KI hat etwas vorgeschlagen, das nicht
  exakt einem vorhandenen Dokumenttyp entspricht, und der Vorschlag wurde
  bewusst **nicht** übernommen.

## 9. Tests ausführen (optional, für Entwickler)

Das ist nicht nötig, um ArchivLotse zu benutzen – nur falls du am Code
mitarbeiten willst:

```bash
pip install -e ".[dev]"
pytest
```
