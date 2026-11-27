# Kulimuh666's Media Subgenre Analyzer

[Deutsch](#deutsch) · [English](#english)

---

## Deutsch

Schreibt Subgenres und das Jahr in Audio-Dateien. Die Dateien bleiben auf dem PC, der `start_gui.bat` startet. Hermes liefert nur die Einschätzung. Instrumente gehen nach Album Artist (TPE2), nicht nach User 1.

### Screenshots

**Werkzeug-Oberfläche (GUI)** — Screenshot auf Grundlage vereinfachter Demo-Daten (keine echten Tracks/Artists/Pfade: Ziel-Ordner und Log sind aus Platzgründen geleert; die drei Zeilen sind fiktive Demo-Tracks):

![GUI-Screenshot](Screenshots/gui.png)

**Einstellungen-Seite** — Screenshot mit maskierten Schlüsseln (hermes_url, api_key, pythonPath) und ohne Werte; nur Struktur und Buttons sind sichtbar:

![Einstellungs-Screenshot](Screenshots/settings.png)

### Was welcher Button macht

Die **ERGEBNISSE-Queryzeile = Row** (Filter- und Sortier-Leiste + Tabellenzeilen) beschreibt alles in der Ergebnis-Ansicht — den Filter-/Sortierkopf und die Ein-Zeile-pro-Track-Zeilen.

#### Kopfzeile
| Element | Funktion |
|---|---|
| Einstellungen | öffnet die Einstellungen (`settings.html`) |
| Hilfe (?) | öffnet die Hilfe (`hilfe.html`) |
| Versionen | öffnet die Versionshistorie (`versionen.html`) |
| GitHub | öffnet das Repository |
| DE / EN (Flags) | wechselt UI-Sprache (Deutsch ↔ English) |
| Versions- / Hermes-Bridge-Badge | zeigt Version und Verbindungsstatus der Hermes-Bridge (getrennt/verbunden) |

#### LAUF-Panel (links oben)
| Element | Funktion |
|---|---|
| Ziel-Ordner (Input) | der Ordner mit den Audio-Dateien |
| Ordner wählen | native Folder-Picker — setzt den Ordner und lädt die Dateien |
| Limit (max. Tracks) | max. Anzahl geladener Dateien |
| Alle analysieren | analysiert alle sichtbaren Tracks |
| Markierte analysieren | analysiert nur die markierten Zeilen (Checkbox) |
| Instrumentanalyse | hört alle sichtbaren Dateien und schreibt die Instrumente nach Album Artist (TPE2) |
| Stop | stoppt eine laufende Analyse |
| Aktualisieren | lädt Config neu und aktualisiert die Zeilen |
| Neue Daten laden | erzwingt eine Neuladung der Trackliste (Rescan) |
| Log leeren | leert die Konsole |

#### Queryzeile = Row (Filter- und Sortier-Leiste + Tabellenzeilen)
| Element | Funktion |
|---|---|
| Filter Genre / Subgenres / Album Artist / Status | `*` zeigt alle, leeres Feld nur leere Zellen; Techno enthält, `-techno` schließt aus (inkl. Peak Time Techno / Driving Techno / Psytrance) |
| Sortierung | Keine / Datei / Genre / Subgenres / Album Artist / Jahr / Status |
| Richtung | A → Z (aufsteigend) / Z → A (absteigend) |
| Reihenfolge festhalten | friert die aktuelle Sortierung ein, statt sie bei jeder Auto-Aktualisierung neu zu mischen |
| Checkbox | Auswahl für „Markierte analysieren" |
| # | Zeilennummer (sortiert) |
| DATEI | Dateiname (Hover: voller Pfad) |
| GENRE / SUBGENRES / ALBUM ARTIST / JAHR | ID3-Zellen — ✏️ Stift-Button öffnet das Bearbeiten-Modal für die Zelle |
| STATUS | Status-Badge (z. B. „Überspringen" = fertiger Track, „Wartend" = noch zu analysieren) |
| EffNet | analysiert den Track mit dem lokalen EffNet-Modell (Genre) |
| MAEST | analysiert mit dem lokalen MAEST-Modell (Genre) |
| Beide | EffNet + MAEST, zusammengefasst |
| API | sendet den Track an den Hermes-API (LLM-Einschätzung, schreibt Jahr/Subgenres) |
| Instrument | per-Track Instrumentanalyse → schreibt nach Album Artist (TPE2) |
| ▶ / Ordner / Genre | ▶️ Track abspielen · 📁 in Dateimanager zeigen · ✏️ Subgenre-Bearbeiten öffnen |

#### Konsole
Zeigt die Aktivität des Backends (Config-Load, Trackliste, Analyse).

#### Player-Leiste (unten)
| Element | Funktion |
|---|---|
| ▶️ | Play/Pause |
| Zeit + Fortschrittsbalken | Position anzeigen / springen |
| Lautsprecher | Mute/Lautstärke |
| ⋯ | Player-Optionen |

#### Einstellungen-Seite
| Element | Funktion |
|---|---|
| Hermes-API-Adresse + API-Key | Ziel des LLM |
| Worker | max. parallele Analysen (1/2/3/5) |
| Timeout | maximale Wartezeit auf die Hermes-Antwort |
| Modus | Websuche / Direkt (kleines LLM, nur Tags + Dateiname) / Audio (hört Datei) |
| Lokales Hör-Modell | EffNet / MAEST / Beide, zusammengefasst |
| Subgenres in Tag | Ziel-Tag für Subgenres (Composer TCOM / Genre TCON / Artist TPE1 / Album Artist TPE2 / Conductor TPE3 / Title TIT2 / TIT1 / Album TALB / Custom TXXX) |
| Instrumente in Tag | Ziel-Tag für Instrumente (identisch) |
| Python für Audio + Python suchen | Python-Interpreter mit mutagen (z. B. 3.11–3.14) |
| Direkte Analyse über + Modelle | Hermes (API) / lokale llama.cpp (RTX, models/) + 3B / 1.5B / Audio-Modelle |
| Zweite LLM zum Kürzen (Checkbox) | separate Zusammenfassung |
| Runtime laden | lädt die lokale llama.cpp-Engine |
| Audio-Modelle laden | lädt EffNet/MAEST (.npz) |
| 3B laden / 1.5B laden | lädt das jeweilige LLM-Modell |
| Lokal starten | startet den lokalen llama.cpp-Server |
| API testen | testet die Hermes-Verbindung |
| Speichern | speichert die Einstellungen |
| Hilfe (?) | öffnet die Hilfe |
| DE / EN (Flags) | UI-Sprache |

### Einrichten
`config.example.json` nach `config.json` kopieren und Key sowie Zielordner eintragen. Modelle liegen lokal in `models/` und sind nicht im Zip. Release-Zip und Git ohne Key, LAN-Adresse, Benutzerpfade, Modelle, Musik oder `config.json`.

## English

Writes subgenres and the year into audio files. Files stay on the PC that starts `start_gui.bat`. Hermes only returns the judgement. Instruments go to album artist (TPE2).

### Screenshots
Same as German section above — GUI screenshot uses fictional demo data (target folder and log are blanked), settings screenshot masks secrets.

### Buttons — short frame
- **Header:** Einstellungen · Hilfe · Versionen · GitHub · DE/EN · version/status pills
- **LAUF:** target folder (+ folder picker), limit, Alle analysieren, Markierte analysieren, Instrumentanalyse, Stop, Aktualisieren, Neue Daten laden, Log leeren
- **Queryzeile = Row (filter/sort + per-track rows):** filter Genre / Subgenres / Album Artist / Status (`*` = all, empty = empty cells, `Techno` includes, `-techno` excludes), Sortierung, Richtung (A→Z / Z→A), Reihenfolge festhalten, checkbox, #, DATEI, GENRE / SUBGENRES / ALBUM ARTIST / JAHR (✏️ per cell), STATUS, EffNet · MAEST · Beide · API · Instrument · ▶ · Ordner · Genre
- **Konsole + Player:** log strip · ▶ / time / volume / ⋯
- **Einstellungen-Seite:** Hermes-Adresse/Key, Worker, Timeout, Modus, lokale Modelle, Python, Runtime/Audio-Modelle, 3B/1.5B, Lokal starten, API testen, Speichern

Copy `config.example.json` to `config.json` and fill in the key and target folder. Models live locally in `models/` and are not in the zip.
