"""Läuft IM isolierten Prozess. Keine Importe aus dem darkroom-Paket (wird einzeln gestartet).

Aufruf: python -I sandbox_runner.py <job-verzeichnis>
Job-Verzeichnis enthält: in.npy, params.json, script.py, policy.json
Schreibt: out.npy und result.json
"""
import builtins
import json
import os
import resource
import sys
import traceback

job = sys.argv[1]
policy = json.load(open(os.path.join(job, "policy.json"), encoding="utf-8"))


def finish(obj: dict, code: int):
    with open(os.path.join(job, "result.json"), "w", encoding="utf-8") as f:
        json.dump(obj, f)
    sys.exit(code)


# Ressourcenlimits (zusätzlich zum Zeitlimit des Hosts)
mem = int(policy["memory_mb"]) * 1024 * 1024
# RLIMIT_AS bringt OpenCV in Docker beim Import zum Absturz (Segfault, im Diagnose-Lauf gemessen) und ist daher nur
# optional (DARKROOM_RLIMIT_AS=1). Den Speicher begrenzt stattdessen der Container (mem_limit in docker-compose.yml).
limits = [(resource.RLIMIT_AS, mem)] if os.environ.get("DARKROOM_RLIMIT_AS") == "1" else []
for res, val in (*limits, (resource.RLIMIT_FSIZE, 2 * 1024**3), (resource.RLIMIT_NOFILE, 256),
                 (resource.RLIMIT_CORE, 0)):
    try:
        resource.setrlimit(res, (val, val))
    except (ValueError, OSError):
        pass

try:
    import numpy as np
    import cv2

    cv2.setNumThreads(2)
    img = np.load(os.path.join(job, "in.npy"), allow_pickle=False)
    params = json.load(open(os.path.join(job, "params.json"), encoding="utf-8"))
    src = open(os.path.join(job, "script.py"), encoding="utf-8").read()
except BaseException as e:  # noqa: BLE001
    finish({"ok": False, "error": f"Vorbereitung fehlgeschlagen: {e}"}, 3)

open(os.path.join(job, "started"), "w").close()  # Marker: Bibliotheken geladen, ab hier läuft das Script
allowed = set(policy["allowed_imports"])
forbidden_prefixes = tuple(policy["forbidden_module_prefixes"])
real_import = builtins.__import__


def _check(name: str):
    top = name.split(".")[0]
    if top not in allowed:
        raise ImportError(f"Import von '{name}' ist nicht erlaubt")
    for pre in forbidden_prefixes:
        if name == pre or name.startswith(pre + "."):
            raise ImportError(f"Modul '{name}' ist gesperrt")


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level != 0:
        raise ImportError("Relative Importe sind nicht erlaubt")
    _check(name)
    for item in fromlist or ():
        if item != "*":
            _check(f"{name}.{item}")
    return real_import(name, globals, locals, fromlist, level)


safe_builtins = {k: v for k, v in vars(builtins).items() if k not in set(policy["forbidden_names"])}
safe_builtins["__import__"] = guarded_import
g = {"__builtins__": safe_builtins, "__name__": "user_script", "np": np, "cv2": cv2}


def script_line(tb) -> int | None:
    line = None
    for fs in traceback.extract_tb(tb):
        if fs.filename == "<script>":
            line = fs.lineno
    return line


try:
    exec(compile(src, "<script>", "exec"), g)  # noqa: S102 - Zweck dieses Prozesses
    fn = g.get("process")
    if not callable(fn):
        finish({"ok": False, "error": "Die Funktion 'process' fehlt."}, 2)
    out = fn(img.copy(), params)
    if not isinstance(out, np.ndarray) or out.dtype == object:
        finish({"ok": False, "error": f"'process' muss ein numpy-Array zurückgeben (erhalten: {type(out).__name__})."}, 2)
    np.save(os.path.join(job, "out.npy"), out, allow_pickle=False)
    finish({"ok": True, "shape": list(out.shape), "dtype": str(out.dtype)}, 0)
except SystemExit:
    raise
except MemoryError:
    finish({"ok": False, "error": f"Speicherlimit ({policy['memory_mb']} MB) überschritten."}, 2)
except BaseException as e:  # noqa: BLE001
    line = script_line(e.__traceback__)
    where = f" (Zeile {line})" if line else ""
    finish({"ok": False, "error": f"{type(e).__name__}: {e}{where}"}, 2)
