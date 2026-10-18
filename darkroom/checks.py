"""Statische Prüfung von Scripts und Prüfung der Ausgabe."""
from __future__ import annotations

import ast

import numpy as np

from . import policy


class ScriptError(Exception):
    """Fehler im Script oder in dessen Ausgabe. Die Meldung ist für den Benutzer bzw. die KI gedacht."""


class SandboxError(ScriptError):
    """Die Sandbox selbst funktioniert nicht (nicht das Script). Das darf nicht an die KI zurückgegeben werden."""


def check_script(code: str) -> list[str]:
    """Gibt eine Liste von Problemen zurück (leer = in Ordnung). Das ist Komfort und kein Schutz;
    der Schutz ist die Sandbox."""
    if len(code.encode("utf-8")) > policy.MAX_SCRIPT_BYTES:
        return [f"Script ist zu gross (maximal {policy.MAX_SCRIPT_BYTES} Bytes)."]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Syntaxfehler in Zeile {e.lineno}: {e.msg}"]

    problems: list[str] = []
    allowed = set(policy.ALLOWED_IMPORTS)

    def bad_module(name: str, line: int):
        top = name.split(".")[0]
        if top not in allowed:
            problems.append(f"Zeile {line}: Import von '{name}' ist nicht erlaubt (erlaubt: {', '.join(policy.ALLOWED_IMPORTS)}).")
            return
        for pre in policy.FORBIDDEN_MODULE_PREFIXES:
            if name == pre or name.startswith(pre + "."):
                problems.append(f"Zeile {line}: Modul '{name}' ist gesperrt (Dateizugriff).")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bad_module(a.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                problems.append(f"Zeile {node.lineno}: Relative Importe sind nicht erlaubt.")
            elif node.module:
                bad_module(node.module, node.lineno)
                for a in node.names:
                    bad_module(f"{node.module}.{a.name}", node.lineno)
        elif isinstance(node, ast.Name):
            if node.id in policy.FORBIDDEN_NAMES or node.id.startswith("__"):
                problems.append(f"Zeile {node.lineno}: Name '{node.id}' ist nicht erlaubt.")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                problems.append(f"Zeile {node.lineno}: Zugriff auf '{node.attr}' ist nicht erlaubt.")
            elif node.attr in policy.FORBIDDEN_ATTRS:
                problems.append(f"Zeile {node.lineno}: '.{node.attr}' ist nicht erlaubt (Dateizugriff).")

    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "process"), None)
    if fn is None:
        problems.append("Es fehlt die Funktion 'def process(img, p):' auf oberster Ebene.")
    elif len(fn.args.args) < 2:
        problems.append("'process' muss zwei Argumente haben: process(img, p).")
    return list(dict.fromkeys(problems))


def validate_output(out, in_hw: tuple[int, int]) -> np.ndarray:
    """Prüft das Ergebnis des Scripts und gibt ein float32-Array (H, W, 3) im Bereich 0..1 zurück."""
    if not isinstance(out, np.ndarray):
        raise ScriptError(f"'process' muss ein numpy-Array zurückgeben, nicht {type(out).__name__}.")
    if out.dtype == np.uint8:
        arr = out.astype(np.float32) / 255.0
    elif out.dtype.kind == "f":
        arr = out.astype(np.float32, copy=False)
    else:
        raise ScriptError(f"Datentyp {out.dtype} nicht erlaubt (erwartet float32/float64 im Bereich 0..1 oder uint8).")
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ScriptError(f"Erwartet wird die Form (H, W, 3), erhalten wurde {tuple(arr.shape)}.")
    h, w = arr.shape[:2]
    ih, iw = in_hw
    if h > ih or w > iw:
        raise ScriptError(f"Ausgabe ({w}x{h}) ist grösser als die Eingabe ({iw}x{ih}). Hochskalieren ist nicht erlaubt.")
    if min(h, w) < policy.MIN_SIDE:
        raise ScriptError(f"Ausgabe ist zu klein ({w}x{h}); Mindestgrösse {policy.MIN_SIDE} Pixel pro Seite.")
    if not np.isfinite(arr).all():
        raise ScriptError("Ausgabe enthält NaN oder unendliche Werte.")
    lo, hi = float(arr.min()), float(arr.max())
    if lo < -0.25 or hi > 1.25:
        raise ScriptError(f"Wertebereich 0..1 erwartet, erhalten {lo:.2f}..{hi:.2f}. Wurde 0..255 verwendet?")
    return np.clip(arr, 0.0, 1.0)
