# Kulimuh666's Media Subgenre Analyzer

[Deutsch](#deutsch) · [English](#english)

## Deutsch

Schreibt Subgenres und das Jahr in Audio-Dateien. Die Dateien bleiben auf dem PC, der `start_gui.bat` startet. Hermes liefert nur die Einschätzung. Der Key in den Einstellungen ist der Hermes-Key. Python 3.15 hat kein onnxruntime. EffNet nimmt 3.11 bis 3.14, sonst installiert der Server 3.12.

In jeder Zeile: EffNet, MAEST, Beide, API und Fertig. Fertig nimmt den Track aus der Liste und lädt den nächsten. Fertige ignorieren beim Laden überspringt Dateien mit Genre oder Komponist. Alle analysieren und Markierte analysieren nehmen das Hör-Modell aus den Einstellungen. Einstellungen, Hilfe und Versionen öffnen ein eigenes Fenster. Genre, Subgenres und die Instrument-Spalte sind mit dem Stift bearbeitbar.

`config.example.json` nach `config.json` kopieren und Key sowie Zielordner eintragen. Modelle liegen lokal in `models/` und sind nicht im Zip.

## English

Writes subgenres and the year into audio files. The files stay on the PC that starts `start_gui.bat`. Hermes only returns the judgement. The key in Settings is the Hermes key. Python 3.15 has no onnxruntime. EffNet uses 3.11 to 3.14, otherwise the server installs 3.12.

Each row has EffNet, MAEST, Both, API, and Finished. Finished removes that track and loads the next one. Ignore finished when loading skips files that already have a genre or composer. Analyze all and Analyze marked use the listening model from Settings. Settings, Help and Versions open their own window. Genre, subgenres, and the instrument column can be edited with the pencil.

Copy `config.example.json` to `config.json` and fill in the key and target folder. Models live locally in `models/` and are not in the zip.
