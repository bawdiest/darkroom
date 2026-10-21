"""Pfade im Index ändern, nachdem Fotos auf dem Laufwerk in einen anderen Ordner der Bibliothek verschoben wurden.

Alle Metadaten hängen am Pfad relativ zur Bibliothek (Index, Parameter, Script, KI-Tags, Vorschau, Filmrollen, Kataloge, Duplikate).
Verschiebt man die Dateien von Hand, z. B. von `2024/05/IMG.CR2` nach `bawdiest/2024/05/IMG.CR2`, finden sie ihre Daten nicht mehr.
Dieses Werkzeug schreibt die Pfade um, ohne etwas an den Bilddateien zu ändern. Der Abgleich mit der Desktop-App ist nicht betroffen
(er erkennt Fotos am Inhalt, nicht am Pfad).

Aufruf (im Container oder in der venv; ohne --apply wird nur gezeigt, was passieren würde):
    python -m darkroom.relocate --to bawdiest                 # alle Fotos bekommen den Ordner «bawdiest» vorangestellt
    python -m darkroom.relocate --to bawdiest --apply
    python -m darkroom.relocate --from alt --to neu --apply   # Ordner umbenennen
    python -m darkroom.relocate --from bawdiest --apply       # Ordner wieder entfernen
    python -m darkroom.relocate --user anna --to anna ...     # für einen anderen Benutzer (Standard: Besitzer/Admin)"""
from __future__ import annotations

import argparse
import shutil
import sys
import time

from . import auth, catalog, syncstate, workspace


def _norm(p: str) -> str:
    return "/".join(x for x in (p or "").replace("\\", "/").split("/") if x not in ("", "."))


def map_path(rel: str, old: str, new: str) -> str | None:
    """Neuer Pfad für `rel`, oder None, wenn nichts zu tun ist (liegt nicht unter `old` bzw. schon unter `new`)."""
    if old:
        if rel != old and not rel.startswith(old + "/"):
            return None
        rest = rel[len(old):].lstrip("/")
    else:
        rest = rel
        if new and (rest == new or rest.startswith(new + "/")):
            return None  # liegt schon unter dem neuen Ordner
    out = f"{new}/{rest}" if new else rest
    return out if out != rel else None


def plan(con, old: str, new: str) -> dict:
    """Was würde geändert? Gibt {moves, skipped, missing, collisions} zurück (ändert nichts)."""
    moves, missing, collisions, skipped = [], [], [], 0
    for r in con.execute("SELECT * FROM photos WHERE source IS NULL ORDER BY path").fetchall():
        n = map_path(r["path"], old, new)
        if n is None:
            skipped += 1
            continue
        file_ok = (catalog.LIBRARY / n).exists()
        other = con.execute("SELECT * FROM photos WHERE path=?", (n,)).fetchone()
        moves.append({"old": r["path"], "new": n, "file_ok": file_ok, "collision": other is not None})
        if not file_ok:
            missing.append(n)
        if other is not None:
            collisions.append(n)
    return {"moves": moves, "skipped": skipped, "missing": missing, "collisions": collisions}


def _drop(con, row, cats: dict, rolls: dict) -> set[str]:
    """Entfernt ein (frisch eingelesenes, unbearbeitetes) Foto samt Duplikaten und allen Daten. Gibt die berührten Filmrollen zurück."""
    touched: set[str] = set()
    for r in con.execute("SELECT * FROM photos WHERE path=? OR source=?", (row["path"], row["path"])).fetchall():
        for f in (catalog.params_file(r["path"]), catalog.script_file(r["path"]), catalog.ai_tags_file(r["path"]),
                  catalog.THUMBS / f"{r['id']}.jpg", *catalog.VIEWS.glob(f"{r['id']}-*.jpg")):
            f.unlink(missing_ok=True)
        con.execute("DELETE FROM photos WHERE id=?", (r["id"],))
        for name, ps in rolls.items():
            if r["path"] in ps:
                touched.add(name)
        for ps in (*cats.values(), *rolls.values()):
            ps[:] = [p for p in ps if p != r["path"]]
    con.commit()
    return touched


def _prune_empty_dirs() -> None:
    for root in (catalog.PARAMS_DIR, catalog.SCRIPTS_DIR, catalog.AI_TAGS_DIR):
        if root.exists():
            for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass


def _backup(con) -> str:
    """Sichert Index, Filmrollen und Kataloge vor dem Umschreiben (Parameter-Dateien werden nur umbenannt)."""
    d = catalog.DATA / f"backup-relocate-{time.strftime('%Y%m%d-%H%M%S')}"
    d.mkdir(parents=True, exist_ok=True)
    for f in (catalog.ROLLS_FILE, catalog.CATALOGS_FILE):
        if f.exists():
            shutil.copy2(f, d / f.name)
    if catalog.DB_PATH.exists():
        shutil.copy2(catalog.DB_PATH, d / catalog.DB_PATH.name)
    return str(d)


