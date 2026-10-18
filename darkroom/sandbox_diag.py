"""Diagnose: findet heraus, welche Sandbox-Einstellung `import cv2` abstürzen lässt.
Aufruf im Container:  python -m darkroom.sandbox_diag
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import sandbox

LIMIT = "import resource;m=4096*1024*1024;resource.setrlimit(resource.RLIMIT_AS,(m,m));"
PY = sys.executable


def variants(job: Path):
    base = sandbox._bwrap_prefix(job)
    flat = [a for a in base]
    no_unshare_all = ["--unshare-net" if a == "--unshare-all" else a for a in flat]
    ins = flat.index("--chdir")
    with_sys = flat[:ins] + ["--ro-bind", "/sys", "/sys", "--tmpfs", "/dev/shm"] + flat[ins:]
    with_proc = flat[:ins] + ["--proc", "/proc"] + flat[ins:] if "--proc" not in flat else flat
    no_proc = [a for i, a in enumerate(flat) if a != "--proc" and not (i and flat[i - 1] == "--proc")]
    imp = lambda code: [PY, "-X", "faulthandler", "-I", "-c", code]
    th = ["--setenv", "OPENBLAS_NUM_THREADS", "1", "--setenv", "OMP_NUM_THREADS", "1", "--setenv", "OPENCV_FOR_THREADS_NUM", "1"]
    yield "Ohne bwrap, nur Netzwerk getrennt: import cv2", (["unshare", "-n"] if os.geteuid() == 0 else []) + imp("import cv2")
    yield "bwrap Standard: import numpy", base + imp("import numpy")
    yield "bwrap Standard: import cv2", base + imp("import cv2")
    yield "bwrap Standard + RLIMIT_AS 4 GB: import cv2", base + imp(LIMIT + "import cv2")
    yield "bwrap mit /sys und /dev/shm: import cv2", with_sys + imp("import cv2")
    yield "bwrap mit /proc: import cv2", with_proc + imp("import cv2")
    yield "bwrap ohne /proc: import cv2", no_proc + imp("import cv2")
    yield "bwrap nur --unshare-net: import cv2", no_unshare_all + imp("import cv2")
    yield "bwrap 1 Thread: import cv2", flat[:ins] + th + flat[ins:] + imp("import cv2")
    yield "bwrap Standard: import skimage, scipy", base + imp("import skimage, scipy")


def main():
    print("bwrap:", sandbox._bwrap_variant(), "| Python:", PY, sys.version.split()[0])
    with tempfile.TemporaryDirectory() as tmp:
        job = Path(tmp)
        (job / "x").write_text("")
        for name, cmd in variants(job):
            try:
                r = subprocess.run(cmd, cwd=job, env=sandbox._env(), capture_output=True, timeout=60)
                err = r.stderr.decode("utf-8", "replace").strip().splitlines()
                print(f"{'OK  ' if r.returncode == 0 else 'FEHLER'} rc={r.returncode:<4} {name}")
                if r.returncode:
                    for line in err[:6]:
                        print("        ", line[:200])
            except Exception as e:  # noqa: BLE001
                print(f"FEHLER {name}: {e}")


if __name__ == "__main__":
    main()
