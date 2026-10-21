"""Führt Scripts isoliert aus: ohne Netzwerk und mit Zugriff nur auf das Job-Verzeichnis (eine Eingabedatei).

Stufen (die stärkste verfügbare wird genutzt):
  bwrap    bubblewrap: Netzwerk, PID, Dateisystem getrennt, nur Job-Verzeichnis beschreibbar  -> voll
  unshare  nur Netzwerk getrennt; Dateizugriff nur durch statische Prüfung + eingeschränkte Builtins  -> teilweise
  none     keine Isolation  -> nur mit DARKROOM_SANDBOX_MIN=none
Standard: Scripts laufen nur mit Stufe 'bwrap'. Mit DARKROOM_SANDBOX_MIN=unshare|none lässt sich das bewusst senken.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import mode, policy
from .checks import SandboxError, ScriptError, check_script, validate_output

RUNNER = Path(__file__).with_name("sandbox_runner.py")
RANK = {"none": 0, "unshare": 1, "bwrap": 2}
DESCRIPTION = {
    "bwrap": "voll (Netzwerk und Dateisystem getrennt)",
    "unshare": "teilweise (nur Netzwerk getrennt)",
    "none": "keine Isolation",
}


def _works(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=1)
def _bwrap_variant() -> str | None:
    """'proc' (mit neuem /proc), 'noproc' (ohne; nötig in Docker, wo /proc teilweise maskiert ist) oder None.
    DARKROOM_BWRAP_PROC=0 erzwingt 'noproc'."""
    if not shutil.which("bwrap"):
        return None
    for with_proc in ((True, False) if os.environ.get("DARKROOM_BWRAP_PROC") != "0" else (False,)):
        cmd = ["bwrap", "--unshare-all", "--ro-bind", "/", "/", "--dev", "/dev"]
        cmd += ["--proc", "/proc"] if with_proc else []
        if _works(cmd + ["true"]):
            return "proc" if with_proc else "noproc"
    return None


@lru_cache(maxsize=1)
def detect_level() -> str:
    if _bwrap_variant():
        return "bwrap"
    if shutil.which("unshare") and _works(["unshare", "-n", "true"] if os.geteuid() == 0 else ["unshare", "-rn", "true"]):
        return "unshare"
    return "none"


def minimum_level() -> str:
    return os.environ.get("DARKROOM_SANDBOX_MIN", "bwrap")


def status() -> dict:
    if mode.desktop():  # Desktop-App: Scripts laufen nie lokal, sondern nach dem Sync auf dem Server
        return {"level": "server", "description": "Scripts laufen nach dem Sync auf dem Server", "required": "server",
                "allowed": False, "server_only": True}
    lvl = detect_level()
    return {
        "level": lvl,
        "description": DESCRIPTION[lvl],
        "required": minimum_level(),
        "allowed": RANK[lvl] >= RANK[minimum_level()],
    }


def _bwrap_prefix(job: Path) -> list[str]:
    binds: list[str] = []
    seen: set[str] = set()

    def ro(path: str):
        if path and os.path.exists(path) and path not in seen:
            seen.add(path)
            binds.extend(["--ro-bind", path, path])

    for p in ("/usr", "/bin", "/lib", "/lib64", "/sbin"):
        if os.path.islink(p):
            binds.extend(["--symlink", os.readlink(p), p])
            seen.add(p)
        else:
            ro(p)
    for p in ("/etc/ld.so.cache", "/etc/ld.so.conf"):
        ro(p)
    ro("/etc/ld.so.conf.d")
    for p in {sys.prefix, sys.base_prefix, *[x for x in sys.path if x]}:
        if not p.startswith("/usr/") and p != "/usr":
            ro(os.path.realpath(p) if os.path.isdir(p) else "")
    return [
        "bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv",
        *binds, *(["--proc", "/proc"] if _bwrap_variant() == "proc" else []), "--dev", "/dev", "--tmpfs", "/tmp",
        "--bind", str(job), "/job", "--ro-bind", str(RUNNER), "/runner.py", "--chdir", "/job",
        "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin", "--setenv", "HOME", "/tmp",
    ]


def _wrap(cmd: list[str], job: Path, level: str) -> tuple[list[str], str]:
    """Verpackt einen Befehl in die Isolation. Gibt (Befehl, Job-Pfad aus Sicht des Prozesses) zurück."""
    if level == "bwrap":
        return _bwrap_prefix(job) + cmd, "/job"
    if level == "unshare":
        return (["unshare", "-n"] if os.geteuid() == 0 else ["unshare", "-rn"]) + cmd, str(job)
    return cmd, str(job)


def _env() -> dict:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
        "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2", "MKL_NUM_THREADS": "2",
        "PYTHONDONTWRITEBYTECODE": "1", "DARKROOM_RLIMIT_AS": os.environ.get("DARKROOM_RLIMIT_AS", "0"),
    }


def run_command(cmd: list[str], job: Path, timeout: float, level: str | None = None) -> subprocess.CompletedProcess:
    """Startet einen Befehl in der Isolation (auch für Tests der Isolation selbst)."""
    level = level or detect_level()
    full, _ = _wrap(cmd, job, level)
    proc = subprocess.Popen(full, cwd=job, env=_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(full, proc.returncode, out, err)


def run_script(code: str, img: np.ndarray, params: dict, timeout: float = policy.PREVIEW_TIMEOUT,
               static_check: bool = True) -> np.ndarray:
    """Prüft und führt ein Script isoliert aus. Gibt das geprüfte Ergebnis (float32, H x W x 3, 0..1) zurück
    oder wirft ScriptError mit einer verständlichen Meldung."""
    st = status()
    if st.get("server_only"):
        raise SandboxError("Scripts laufen in der Desktop-App nicht. Sie werden nach der Synchronisierung auf dem Server ausgeführt.")
    if not st["allowed"]:
        raise SandboxError(
            f"Scripts sind gesperrt: Sandbox-Stufe '{st['level']}' ({st['description']}), verlangt ist '{st['required']}'. "
            "Siehe README, Abschnitt Sandbox."
        )
    if static_check:
        problems = check_script(code)
        if problems:
            raise ScriptError("Script nicht erlaubt:\n" + "\n".join(problems))
    if img.ndim != 3 or img.shape[2] != 3 or img.shape[0] * img.shape[1] > policy.MAX_PIXELS:
        raise ScriptError("Eingabebild hat eine unzulässige Form oder Grösse.")

    level = st["level"]
    with tempfile.TemporaryDirectory(prefix="darkroom-job-") as tmp:
        job = Path(tmp)
        np.save(job / "in.npy", np.ascontiguousarray(img, dtype=np.float32), allow_pickle=False)
        (job / "params.json").write_text(json.dumps(params, default=str), encoding="utf-8")
        (job / "script.py").write_text(code, encoding="utf-8")
        (job / "policy.json").write_text(json.dumps(policy.runner_policy()), encoding="utf-8")
        if level == "bwrap":
            runner, jobarg = "/runner.py", "/job"
        else:
            runner, jobarg = str(RUNNER), str(job)
        cmd = [sys.executable, "-X", "faulthandler", "-I", runner, jobarg]
        try:
            proc = run_command(cmd, job, timeout, level)
        except subprocess.TimeoutExpired:
            raise ScriptError(f"Zeitlimit überschritten ({timeout:.0f} s). Das Script ist zu langsam oder endlos.") from None

        res_file = job / "result.json"
        if not res_file.exists():
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-6:]
            if proc.returncode in (-9, 137):
                raise ScriptError(f"Prozess wurde beendet (vermutlich Speicherlimit {policy.MEMORY_MB} MB).")
            if (job / "started").exists() and proc.returncode in (-11, 139, -6, 134, -7, -4):
                # Start war in Ordnung, der Absturz geschah beim Ausführen des Scripts (z. B. in OpenCV): Script-Fehler
                raise ScriptError(f"Das Script hat den Prozess abstürzen lassen (Signal, Exit-Code {proc.returncode}). "
                                  "Verwende andere/robustere Funktionen oder kleinere Parameter. "
                                  + " | ".join(tail[-3:]))
            raise SandboxError("Die Sandbox konnte das Script nicht starten (kein Fehler im Script): "
                               + (" | ".join(tail) or f"Exit-Code {proc.returncode}")
                               + f" [Exit-Code {proc.returncode}, Stufe {level}]")
        res = json.loads(res_file.read_text(encoding="utf-8"))
        if not res.get("ok"):
            raise ScriptError(res.get("error", "Unbekannter Fehler im Script."))

        out_file = job / "out.npy"
        max_bytes = policy.MAX_PIXELS * 3 * 8 + 4096
        if not out_file.exists() or out_file.stat().st_size > max_bytes:
            raise ScriptError("Ausgabe fehlt oder ist unzulässig gross.")
        view = np.load(out_file, mmap_mode="r", allow_pickle=False)  # prüft Header, ohne alles zu laden
        if view.ndim != 3 or view.shape[0] * view.shape[1] > img.shape[0] * img.shape[1]:
            raise ScriptError(f"Ausgabeform {tuple(view.shape)} ist nicht zulässig (erwartet H x W x 3, nicht grösser als die Eingabe).")
        out = np.array(view)
        del view
        return validate_output(out, img.shape[:2])
