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

Die App hat eine **Anmeldung mit mehreren Benutzern** (Abschnitt «Mehrere Benutzer»). Betreibe sie trotzdem nur über https (Reverse Proxy) oder im eigenen Netz/VPN,
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

## Filmrollen, Kataloge, Duplikate und Galerie

**Galerie.** Ein Klick auf ein Foto öffnet es gross in einer Galerie-Ansicht (nicht mehr direkt die Entwicklung). Rechts stehen Dateiname, Filmrolle, Bewertung, Aufnahmedaten (Kamera, Objektiv, Brennweite, Blende, Belichtung, ISO, Auflösung, Dateigrösse), Bearbeitungsstand, Tags (eigene und KI), Kataloge und Versionen. Unten läuft ein Filmstreifen. Tasten: `←`/`→` (auch Leertaste/Rücktaste) blättern, `0`–`5` bewerten, `O` Original (Kamera-Vorschaubild) gegen bearbeitete Ansicht, `V` Duplikat erstellen, `Entf` Duplikat löschen, `Enter` in die Dunkelkammer, `Esc` zurück zum Raster. Die grosse Ansicht (bis 3200 px, JPEG 92) zeigt Parameter und Script und wird zwischengespeichert (`data/cache/views`); nach dem Speichern in der Dunkelkammer entsteht sie im Hintergrund, und die Galerie zeigt eine schon erzeugte Ansicht sofort. Unbearbeitete RAWs erscheinen sofort über das eingebettete Kamera-JPEG. Schwere Ansichten werden nacheinander gerechnet, damit die Dunkelkammer-Vorschau nicht warten muss.

**Ordnung auf dem Laufwerk.** Die Originale liegen in der Bibliothek als `JJJJ/MM/TT/Dateiname` (Aufnahmedatum aus EXIF). Der Knopf **«Ordnen»** zeigt zuerst, was verschoben würde, und sortiert nach Bestätigung alle Originale entsprechend ein. Mitgenommen werden XMP-Dateien, Parameter, Scripts, KI-Tags, Duplikate, Vorschaubilder sowie Filmrollen- und Katalog-Zuordnungen. Bei gleichem Namen am selben Tag wird `_1`, `_2` … angehängt. Fotos ohne Aufnahmedatum bleiben liegen, leere Ordner werden entfernt. Neue Dateien nach dem Scan einfach wieder mit «Ordnen» einsortieren. Der Bibliotheksordner muss dafür beschreibbar sein (`:ro` im Volume entfernen); sonst meldet «Ordnen» einen Fehler und ändert nichts.

**Filmrollen** sind benannte, virtuelle Ordner (wie in darktable): jedes Foto liegt in genau einer Rolle, unabhängig vom echten Ordner. Es gibt zwei Wege:
- **Beim Import:** «Bibliothek scannen» fragt nach einem Namen (Vorschlag «Import JJJJ-MM-TT HH:MM»); alle dabei neu gefundenen Fotos kommen in diese Rolle. Wird nichts Neues gefunden, entsteht keine Rolle.
- **Von Hand:** Fotos auswählen und oben «In Filmrolle verschieben…» wählen (bestehende Rolle, «＋ Neue Filmrolle …» oder «Aus Filmrolle nehmen»); in der Galerie geht das im Infopanel. Mit «＋» in der Seitenleiste legst du eine leere Rolle an; eine geöffnete Rolle lässt sich umbenennen oder löschen (die Fotos bleiben, unter «Ohne Filmrolle»).
Duplikate folgen der Rolle ihres Originals. Die Zuordnung steht in `data/rolls.yaml` (gehört ins Git-Repo). Fotos, die schon vor diesem Update importiert wurden, stehen zunächst unter «Ohne Filmrolle» und lassen sich mit «Alle auswählen» in einem Schritt verschieben. Zusätzlich gibt es den automatischen Baum **Nach Datum** (Jahr › Monat › Tag aus dem Aufnahmedatum) zum Blättern.

**Kataloge** sind ebenfalls virtuelle Ordner, manuelle Sammlungen über beliebig viele Filmrollen hinweg (ein Foto darf in mehreren sein). So legst du sie an: Fotos auswählen (Strg-Klick, Umschalt-Klick für Bereiche, oder das ✓ auf der Karte; die Auswahl bleibt beim Wechsel zwischen Rollen erhalten), dann oben «Zu Katalog hinzufügen…» und einen Katalog oder «＋ Neuer Katalog …» wählen. Ist ein Katalog geöffnet, kannst du ihn umbenennen, löschen oder per «Zusammenführen in …» in einen anderen Katalog übernehmen (der erste verschwindet, die Fotos bleiben, keine Doppelten). Eine ganze Filmrolle lässt sich über «Als Katalog speichern» in einen Katalog legen. Gelöscht oder zusammengeführt werden nur Kataloge, nie Fotos. Gespeichert werden sie in `data/catalogs.yaml` (gehört ins Git-Repo). Suche: `katalog:urlaub`, `ist:duplikat`.

