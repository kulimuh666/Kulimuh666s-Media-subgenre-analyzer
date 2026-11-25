# Kulimuh666's Media Subgenre Analyzer

[Deutsch](#deutsch) · [English](#english)

## Deutsch

Schreibt Subgenres und das Jahr in Audio-Dateien. Die Dateien bleiben auf dem PC, der `start_gui.bat` startet. Hermes liefert nur die Einschätzung. Der Key in den Einstellungen ist der Hermes-Key. Instrumente gehen nach Album Artist und kommen von Jamendo, nicht von MusicNN.

In jeder Zeile: EffNet, MAEST, Beide, API und Fertig. Instrumentanalyse hört alle sichtbaren Dateien und schreibt die Instrumente nach Album Artist. Über der Liste filtern Genre, Subgenres, Album Artist und Status. * zeigt alle, ein leeres Feld nur leere Zellen. Techno enthält, -techno schließt aus. Sortierung A bis Z oder Z bis A, auch Status.

`config.example.json` nach `config.json` kopieren und Key sowie Zielordner eintragen. Modelle liegen lokal in `models/` und sind nicht im Zip.

## English

Writes subgenres and the year into audio files. The files stay on the PC that starts `start_gui.bat`. Hermes only returns the judgement. The key in Settings is the Hermes key. Instruments are written to album artist.

Each row has EffNet, MAEST, Both, API, and Finished. Instrument analysis listens to every visible file and writes the instruments to album artist. Above the list, * shows all and an empty field shows only empty cells. Sort A to Z or Z to A, including status.

Copy `config.example.json` to `config.json` and fill in the key and target folder. Models live locally in `models/` and are not in the zip.
