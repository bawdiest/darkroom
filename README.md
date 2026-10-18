# Darkroom

RAW-Fotos verwalten und entwickeln: Parameter (YAML, Lightroom-Vorbild) plus optional ein Python-Script pro Foto,
das mit OpenCV / scikit-image arbeitet und von einer KI aus einer Beschreibung erzeugt werden kann.
Scripts laufen isoliert; die Ausgabedatei erzeugt immer der Server nach einem Ausgabeprofil.

## Start

```bash
cp .env.example .env        # ANTHROPIC_API_KEY eintragen
# docker-compose.yml: /pfad/zu/deinen/fotos anpassen
docker compose up --build   # http://localhost:8000  ->  "Bibliothek scannen"
cd data && git init         # Parameter, Scripts, Presets versionieren
```

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

Das Script bekommt nur ein temporäres Job-Verzeichnis mit dem einen Foto (als Array), kein Netzwerk, Limits für Zeit und Speicher.

| Stufe | Wirkung | Wann |
|---|---|---|
| `bwrap` | Netzwerk, Prozesse und Dateisystem getrennt | bubblewrap installiert und funktionsfähig |
| `unshare` | nur Netzwerk getrennt; Dateizugriff nur durch statische Prüfung und eingeschränkte Builtins | Fallback |
| `none` | keine Isolation | nie ohne ausdrückliche Einstellung |

Standardmässig laufen Scripts **nur mit `bwrap`**. Die Statuszeile in der Dunkelkammer zeigt die aktive Stufe.
`DARKROOM_SANDBOX_MIN=unshare` erlaubt den Fallback bewusst. Die statische Prüfung ist Komfort, kein Schutz.

## KI

`DARKROOM_AI_PROVIDER=claude` (Standard, `ANTHROPIC_API_KEY`, Modell via `DARKROOM_AI_MODEL`) oder `ollama`
(`OLLAMA_URL`, `OLLAMA_MODEL`). Gesendet werden nur deine Beschreibung, die YAML-Parameter und ein vorhandenes Script.
Das Foto verlässt den Server nicht.

## Ausgabeprofile

Standard: `web`, `voll_srgb`, `druck_adobergb`, `archiv_tiff`. Eigene in `data/output_profiles.yaml`:

```yaml
mein_profil: { format: jpeg, color_space: adobergb, quality: 92, max_side: 4000 }
```

JPEG bettet immer ein ICC-Profil ein (sRGB oder Adobe RGB). TIFF 16 Bit ist vorerst nur mit sRGB möglich.

## Grenzen

- Parameter: Belichtung, Kontrast, Lichter/Tiefen/Weiss/Schwarz, Weissabgleich, Crop mit Drehung, Schärfen, Rauschreduzierung.
  Der Look ist eine eigene Näherung und weicht von Lightroom ab. Die Umbenennung auf Lightroom-Feldnamen steht noch aus.
- XMP-Export schreibt nur die Parameter; **Scripts lassen sich nicht in XMP abbilden** (die Antwort nennt `mit_script`).
- darktable-XMPs: Rating, Tags und die Liste der Module werden gelesen, nicht die binären Parameter.
- Lightroom-Katalog (`.lrcat`) wird noch nicht gelesen. Objektivprofil wird importiert/exportiert, aber nicht gerendert.