**Duplikate** (virtuelle Kopien) haben eigene Parameter, ein eigenes Script, eigene Tags und Bewertung, teilen sich aber die Bilddatei (es wird nichts kopiert). Sie sind überall blau gestrichelt und mit «⧉ Duplikat N» markiert und stehen im Raster direkt hinter ihrem Original. Anlegen: Galerie (`V` oder Knopf), oder im Raster mehrere auswählen und «Duplizieren». Ein Duplikat startet mit dem aktuellen Stand der Vorlage und bleibt in deren Katalogen. Löschen lassen sich nur Duplikate, nie Originale. Gespeichert werden sie als `data/params/<Original>~dN.yaml` (plus Script und KI-Tags unter demselben Namen); nach einem Neuaufbau des Index stellt «Bibliothek scannen» sie daraus wieder her. Beim XMP-Export werden Duplikate übersprungen (XMP kennt pro Datei nur eine Version); der Export aus der Dunkelkammer heisst `<Name>_dN.jpg`.

## Desktop-App (Windows, macOS, Linux)

Eigenständige App für Notebook und PC: Fotos importieren, bewerten, ordnen und entwickeln **ohne Internet**; sobald der Server
erreichbar ist, gleicht sie in beide Richtungen ab, **inklusive der RAW-Dateien**. Dieselbe Oberfläche wie im Browser, die Fotos
liegen lokal (Standard `Bilder/Darkroom`, änderbar über ⚙), Einstellungen und Daten im Benutzerprofil
(`%APPDATA%\Darkroom`, `~/Library/Application Support/Darkroom`, `~/.local/share/Darkroom`).

**Was offline geht:** importieren (Knopf «Importieren…» kopiert von Speicherkarte oder Ordner nach `JJJJ/MM/TT`, XMPs werden
mitgenommen, bereits Kopiertes wird übersprungen), Bibliothek scannen, bewerten, taggen, Filmrollen, Kataloge, Duplikate, Entwicklung
über die Parameter, Angleichen, Export. **Scripts laufen lokal nie:** man kann sie schreiben und speichern, angewendet werden sie
erst auf dem Server (lokal zeigen Vorschau und Export nur die Parameter, mit Hinweis). Die KI-Funktionen gibt es nur am Server.

**Mit dem Server verbinden:** ☁-Knopf oder ⚙ → Adresse (`https://…`), Benutzername, Passwort. Die App speichert nur einen
Zugangs-Token (`sync.json` im Datenordner), nie das Passwort; der Benutzer ist ein normales Konto aus «Mehrere Benutzer».
Die App zeigt **Offline / n ausstehend / Synchronisiert**, prüft jede Minute die Erreichbarkeit und gleicht automatisch ab, wenn lokal
etwas ansteht (abschaltbar). Ein Klick auf den ☁-Knopf startet den Abgleich von Hand, während des Laufs stoppt er ihn.

**Was abgeglichen wird:** neue RAW-Dateien (in Teilen von 4 MB, ein abgebrochener Upload setzt später dort fort, Prüfsumme beim
Empfang), Parameter-YAML mit Bewertung und Tags, Scripts, KI-Tags, Filmrollen, Kataloge, Duplikate (auch das Löschen). Ein Foto
wird über den Hash seines Inhalts erkannt, nicht über den Pfad; Umbenennen und «Ordnen» stören den Abgleich also nicht.
Fotos, die nur auf dem Server liegen (z. B. vom zweiten Gerät), erscheinen mit Vorschau und «☁ Server»-Marke; **«Original laden»**
im Info-Blatt der Galerie holt die RAW-Datei (fortsetzbar, mit Prüfsumme). Nach dem Abgleich zeigt eine lokale Vorschau
von Fotos mit Script den Look, den der Server mit dem Script gerechnet hat.

**Konflikte:** Wurde dasselbe Foto hier und auf dem Server verschieden entwickelt (Parameter, Script, Bewertung, Tags), wird
nichts überschrieben. Das ⚠ oben nennt die Fotos; der Dialog zeigt beide Fassungen (nur die abweichenden Zeilen) und fragt:
**Meine behalten** (überschreibt den Server), **Server übernehmen** (deine Änderung geht verloren) oder **Beide behalten** (deine
Fassung wird ein Duplikat, das Original bekommt die Server-Fassung). Filmrolle und Kataloge fügt die App selbst zusammen (Kataloge
als Vereinigung, abzüglich dessen, was auf einer Seite entfernt wurde; bei verschiedenen Filmrollen gilt die des Servers).
Beim ersten Abgleich einer Datei, die auf beiden Seiten schon liegt, gewinnt die bearbeitete Seite, wenn die andere noch
unverändert ist; nur wenn beide bearbeitet sind, gibt es die Frage.

