# ArchivLotse

**Ein konfigurationsgesteuerter KI-Assistent für Paperless-ngx**

*Lokale oder europäische KI-Dienste, intelligente Dokumentanalyse und nachvollziehbare Metadatenpflege.*

## Projektidee

ArchivLotse ist eine schlanke, in Python entwickelte Software zur intelligenten Nachbearbeitung von Dokumenten in Paperless-ngx.

Ziel ist es, die Erkennung und Klassifizierung von Dokumenten zu verbessern, aussagekräftige Titel zu erzeugen und relevante Metadaten automatisch zu extrahieren – ohne dass für jedes Dokument sämtliche verfügbaren Informationen an ein großes Sprachmodell übertragen werden müssen.

Statt eines einzigen umfangreichen KI-Aufrufs verwendet ArchivLotse eine **mehrstufige, regelbasierte Verarbeitungspipeline**. Je nach Dokumenttyp, vorhandenen Metadaten und Qualität des erkannten Textes entscheidet die Software, welche Verarbeitungsschritte notwendig sind und welches KI-Modell dafür eingesetzt wird.

Die fachliche Konfiguration erfolgt weitgehend über **YAML-Dateien und Jinja2-Prompt-Templates**. Der Python-Code stellt die allgemeine Ausführungslogik, API-Anbindungen und Validierung bereit.

Das Projekt soll bewusst überschaubar bleiben, sich gut über Git versionieren lassen und ohne komplexe zusätzliche Infrastruktur betrieben werden können.

## 1. Projektziele

ArchivLotse soll insbesondere folgende Aufgaben übernehmen:

- Vorhandene Dokumente über die Paperless-ngx-API abrufen und neue Dokumente bei Bedarf automatisch bearbeiten.
- Zwischen digital erzeugten PDFs und eingescannten Dokumenten unterscheiden.
- Bei Bedarf eine verbesserte OCR mit Docling und RapidOCR ausführen.
- Dokumente anhand ihres Inhalts klassifizieren.
- Aussagekräftige, einheitlich formatierte Dokumenttitel erstellen.
- Vorhandene Korrespondenten, Dokumenttypen und Tags intelligent zuordnen.
- Dokumenttypabhängige Custom Fields extrahieren und validieren.
- Das maßgebliche Dokumentdatum bestimmen und von anderen Datumsangaben unterscheiden.
- Je nach Aufgabe zwischen lokalen und externen KI-Modellen wechseln.
- Verarbeitungsergebnisse, Kosten und Änderungen nachvollziehbar protokollieren.
- Bestehende, korrekte Metadaten schützen und unsichere Ergebnisse zur Prüfung vorlegen.

Paperless-ngx bleibt dabei das führende Dokumentenmanagementsystem (*Single Source of Truth*).

## 2. Konfigurationsgesteuerte Verarbeitung

Die zentrale Idee ist eine klare Trennung zwischen Programmcode und fachlichen Regeln.

### YAML-Konfiguration

YAML-Dateien definieren unter anderem:

- Verfügbare KI-Anbieter und Modelle
- Verarbeitungsschritte und ihre Reihenfolge
- Bedingungen für die Ausführung einzelner Schritte
- Beschreibungen und Auswahlkriterien für Dokumenttypen
- Bedeutungen und Verwendung bestehender Tags
- Namensvarianten und Zuordnungsregeln für Korrespondenten
- Dokumenttypabhängige Custom Fields
- Regeln zur Bestimmung von Dokumentdaten
- Token-Budgets, Kostenlimits und Eskalationsregeln
- Validierungs- und Freigabebedingungen

Vorhandene Tags, Dokumenttypen, Korrespondenten und Custom Fields werden aus der Paperless-API geladen. Die Konfiguration ergänzt diese um fachliche Beschreibungen und Regeln. IDs müssen nicht doppelt gepflegt werden.

### Jinja2-Prompts

Jede KI-Aufgabe erhält ein eigenes, versioniertes Prompt-Template.

Beispiele:

