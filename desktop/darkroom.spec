# -*- mode: python ; coding: utf-8 -*-
# PyInstaller-Beschreibung der Desktop-App. Bauen (im Hauptordner des Projekts):
#   pyinstaller desktop/darkroom.spec --noconfirm --distpath dist --workpath build/pyi
# Ergebnis: dist/Darkroom/ (Windows, Linux) bzw. dist/Darkroom.app (macOS). Ein Cross-Build gibt es nicht:
# jedes Betriebssystem muss auf sich selbst gebaut werden (siehe desktop/build.ps1, desktop/build.sh, .gitlab-ci.yml).
# DARKROOM_CONSOLE=1 baut eine Variante mit Konsolenfenster (für Fehlersuche).
import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "darkroom" / "__init__.py").read_text(encoding="utf-8")).group(1)

datas = [(str(ROOT / "darkroom" / "static"), "darkroom/static")]
datas += collect_data_files("lensfunpy")  # Objektivdatenbank (XML) liegt im Paket
binaries = collect_dynamic_libs("rawpy") + collect_dynamic_libs("lensfunpy")
hiddenimports = (collect_submodules("uvicorn") + collect_submodules("webview") + collect_submodules("darkroom")
                 + ["anyio._backends._asyncio", "h11", "multipart"])

a = Analysis(
    [str(ROOT / "desktop" / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Darkroom",
    console=os.environ.get("DARKROOM_CONSOLE") == "1",
    icon=str(ROOT / "desktop" / "darkroom.ico") if (ROOT / "desktop" / "darkroom.ico").exists() and sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Darkroom")
if sys.platform == "darwin":
    app = BUNDLE(
        coll, name="Darkroom.app", bundle_identifier="ch.mq-data.darkroom", version=VERSION,
        icon=str(ROOT / "desktop" / "darkroom.icns") if (ROOT / "desktop" / "darkroom.icns").exists() else None,
        info_plist={"NSHighResolutionCapable": True, "CFBundleShortVersionString": VERSION, "LSApplicationCategoryType": "public.app-category.photography"},
    )
