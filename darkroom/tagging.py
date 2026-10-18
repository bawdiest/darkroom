"""KI-Tagging im Hintergrund: schickt das Vorschaubild an ein Vision-Modell und speichert Schlagwörter.

Datenschutz: Beim Claude-Anbieter verlässt das Vorschaubild (ca. 640 px) den Server. Mit DARKROOM_AI_PROVIDER=ollama und
einem Vision-Modell bleibt alles im eigenen Netz. Die Tags landen pro Foto in data/ai_tags/<pfad>.json und im Index;
die YAML-Parameter bleiben unberührt.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ai, catalog

# Wörter, aus denen das Modell auswählen soll. Eigene Wörter in data/tag_vocabulary.txt (ein Tag pro Zeile) ergänzen sie.
VOCABULARY = [
    "Portrait", "Menschen", "Gruppe", "Kinder", "Landschaft", "Natur", "Berge", "Schnee", "Wald", "See", "Meer", "Strand",
    "Fluss", "Wasser", "Himmel", "Wolken", "Sonnenuntergang", "Sonnenaufgang", "Nacht", "Architektur", "Stadt", "Strasse",
    "Gebäude", "Brücke", "Kirche", "Innenraum", "Auto", "Fahrzeug", "Motorrad", "Zug", "Flugzeug", "Boot", "Segeln", "Tiere",
    "Vögel", "Haustier", "Blumen", "Pflanzen", "Makro", "Detail", "Essen", "Veranstaltung", "Sport", "Reise", "Schwarzweiss",
    "Dokument", "Bildschirm",
]
MAX_TAGS = 6
MAX_EXTRA = 2
SYSTEM = """Du vergibst Schlagwörter (Tags) für Fotos in einer Foto-Verwaltung. Antworte NUR mit einem JSON-Objekt der Form
{"tags": ["Tag1", "Tag2"]}, ohne weiteren Text.

Regeln:
- Wähle 1 bis %(max)d Tags, die das Bild am besten beschreiben, vorzugsweise aus dieser Liste: %(vocab)s.
- Nur wenn die Liste etwas Wichtiges nicht abdeckt, darfst du bis zu %(extra)d zusätzliche deutsche Tags vergeben (ein Hauptwort, mit Grossbuchstaben).
- Beschreibe nur, was sichtbar ist. Keine Namen von Personen, keine Vermutungen über Identität, Alter oder Herkunft, keine Bewertungen.
- «Portrait» nur, wenn eine Person das Hauptmotiv ist. «Landschaft» für weite Naturaufnahmen.
"""
PROMPT = "Vergib Tags für dieses Foto."
_WORD = re.compile(r"^[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß\- ]{2,23}$")


def vocabulary() -> list[str]:
    extra = []
    f = catalog.DATA / "tag_vocabulary.txt"
    if f.exists():
        extra = [x.strip() for x in f.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]
    return list(dict.fromkeys([*VOCABULARY, *extra]))


def system_prompt() -> str:
    return SYSTEM % {"max": MAX_TAGS, "extra": MAX_EXTRA, "vocab": ", ".join(vocabulary())}


def parse_tags(reply: str) -> list[str]:
    """Liest die Antwort des Modells tolerant (auch mit Text oder Codeblock drumherum) und normalisiert auf das Vokabular."""
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        raw = json.loads(m.group(0))["tags"] if m else json.loads(reply)
    except (ValueError, KeyError, TypeError):
        m = re.search(r"\[.*?\]", reply, re.S)
        try:
            raw = json.loads(m.group(0)) if m else []
        except ValueError:
            raw = []
    vocab = {v.casefold(): v for v in vocabulary()}
    out, extra = [], 0
    for t in raw if isinstance(raw, list) else []:
        t = str(t).strip()
        if t.casefold() in vocab:
            tag = vocab[t.casefold()]
        elif _WORD.match(t) and extra < MAX_EXTRA:
            tag, extra = t[:1].upper() + t[1:], extra + 1
        else:
            continue
        if tag not in out:
            out.append(tag)
    return out[:MAX_TAGS + MAX_EXTRA]


def tag_photo(provider, row: dict) -> list[str]:
    thumb = catalog.THUMBS / f"{row['id']}.jpg"
    if not thumb.exists():
        raise ai.AIError("Vorschaubild fehlt (zuerst scannen)")
    return parse_tags(provider.describe_image(system_prompt(), PROMPT, thumb.read_bytes()))


def candidates(ids: list[str] | None, redo: bool = False) -> list[dict]:
    wanted = set(ids) if ids is not None else None
    out = []
    for row in catalog.db().execute("SELECT id, path, ai_tags FROM photos ORDER BY path"):
        if wanted is not None and row["id"] not in wanted:
            continue
        if not redo and catalog.read_ai_tags(row["path"]) is not None:
            continue
        out.append({"id": row["id"], "path": row["path"]})
    return out


class TagJob:
    MAX_CONSECUTIVE_ERRORS = 3

    def __init__(self, provider_factory=ai.get_provider):
        self.provider_factory = provider_factory
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.state = {"running": False, "total": 0, "done": 0, "getaggt": 0, "fehler": 0, "aktuell": "", "meldung": "",
                      "gestartet": None, "beendet": None}

    def start(self, ids: list[str] | None = None, redo: bool = False) -> bool:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            try:
                provider = self.provider_factory()
            except ai.AIError as e:
                self.state.update(running=False, total=0, done=0, meldung=str(e))
                return False
            todo = candidates(ids, redo)
            self.stop_flag.clear()
            self.state.update(running=bool(todo), total=len(todo), done=0, getaggt=0, fehler=0, aktuell="", meldung="",
                              gestartet=time.time(), beendet=None)
            if not todo:
                return True
            self.thread = threading.Thread(target=self._run, args=(provider, todo), daemon=True, name="ai-tagging")
            self.thread.start()
            return True

    def stop(self) -> None:
        self.stop_flag.set()

    def _run(self, provider, todo: list[dict]) -> None:
        errors_in_a_row = 0
        try:
            for row in todo:
                if self.stop_flag.is_set():
                    break
                self.state["aktuell"] = row["path"]
                try:
                    tags = tag_photo(provider, row)
                    catalog.write_ai_tags(row["id"], row["path"], tags, {
                        "provider": getattr(provider, "name", "?"), "model": getattr(provider, "model", ""),
                        "date": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                    self.state["getaggt"] += 1
                    errors_in_a_row = 0
                except Exception as e:  # noqa: BLE001
                    self.state["fehler"] += 1
                    self.state["meldung"] = f"{row['path']}: {e}"
                    errors_in_a_row += 1
                    if errors_in_a_row >= self.MAX_CONSECUTIVE_ERRORS:  # z. B. ungültiger Schlüssel, Dienst nicht erreichbar
                        self.state["meldung"] += " (abgebrochen nach mehreren Fehlern in Folge)"
                        break
                self.state["done"] += 1
        finally:
            self.state.update(running=False, aktuell="", beendet=time.time())


JOB = TagJob()