- `classify.j2` – Dokument grob klassifizieren
- `title.j2` – Aussagekräftigen Dokumenttitel erzeugen
- `document_date.j2` – Maßgebliches Dokumentdatum ermitteln
- `correspondent.j2` – Korrespondenten identifizieren
- `tags.j2` – Passende Tags auswählen
- `bank_statement.j2` – Bankdaten extrahieren
- `invoice.j2` – Rechnungsdaten extrahieren
- `custom_fields.j2` – Weitere Metadaten ermitteln

Prompts erhalten nur die für ihre Aufgabe relevanten Dokumentinformationen, Regeln und Kandidaten.

## 3. Mehrstufige Dokumentverarbeitung

Die Pipeline besteht aus mehreren unabhängig ausführbaren Schritten.

### Schritt 1: Dokumentprüfung und OCR

ArchivLotse untersucht zunächst den Dokumentzustand:

- Ist bereits ein brauchbarer Text vorhanden?
- Enthält die Original-PDF eingebetteten Text?
- Handelt es sich um eine gescannte oder teilweise gescannte PDF?
- Wie viele Seiten besitzt das Dokument?
- Gibt es Hinweise auf mangelhafte OCR?

Nur wenn eine Verbesserung sinnvoll erscheint, wird Docling/RapidOCR aufgerufen.

Die Originaldatei bleibt unverändert. Ein aktualisierter OCR-Text wird erst nach einer Qualitätsprüfung an Paperless übertragen.

### Schritt 2: Grobe Dokumentklassifizierung

Ein kleines, kostengünstiges Modell ordnet das Dokument zunächst einer allgemeinen Kategorie zu, beispielsweise:

- Rechnung
- Kontoauszug oder Kreditkartenabrechnung
- Vertrag
- Behördenschreiben
- Urkunde
- Versicherung
- Allgemeiner Schriftverkehr
- Unbekannt

Diese Einordnung entscheidet, welche weiteren Analysen notwendig sind.

Sie muss nicht unmittelbar einem endgültigen Paperless-Dokumenttyp entsprechen.

### Schritt 3: Gezielte Metadatenanalyse

Abhängig von der Klassifizierung führt ArchivLotse spezialisierte Analysen aus.

Bei einem Kontoauszug beispielsweise:

- Bank identifizieren
- Passenden Korrespondenten finden
- Auszugsdatum und Abrechnungszeitraum bestimmen
- Kontonummer oder IBAN extrahieren
- Dokumenttitel erstellen
- Relevante Tags auswählen

Bei einer Rechnung dagegen:

- Rechnungsaussteller ermitteln
- Rechnungsnummer extrahieren
- Rechnungsdatum bestimmen
- Rechnungsbetrag und Währung erkennen
- Dokumenttyp und Tags zuordnen

Unpassende Analysen werden übersprungen. So muss ein Kontoauszug nicht nach Rechnungsnummern und eine Heiratsurkunde nicht nach Zahlungsbeträgen untersucht werden.

### Schritt 4: Validierung und Freigabe

KI-Ergebnisse werden nicht ungeprüft gespeichert.

Dazu gehören:

- Prüfung, ob vorgeschlagene Dokumenttypen und Tags tatsächlich existieren
- IBAN-Prüfsummenvalidierung
- Erhaltung führender Nullen in Kontonummern und anderen Kennungen
- Plausibilitätsprüfung von Datumsangaben und Beträgen
- Prüfung, ob ein Wert durch den OCR-Text belegt ist
- Erkennung widersprüchlicher oder mehrdeutiger Ergebnisse
- Schutz vorhandener Metadaten vor unbeabsichtigtem Überschreiben

Unsichere Vorschläge werden markiert und können manuell überprüft werden.

## 4. Intelligente Auswahl vorhandener Metadaten

Ein besonderes Ziel ist die effiziente Verarbeitung großer Metadatenbestände.

Wenn Paperless beispielsweise 521 Korrespondenten enthält, sollen nicht sämtliche Namen bei jeder Anfrage an das Sprachmodell gesendet werden.

Stattdessen verwendet ArchivLotse eine lokale Kandidatensuche.

Diese berücksichtigt:

- Exakte Namensübereinstimmungen
- Alternative Schreibweisen und Abkürzungen
- Unscharfe Namenssuche mit RapidFuzz
- Bekannte E-Mail-Domains und Anschriften
- Weitere eindeutige Identifikationsmerkmale