**Server-Voraussetzungen:** keine neuen Einstellungen. Hinter einem Reverse Proxy muss die maximale Anfragegrösse mindestens
8 MB betragen (nginx: `client_max_body_size 8m;`, Standard wäre 1 MB). Obergrenze je Datei `DARKROOM_MAX_UPLOAD_GB` (Standard 2).
Server und App sollten über https laufen; bei `http://` warnt die App.

**Sicherheit:** Der lokale Server der App hört nur auf `127.0.0.1` an einem zufälligen Port und akzeptiert Anfragen nur mit einem
Schlüssel, den das App-Fenster bei jedem Start neu bekommt (Cookie, `SameSite=Strict`, dazu eine Prüfung des Host-Namens gegen
DNS-Rebinding). Eine Webseite im normalen Browser kommt so weder an die Fotos noch kann sie den Abgleich umlenken.

**Bauen und installieren.** Ein Betriebssystem baut nur für sich selbst (PyInstaller, kein Cross-Build):

```bash
# Windows (PowerShell, Python 3.12 x64):  Ergebnis dist\Darkroom\Darkroom.exe, .zip, mit Inno Setup auch ...-setup.exe
powershell -ExecutionPolicy Bypass -File desktop\build.ps1
# macOS / Linux:  Ergebnis dist/Darkroom.app + .dmg bzw. dist/Darkroom + .tar.gz
bash desktop/build.sh
```

Das Skript installiert die Abhängigkeiten in `.venv-build`, baut und startet zur Probe `Darkroom --smoke-test`. `.gitlab-ci.yml`
baut bei einem Tag `v*` oder per Play-Knopf auf Runnern mit den Tags `windows` bzw. `macos` und auf jedem Docker-Runner für Linux.
Aus dem Quellbaum startet man die App mit `python desktop/main.py` (Optionen `--library`, `--data`, `--browser`).

- **Windows on ARM:** Es gibt zwei Windows-Versionen (`Darkroom-<Version>-windows-x64` und `-windows-arm64`).
  Die **x64**-Version läuft überall, auch auf ARM-Rechnern per Emulation, mit vollem Funktionsumfang (OpenCV, Objektivkorrektur).
  Die **arm64**-Version läuft nativ und ist dort schneller, hat aber zwei Einschränkungen: Für OpenCV und lensfunpy gibt es keine
  ARM64-Pakete, und das Fenster ist ein Edge-Anwendungsfenster (ohne Tabs und Adressleiste, Edge ist auf jedem Windows 10/11
  vorhanden) statt eines pywebview-Fensters, denn .NET/pythonnet und QtWebEngine laufen auf ARM64 nicht. Beim Schliessen des
  Fensters beendet sich die App; «Ordner wählen» fragt dort nach dem Pfad als Text. OpenCV ersetzt `darkroom/cvx.py` (PIL, SciPy, scikit-image; `tests/test_cvx.py` vergleicht jede Funktion mit
  OpenCV: Drehen, Weichzeichnen, Skalieren stimmen praktisch überein, die Rauschreduzierung rechnet im gleichen Verfahren etwas
  anders und ist langsamer, ca. 9 s statt 1 s bei 1,5 Megapixeln), und die **Objektivkorrektur fehlt**. Gebaut wird sie mit
  `desktop\build.ps1 -Python <Pfad zu Python 3.12 ARM64>` auf einem ARM-Rechner; die Architektur erkennt das Skript am Python.
  GitHubs ARM-Rechner stehen nur öffentlichen Repos zur Verfügung: für das Release dort die Repo-Variable `BUILD_WINDOWS_ARM=true`
  setzen, sonst bleibt dieser Job aus.
- **Windows (alle):** nicht signiert, SmartScreen meldet «Unbekannter Herausgeber» → «Weitere Informationen» → «Trotzdem ausführen».
  Fehlermeldungen der fensterlosen App stehen in `%APPDATA%\Darkroom\darkroom.log`.
  Das Fenster nutzt WebView2 (bei Windows 10/11 vorhanden).
- **macOS:** nicht signiert/notarisiert: beim ersten Start Rechtsklick → «Öffnen», oder `xattr -dr com.apple.quarantine Darkroom.app`.
- **Linux:** Qt liegt dabei (kein System-WebKit nötig); auf manchen Distributionen fehlen `libxcb-cursor0` o. Ä.
  Ohne Fenstersystem öffnet `--browser` den Standardbrowser.
