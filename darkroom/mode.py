"""Betriebsart: «server» (Docker/Browser, mit Anmeldung) oder «desktop» (lokale App mit Fenster, eine Person, Sync zum Server)."""
import os


def desktop() -> bool:
    return os.environ.get("DARKROOM_MODE", "").strip().lower() == "desktop"