def apply(con, old: str, new: str, allow_missing: bool = False, replace_scanned: bool = True) -> dict:
    """Schreibt die Pfade um. Bricht ab (ohne etwas zu ändern), wenn Bilddateien am neuen Ort fehlen (außer allow_missing).
    Hat ein Scan an den neuen Orten schon unbearbeitete Doppelgänger angelegt, werden sie durch die echten Einträge ersetzt
    (replace_scanned); bearbeitete Doppelgänger bleiben unangetastet und werden gemeldet."""
    p = plan(con, old, new)
    if p["missing"] and not allow_missing:
        raise SystemExit(f"{len(p['missing'])} Dateien liegen nicht am neuen Ort, z. B. {p['missing'][0]}. Nichts geändert. "
                         "Stimmt der Ordnername? (Mit --allow-missing trotzdem umschreiben.)")
    backup = _backup(con)
    cats, rolls = catalog.read_catalogs(), catalog.read_rolls()
    done, replaced, conflicts, touched_rolls = 0, 0, [], set()
    for m in p["moves"]:
        if m["collision"]:
            other = con.execute("SELECT * FROM photos WHERE path=?", (m["new"],)).fetchone()
            dups = con.execute("SELECT COUNT(*) FROM photos WHERE source=?", (m["new"],)).fetchone()[0]
            if replace_scanned and m["file_ok"] and not dups and syncstate.is_pristine(syncstate.read_dev(m["new"])):
                touched_rolls |= _drop(con, other, cats, rolls)
                replaced += 1
            else:
                conflicts.append(m["new"])
                continue
        catalog._rename_meta(con, m["old"], m["new"], cats, rolls)
        done += 1
    for name in touched_rolls:  # Filmrollen, die nur durch ersetzte Doppelgänger gefüllt waren
        if not rolls.get(name):
            rolls.pop(name, None)
    catalog.write_catalogs(cats)
    catalog.write_rolls(rolls)
    _prune_empty_dirs()
    return {"umgeschrieben": done, "ersetzt": replaced, "konflikte": conflicts, "fehlend": len(p["missing"]), "unberuehrt": p["skipped"], "sicherung": backup}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Umlaute auch in Konsolen mit anderer Kodierung (Windows)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m darkroom.relocate", description="Pfade im Index nach einem Verschieben der Fotos anpassen.")
    ap.add_argument("--from", dest="old", default="", help="bisheriger Ordner (leer = Bibliothekswurzel)")
    ap.add_argument("--to", dest="new", default="", help="neuer Ordner (leer = Bibliothekswurzel)")
    ap.add_argument("--user", help="Benutzer (Standard: Besitzer/Admin mit der ursprünglichen Bibliothek)")
    ap.add_argument("--apply", action="store_true", help="wirklich umschreiben (ohne: nur Vorschau)")
    ap.add_argument("--allow-missing", action="store_true", help="auch umschreiben, wenn Dateien am neuen Ort fehlen")
    args = ap.parse_args(argv)
    old, new = _norm(args.old), _norm(args.new)
    if old == new:
        ap.error("--from und --to sind gleich")

    ws = workspace.DEFAULT
    if args.user:
        users = {u["name"].lower(): u for u in auth.list_users()}
        u = users.get(args.user.lower())
        if not u:
            print(f"Benutzer «{args.user}» gibt es nicht. Vorhanden: {', '.join(x['name'] for x in users.values()) or '-'}", file=sys.stderr)
            return 2
        ws = auth.workspace_for({"owner": u["owner"], "slug": u["name"].lower()})
    with workspace.use(ws):
        print(f"Bibliothek: {catalog.LIBRARY}\nDaten:      {catalog.DATA}")
        con = catalog.db()
        p = plan(con, old, new)
        print(f"\n{len(p['moves'])} Fotos würden umgeschrieben, {p['skipped']} bleiben unberührt (liegen nicht unter «{old or '/'}» oder schon unter «{new or '/'}»).")
        for m in p["moves"][:5]:
            print(f"  {m['old']}  ->  {m['new']}{'' if m['file_ok'] else '   [Datei fehlt am neuen Ort]'}{'   [Eintrag dort schon vorhanden]' if m['collision'] else ''}")
        if len(p["moves"]) > 5:
            print(f"  … und {len(p['moves']) - 5} weitere")
        if p["missing"]:
            print(f"\nACHTUNG: {len(p['missing'])} Dateien liegen nicht am neuen Ort (z. B. {p['missing'][0]}).")
        if p["collisions"]:
            print(f"{len(p['collisions'])} neue Pfade haben schon einen Eintrag (z. B. durch «Bibliothek scannen» nach dem Verschieben): "
                  "unbearbeitete werden durch die echten Einträge ersetzt, bearbeitete bleiben und werden gemeldet.")
        if not args.apply:
            print("\nNur Vorschau. Mit --apply wirklich umschreiben (vorher wird Index, rolls.yaml und catalogs.yaml gesichert).")
            return 0
        if not p["moves"]:
            print("Nichts zu tun.")
            return 0
        r = apply(con, old, new, allow_missing=args.allow_missing)
        print(f"\nFertig: {r['umgeschrieben']} umgeschrieben, {r['ersetzt']} Doppelgänger ersetzt, {len(r['konflikte'])} Konflikte, Sicherung in {r['sicherung']}")
        for c in r["konflikte"][:10]:
            print(f"  Konflikt (bleibt unverändert): {c}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
