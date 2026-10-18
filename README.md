# Darkroom

RAW-Fotos verwalten und entwickeln: Parameter (YAML, Lightroom-Vorbild) plus optional ein Python-Script pro Foto,
das mit OpenCV / scikit-image arbeitet und von einer KI aus einer Beschreibung erzeugt werden kann.
Scripts laufen isoliert; die Ausgabedatei erzeugt immer der Server nach einem Ausgabeprofil.

## Start

```bash
cp .env.example .env        # DARKROOM_LIBRARY_PATH und ANTHROPIC_API_KEY eintragen
docker compose up --build   # http://localhost:8000  ->  "Bibliothek scannen"
```

### Deploy mit Portainer (Stack aus Git-Repository)

1. *Stacks → Add stack → Repository*. URL `https://gitlab.mikmak.cc/<benutzer>/darkroom.git`, Branch `main`,
   Compose-Pfad `docker-compose.yml`. Für ein privates Repo *Authentication* aktivieren (Benutzer + Token mit `read_repository`).
2. *Environment variables*: `DARKROOM_LIBRARY_PATH` (Pflicht), `ANTHROPIC_API_KEY`, optional `DARKROOM_DATA_PATH`, `DARKROOM_PORT`.
   Ohne `DARKROOM_DATA_PATH` liegen Parameter und Scripts in einem Docker-Volume `darkroom-data` (Backup nicht vergessen).
3. *Deploy the stack*. Portainer baut das Image aus dem Repo. Mit *GitOps updates* (Polling oder Webhook von GitLab) wird bei jedem Push neu gebaut.
4. Prüfen: Statuszeile in der Dunkelkammer muss **Sandbox: voll** zeigen, sonst sind Scripts gesperrt (siehe Abschnitt Sandbox).

Die App hat **keine Anmeldung**. Betreibe sie nur im eigenen Netz/VPN oder hinter einem Reverse Proxy mit Authentifizierung,
denn sie kann Dateien in der Foto-Bibliothek schreiben und den KI-Schlüssel nutzen.

Ohne Docker (z. B. Bazzite/Fedora, bubblewrap ist dort meist schon da):

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
DARKROOM_LIBRARY=/fotos DARKROOM_DATA=./data ANTHROPIC_API_KEY=... uvicorn darkroom.app:app
```

Tests: `python -m unittest discover -s tests` (Sandbox, Prüfungen, Ausgabe, KI-Schleife) und `pytest` (API).

## Ablauf

1. **Scan** liest RAWs und vorhandene XMPs ein und legt `data/params/<pfad>.yaml` an (nicht unterstützte Werte bleiben unter `_unmapped`).
2. **Dunkelkammer** (Doppelklick): Reiter *Parameter* (YAML) und *Script*. Vorschau links.
3. **Script**: Beschreibung eingeben, «Code erzeugen». Die KI bekommt die Regeln, der Code wird geprüft und in der Sandbox
   auf einem Testbild ausgeführt. Bei Fehlern geht die Meldung automatisch zurück an die KI (bis zu 3 Versuche).
   Den Code kannst du jederzeit selbst ändern; ausgeführt wird mit Strg+Enter oder «Vorschau».
4. **Export** (Ausgabeprofil wählen): volle Auflösung, Script, dann Farbraum/Format/Grösse nach Profil.
   Die Datei landet auch in `data/exports/<profil>/`.

## Angleichen an das Kamera-Vorschaubild

Der Button **Angleichen** im Leuchttisch startet einen Hintergrundjob für die **aktuell angezeigten Fotos** (Filter
einsetzen, um die Auswahl einzuschränken; vorher gibt es eine Rückfrage). Pro unbearbeitetem RAW sucht er Belichtung,
Kontrast, Lichter, Tiefen, Weiss, Schwarz und Schärfung so, dass das Bild dem eingebetteten JPEG der Kamera ähnlicher
wird, und schreibt die Werte in die YAML. Es entsteht **kein Script**; ein früher vom Job angelegtes Auto-Script
(`# auto_match`) wird beim erneuten Lauf entfernt.

Solange nur Werte des Jobs vorhanden sind, gilt das Foto als «unbearbeitet» (Marker `auto_match`). Bearbeitete Fotos
und Fotos mit eigenem Script fasst der Job nie an. Ohne deutliche Verbesserung (weniger als 10 %), ohne eingebettetes
Vorschaubild oder mit abweichendem Seitenverhältnis wird übersprungen. Ein Klick während des Laufs stoppt den Job.
Grenzen: nur globale Tonwerte; Farbe/Sättigung, lokaler Kontrast und Rauschreduzierung der Kamera werden nicht nachgebildet.
Experimentell: `DARKROOM_AUTOMATCH_SCRIPT=1` erzeugt zusätzlich einen Kamera-Look (Farbmatrix und Kurven) als Script.

## Objektivkorrektur

`lens: {enabled: true}` in der YAML schaltet Verzeichnung, Vignettierung und Farbsäume ein (einzeln mit `distortion`,
`vignetting`, `tca` auf `false` abschaltbar). Kamera und Objektiv kommen aus den EXIF-Daten, die Profile aus der
lensfun-Datenbank (Paket `lensfunpy`). Die Statuszeile der Dunkelkammer nennt das gefundene Objektiv oder sagt, warum
keines gefunden wurde. Das Foto wird vor Tonwerten und Crop korrigiert und mit automatischer Skalierung gefüllt.

## Grenzen

- Parameter: Belichtung, Kontrast, Lichter/Tiefen/Weiss/Schwarz, Weissabgleich, Crop mit Drehung, Schärfen, Rauschreduzierung.
  Der Look ist eine eigene Näherung und weicht von Lightroom ab. Die Umbenennung auf Lightroom-Feldnamen steht noch aus.
- XMP-Export schreibt nur die Parameter; **Scripts lassen sich nicht in XMP abbilden** (die Antwort nennt `mit_script`).
- darktable-XMPs: Rating, Tags und die Liste der Module werden gelesen, nicht die binären Parameter.
- Lightroom-Katalog (`.lrcat`) wird noch nicht gelesen. 
