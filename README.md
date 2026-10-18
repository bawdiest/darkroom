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

## Script-Vertrag

```python
def process(img, p):
    # img: float32 (H, W, 3), RGB, 0..1, sRGB-kodiert, aus dem RAW mit den YAML-Parametern entwickelt
    # p:   Parameter-Dict; p["_scale"] = 1.0 bei voller Auflösung, kleiner in der Vorschau
    #      -> alle Pixelgrössen (Radius, Sigma, Kernel) mit p["_scale"] multiplizieren
    return img   # float32 (H2, W2, 3), 0..1, H2 <= H, W2 <= W, nur endliche Werte
```

Erlaubt: `numpy`, `cv2`, `skimage`, `scipy`, `math`, `functools`, `itertools`, `typing` (`np` und `cv2` sind vorbelegt).
Verboten: jeder Dateizugriff (`open`, `imread`, `np.load`, `skimage.io` …), `eval`/`exec`, Doppel-Unterstrich-Namen,
andere Importe. Die vollständige Liste steht in `darkroom/policy.py` und wird von Prüfung, Sandbox **und** KI-Prompt gemeinsam benutzt.

Scripts werden in `data/scripts/<foto>.py` gespeichert, wiederverwendbare in `data/script_presets/`.
Mehr Bibliotheken: Name in `ALLOWED_IMPORTS` (policy.py) ergänzen und installieren.

## Sandbox

Das Script bekommt nur ein temporäres Job-Verzeichnis mit dem einen Foto (als Array), kein Netzwerk, Limits für Zeit und Dateigrösse.
Den Speicher begrenzt der Container (`mem_limit` in `docker-compose.yml`, Variable `DARKROOM_MEMORY`); ein Adressraum-Limit
im Script-Prozess ist aus, weil es OpenCV beim Import abstürzen lässt (`DARKROOM_RLIMIT_AS=1` schaltet es wieder ein).

| Stufe | Wirkung | Wann |
|---|---|---|
| `bwrap` | Netzwerk, Prozesse und Dateisystem getrennt | bubblewrap installiert und funktionsfähig |
| `unshare` | nur Netzwerk getrennt; Dateizugriff nur durch statische Prüfung und eingeschränkte Builtins | Fallback |
| `none` | keine Isolation | nie ohne ausdrückliche Einstellung |

In Docker braucht `bwrap` die `security_opt`-Einträge der `docker-compose.yml`; ohne `/proc`-Neumontage (in Docker maskiert) arbeitet es automatisch weiter, erzwingbar mit `DARKROOM_BWRAP_PROC=0`.
Standardmässig laufen Scripts **nur mit `bwrap`**. Die Statuszeile in der Dunkelkammer zeigt die aktive Stufe.
`DARKROOM_SANDBOX_MIN=unshare` erlaubt den Fallback bewusst. Die statische Prüfung ist Komfort, kein Schutz.
Diagnose im Container: `python -m darkroom.sandbox_diag`.

## KI

`DARKROOM_AI_PROVIDER=claude` (Standard, `ANTHROPIC_API_KEY`, Modell via `DARKROOM_AI_MODEL`) oder `ollama`
(`OLLAMA_URL`, `OLLAMA_MODEL`; für Bilder optional `OLLAMA_VISION_MODEL`, z. B. `qwen2.5vl`).

- **Script-Erzeugung:** Gesendet werden nur deine Beschreibung, die YAML-Parameter und ein vorhandenes Script; kein Bild.
- **KI-Tags (Button «KI-Tags»):** Gesendet wird **pro Foto das Vorschaubild (ca. 640 px)**, bei Claude also an Anthropic.
  Das passiert nur nach Rückfrage und nur für die angezeigten Fotos. Mit Ollama und einem Vision-Modell bleibt alles im eigenen Netz.

## KI-Tags und Suche

Der Button **KI-Tags** startet einen Hintergrundjob für die **aktuell angezeigten Fotos** (Suche/Filter einsetzen, um die Auswahl
zu begrenzen). Das Modell wählt pro Foto 1 bis 6 Tags aus einer festen Liste (Portrait, Landschaft, Auto, Schnee, Berge,
Architektur, Natur …) und darf höchstens zwei eigene Wörter ergänzen. Eigene Wörter für die Liste: `data/tag_vocabulary.txt`
(ein Tag pro Zeile). Die Tags liegen in `data/ai_tags/<foto>.json` (versionierbar, die YAML bleibt unberührt). Bereits getaggte
Fotos werden übersprungen. Nach drei Fehlern in Folge (z. B. ungültiger Schlüssel) stoppt der Job mit einer Meldung.

Das grosse **Suchfeld** oben (Taste `/`) durchsucht Tags (eigene und KI), Dateiname und Ordner, Kamera, Objektiv und Datum.
Mehrere Wörter müssen alle passen. Beispiele: `berge 2024 canon`, `"Canon EOS R5" schnee`, `mai 2024`, `tag:portrait kamera:sony`,
`datum:2023-06..2023-08`, `iso:>=1600`, `brennweite:24-70`, `bewertung:>=3`, `-auto` (ausschliessen), `ist:ungetaggt`,
`ist:bearbeitet`. Über der Übersicht stehen die häufigsten Tags als Schaltflächen zum Anklicken.

## Ausgabeprofile

Standard: `web`, `voll_srgb`, `druck_adobergb`, `archiv_tiff`. Eigene in `data/output_profiles.yaml`:

```yaml
mein_profil: { format: jpeg, color_space: adobergb, quality: 92, max_side: 4000 }
```

JPEG bettet immer ein ICC-Profil ein (sRGB oder Adobe RGB). TIFF 16 Bit ist vorerst nur mit sRGB möglich.

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