Erst wenn die Zuordnung nicht eindeutig ist, erhält die KI eine kleine Liste der wahrscheinlichsten Kandidaten.

Bei Tags und Dokumenttypen gilt dasselbe Prinzip: Die KI erhält eine begrenzte Auswahl mit fachlichen Beschreibungen und eindeutigen Regeln.

Neue Metadatenobjekte werden nicht ungefragt angelegt.

## 5. Intelligente Verarbeitung langer Dokumente

Nicht jedes Dokument sollte vollständig und mehrfach an ein Sprachmodell geschickt werden.

ArchivLotse verwendet deshalb verschiedene Strategien:

**Kurze Dokumente:** Der vollständige Text kann direkt analysiert werden.

**Mittellange Dokumente:** Relevante Abschnitte werden anhand von Überschriften, Schlüsselbegriffen und Dokumentstruktur ausgewählt.

**Lange Dokumente:** Die Software durchsucht zunächst das gesamte Dokument nach geeigneten Textstellen. Diese werden mit Seiten- und Positionsangaben gespeichert und nur bei Bedarf an die KI übergeben.

Die Bestimmung eines Dokumentdatums ist ein typisches Beispiel: Ein Vertrag kann sein Abschlussdatum auf der ersten Seite, neben einer Unterschrift oder in einem Nachtrag enthalten.

ArchivLotse darf daher nicht pauschal den Anfang eines Dokuments als ausreichende Informationsquelle behandeln.

Wenn die relevanten Informationen nicht gefunden werden oder mehrere Interpretationen möglich sind, kann ein leistungsfähigeres Modell weitere Textabschnitte analysieren.

## 6. Flexible KI-Anbieter und Modellwahl

ArchivLotse soll KI-Anbieter austauschbar unterstützen.

Geplant sind insbesondere:

- **Ollama:** Lokale Verarbeitung auf eigener CPU oder GPU
- **Scaleway Model-as-a-Service:** Europäische Cloud-Verarbeitung für ausgewählte Aufgaben
- **Weitere OpenAI-kompatible APIs:** Über eine gemeinsame Schnittstelle

In der Konfiguration werden logische Modellrollen definiert, beispielsweise:

- `fast` – günstige Klassifizierung und Titelgenerierung
- `precise` – komplexe Extraktionen und Datumsentscheidungen
- `fallback` – zweite Analyse bei unklaren Ergebnissen

Die Pipeline entscheidet anhand von Dokumenttyp, Textlänge, Aufgabenkomplexität und Validierungsergebnis, welches Modell verwendet wird.

Sensible Dokumente können auf lokale Verarbeitung beschränkt werden.

Ein externer Anbieter erhält nach Möglichkeit nur die benötigten Textausschnitte und Metadaten.

## 7. Nachvollziehbarkeit und Wiederholbarkeit

Jeder Verarbeitungsschritt wird protokolliert.

Gespeichert werden unter anderem:

- Dokument-ID
- Zeitpunkt der Verarbeitung
- Verwendete Pipeline und Git-Version
- Ausgeführter Verarbeitungsschritt
- Verwendetes Modell und dessen Konfiguration
- Prompt-Version
- Ergebnis und Validierungsstatus
- Betroffene Paperless-Felder
- Vorherige und vorgeschlagene Werte
- Tatsächlich übernommene Änderungen
- Fehler und Wiederholungsversuche
- Tokenverbrauch und geschätzte API-Kosten

Das Audit-Protokoll wird getrennt von den n8n- beziehungsweise KI-Dienst-Logs geführt.

Bei jeder Änderung soll nachvollziehbar bleiben, auf welcher Grundlage ein Wert ermittelt und warum er übernommen wurde.

### Inkrementelle Verarbeitung

Jeder Schritt besitzt einen eigenen Verarbeitungsstatus und einen Fingerabdruck seiner relevanten Eingaben.

Wenn beispielsweise nur die Vorlage für das Dokumentdatum verändert wird, müssen OCR, Klassifizierung und andere unveränderte Verarbeitungsschritte nicht erneut ausgeführt werden.

Das reduziert Kosten und Rechenzeit bei der Nachbearbeitung großer Archive.

## 8. Bedienung und Betriebsmodell

