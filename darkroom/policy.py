"""Regeln für Scripts. Einzige Quelle für statische Prüfung, Sandbox-Runner und KI-Prompt."""
from __future__ import annotations

ALLOWED_IMPORTS = ("numpy", "cv2", "skimage", "scipy", "math", "functools", "itertools", "typing")

# Untermodule, die trotz erlaubtem Hauptmodul gesperrt sind (Dateizugriff)
FORBIDDEN_MODULE_PREFIXES = ("skimage.io", "skimage.data", "scipy.io", "scipy.misc", "numpy.ctypeslib", "numpy.f2py")

# Namen, die im Script nicht vorkommen dürfen
FORBIDDEN_NAMES = (
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint",
    "getattr", "setattr", "delattr", "globals", "locals", "vars", "memoryview", "exit", "quit", "help",
)

# Attribute, über die Bibliotheken Dateien lesen/schreiben oder Prozesse starten
FORBIDDEN_ATTRS = (
    "imread", "imwrite", "fromfile", "tofile", "load", "save", "savez", "savez_compressed", "savetxt",
    "loadtxt", "genfromtxt", "memmap", "fromregex", "dump", "open", "VideoCapture", "VideoWriter",
    "FileStorage", "system", "popen", "environ", "getenv", "ctypes",
)

MAX_SCRIPT_BYTES = 30_000
MIN_SIDE = 16
MAX_PIXELS = 80_000_000

PREVIEW_TIMEOUT = 30  # Sekunden
EXPORT_TIMEOUT = 300
MEMORY_MB = 4096


def runner_policy() -> dict:
    """Wird als policy.json in das Job-Verzeichnis geschrieben und vom Runner gelesen."""
    return {
        "allowed_imports": list(ALLOWED_IMPORTS),
        "forbidden_module_prefixes": list(FORBIDDEN_MODULE_PREFIXES),
        "forbidden_names": list(FORBIDDEN_NAMES),
        "memory_mb": MEMORY_MB,
    }


def rules_text() -> str:
    """Regeltext für den KI-Prompt, aus denselben Konstanten wie die Prüfung."""
    return (
        f"ALLOWED imports: {', '.join(ALLOWED_IMPORTS)} (np and cv2 are already imported).\n"
        f"FORBIDDEN modules: {', '.join(FORBIDDEN_MODULE_PREFIXES)}.\n"
        f"FORBIDDEN names: {', '.join(FORBIDDEN_NAMES)}, and any name or attribute starting with a double underscore.\n"
        f"FORBIDDEN attributes (file access / processes): {', '.join(FORBIDDEN_ATTRS)}.\n"
        "No file access of any kind, no network, no subprocesses. The script only receives the image array "
        "and returns an image array; the app reads and writes all files."
    )