- Signieren/Notarisieren und automatische Updates sind nicht enthalten.

## Mobile Ansicht

Auf schmalen Bildschirmen (Handy, bis 820 px Breite, auch im Querformat) schaltet dieselbe Seite automatisch auf eine mobile Ansicht um (`?mobile=1` erzwingt sie, `?mobile=0` schaltet sie ab). Es gibt nur **Leuchttisch** und **Galerie**; Dunkelkammer, Scan, Ordnen, Angleichen, KI-Tags und XMP-Export bleiben dem Desktop vorbehalten.
- **Leuchttisch:** ☰ öffnet Filmrollen, Kataloge, «Nach Datum» und die Filter als Seitenmenü; die Suche liegt darunter in voller Breite. Raster mit grossen Kacheln; Tippen öffnet die Galerie, der Kreis oben rechts auf der Kachel wählt aus (dann «⇪ Teilen» für mehrere Fotos, bis 30). Verändern lässt sich auf dem Handy nichts ausser der Bewertung: keine Tags, Verschieben, Katalogpflege oder Duplikate.
- **Teilen / Export:** «⇪ Teilen» (Galerie oder Auswahl) rendert die Fotos so, wie sie in der Galerie aussehen, im gewählten Profil (Standard **WhatsApp**: 1600 px, JPEG 85 %; ausserdem «Gross» 3000 px und alle eigenen sRGB-JPEG-Profile bis 3200 px) und öffnet das Teilen-Menü des Handys. Das geht nur über **https://** (Reverse Proxy); über http werden die Dateien stattdessen heruntergeladen.
- **Galerie:** Bild füllt den Bildschirm. Wischen nach links/rechts blättert, Doppeltippen oder Zweifinger-Zoom vergrössert (verschieben mit einem Finger), ⓘ blendet die Angaben als Blatt von unten ein (Bewertung zum Antippen, Tags, Filmrolle, Kataloge, Aufnahmedaten), «Original» wechselt zum Kamera-Vorschaubild.

## Mehrere Benutzer

Beim ersten Aufruf legst du den **Admin** an; er behält die bisherige Bibliothek und alle bisherigen Daten (nichts wird verschoben oder kopiert). Der Admin legt über das Konto-Symbol oben rechts weitere Benutzer an (Name, Passwort, optional Admin), setzt Passwörter zurück und löscht Konten. Ohne Anmeldung gibt es keinen Zugriff auf die Daten (nur `/api/status` für den Healthcheck bleibt offen).

- **Getrennt pro Benutzer:** Fotos, Parameter, Scripts, Tags, KI-Tags, Bewertungen, Filmrollen, Kataloge, Duplikate, Presets, Vorschauen, Exporte und die Hintergrundjobs (Angleichen, KI-Tags). Niemand sieht oder ändert die Daten der anderen.
- **Wo liegt was:** weitere Benutzer bekommen `<DARKROOM_USERS_LIBRARY>/<name>` für die Fotos (Standard: `<DARKROOM_LIBRARY>/_users/<name>`, wird beim Scan des Admins übersprungen) und `<DARKROOM_DATA>/users/<name>` für ihre Daten. Kopiere die Fotos eines Benutzers in seinen Ordner und lass ihn «Bibliothek scannen» drücken.
- **Gemeinsam:** der KI-Schlüssel (alle Benutzer nutzen dieselben Kosten), die Sandbox und der Server.
- **Passwörter** werden mit scrypt gespeichert, Sitzungen laufen 30 Tage (Cookie, nur über https mit «Secure»), nach 5 Fehlversuchen gilt eine Pause von 5 Minuten. Die Zugangsdaten liegen in `<DARKROOM_DATA>/auth.db`: **nicht ins Git-Repo einchecken** (in die `.gitignore` des Datenordners aufnehmen) und mit sichern.
- **Gelöschte Konten** verlieren nur ihren Zugang; Fotos und Daten bleiben auf dem Laufwerk.
- **Anmeldung abschalten** (ein Benutzer wie früher, z. B. hinter einem Proxy mit eigener Authentifizierung): `DARKROOM_AUTH=off`.
- **Passwort vergessen:** Ein anderer Admin setzt es zurück. Ist es der einzige Admin, lösche `auth.db` (dann wird beim nächsten Aufruf wieder ein Admin angelegt, der die bisherigen Ordner übernimmt; Daten der weiteren Benutzer bleiben liegen, ihre Konten musst du neu anlegen).

## Ausgabeprofile

Standard: `whatsapp`, `handy_gross`, `web`, `voll_srgb`, `druck_adobergb`, `archiv_tiff`. Eigene in `data/output_profiles.yaml`:

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