ArchivLotse soll zunächst als schlanker Docker-Container ohne zwingend erforderliche eigene Weboberfläche laufen.

Die Bedienung erfolgt über eine Kommandozeile und später optional über eine API oder eine einfache Review-Oberfläche.

Beispielhafte Befehle:

```bash
archivlotse process 6023 --dry-run
archivlotse process 6023 --step title
archivlotse process 6023 --step document_date
archivlotse process --tag ki-test --limit 10
archivlotse status 6023
archivlotse audit 6023
```

Ein **Dry-Run-Modus** ist eine zentrale Funktion: Alle Analysen und Prüfungen werden durchgeführt, aber keine Metadaten in Paperless verändert.

Automatische Verarbeitung soll auf bestimmte Tags, Dokumenttypen, Zeiträume oder andere Auswahlkriterien begrenzt werden können.

Für den produktiven Betrieb sind eine kontrollierte Warteschlange, Wiederholungsversuche und die Wiederaufnahme unterbrochener Jobs vorgesehen.

## 9. Technische Grundlage

Die geplanten Hauptkomponenten:

| Komponente | Technologie |
|---|---|
| Sprache | Python 3 |
| Konfiguration | YAML |
| Prompt-Vorlagen | Jinja2 |
| Strukturierte KI-Ergebnisse | Pydantic |
| HTTP-APIs | httpx |
| Unscharfe Namenssuche | RapidFuzz |
| Lokale Status- und Audit-Datenbank | SQLite |
| OCR | Docling Serve / RapidOCR |
| KI-Anbindung | Ollama und OpenAI-kompatible APIs |
| Versionsverwaltung | Git |
| Bereitstellung | Docker Compose |
| Tests | pytest |

Die Architektur soll möglichst wenige Abhängigkeiten benötigen und keine separate Datenbank oder Message-Broker voraussetzen.

## 10. Nichtziele

ArchivLotse soll ausdrücklich nicht:

- Paperless-ngx als Dokumentenmanagementsystem ersetzen
- Originaldokumente dauerhaft doppelt speichern
- Ein vollständiges eigenes Dokumentenarchiv aufbauen
- Ohne Berechtigung neue Tags oder Dokumenttypen erzeugen
- Unsichere KI-Ergebnisse ungeprüft überschreiben
- Ein universelles Agenten-Framework werden
- Eine umfangreiche Webanwendung voraussetzen

Der Fokus bleibt auf zuverlässiger Dokumentanalyse und kontrollierter Metadatenpflege.

## 11. Geplante Entwicklungsphasen

### Phase 1 – Funktionsfähiger Prototyp

- Paperless-Anbindung
- YAML- und Jinja2-Konfiguration
- Ollama- und Scaleway-Anbindung
- Einzelschritte für Titel, Dokumenttyp und Korrespondent
- Dry-Run-Modus
- JSON-validierte Ergebnisse
- Einfache Protokollierung

### Phase 2 – Dokumentabhängige Intelligenz

- Dokumentprofile für Rechnungen, Kontoauszüge und allgemeine Schreiben
- Vorauswahl bestehender Tags und Korrespondenten
- Custom-Field-Extraktion
- Datumsbestimmung mit Textbelegen
- Validierung und manuelle Freigabe
- Intelligente Textauswahl bei langen Dokumenten

### Phase 3 – Produktiver Archivbetrieb

- Docling/RapidOCR-Integration
- Automatische und inkrementelle Verarbeitung
- Persistente Warteschlange
- Wiederholungs- und Fehlerbehandlung
- Audit-Historie mit alten und neuen Werten
- Kostenstatistiken
- Kontrollierte Stapelverarbeitung des Bestandsarchivs

## Leitprinzip

**So wenig KI wie nötig, so viel Nachvollziehbarkeit wie möglich.**

ArchivLotse verwendet klassische Programmierung, wo eindeutige Regeln ausreichen, und Sprachmodelle dort, wo Dokumentinhalte interpretiert werden müssen.

Die KI macht Vorschläge. Die Software prüft sie. Paperless bleibt das führende Archiv.

Das Projekt soll so gestaltet sein, dass sich Regeln, Prompts und Modelle verändern lassen, ohne für jede fachliche Anpassung den Python-Code ändern zu müssen.
