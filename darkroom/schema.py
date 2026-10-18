"""Parameter-Schema (Phase 1). Eine YAML-Datei pro Foto, validiert mit pydantic."""
from __future__ import annotations

from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


class WB(BaseModel):
    temp: Optional[float] = Field(None, ge=2000, le=50000, description="Kelvin; None = Kamera-WB")
    tint: float = Field(0, ge=-150, le=150)


class Crop(BaseModel):
    left: float = Field(0.0, ge=0, le=1)
    top: float = Field(0.0, ge=0, le=1)
    right: float = Field(1.0, ge=0, le=1)
    bottom: float = Field(1.0, ge=0, le=1)
    angle: float = Field(0.0, ge=-45, le=45, description="Grad, positiv = im Uhrzeigersinn")


class Sharpen(BaseModel):
    amount: float = Field(0.0, ge=0, le=3)
    radius: float = Field(1.0, ge=0.3, le=5)


class Denoise(BaseModel):
    luminance: float = Field(0.0, ge=0, le=1)
    color: float = Field(0.0, ge=0, le=1)


class LensProfile(BaseModel):
    enabled: bool = False
    distortion: bool = True  # Verzeichnung
    vignetting: bool = True  # Vignettierung
    tca: bool = True  # Farbsäume (chromatische Aberration)


class Params(BaseModel):
    # Unbekannte Felder bleiben erhalten (z. B. vom Importer, noch nicht unterstuetzt)
    model_config = ConfigDict(extra="allow")

    preset: Optional[str] = None
    exposure: float = Field(0.0, ge=-5, le=5, description="EV")
    contrast: float = Field(0.0, ge=-100, le=100)
    highlights: float = Field(0.0, ge=-100, le=100)
    shadows: float = Field(0.0, ge=-100, le=100)
    whites: float = Field(0.0, ge=-100, le=100)
    blacks: float = Field(0.0, ge=-100, le=100)
    wb: WB = WB()
    crop: Crop = Crop()
    sharpen: Sharpen = Sharpen()
    denoise: Denoise = Denoise()
    lens: LensProfile = LensProfile()
    rating: int = Field(0, ge=0, le=5)
    label: Optional[str] = None
    tags: list[str] = []
    auto_match: Optional[dict] = None  # Marker des Hintergrundjobs «Angleichen»; siehe automatch.py


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_params(text: str, presets_dir=None) -> Params:
    """YAML -> Params. Ein optionales Preset wird als Basis geladen, Fotowerte ueberschreiben."""
    data = yaml.safe_load(text) or {}
    name = data.get("preset")
    if name and presets_dir is not None:
        from pathlib import Path

        pf = Path(presets_dir) / f"{name}.yaml"
        if pf.exists():
            base = yaml.safe_load(pf.read_text(encoding="utf-8")) or {}
            data = _merge(base, data)
    return Params.model_validate(data)


def dump_params(p: Params) -> str:
    return yaml.safe_dump(
        p.model_dump(exclude_none=True), sort_keys=False, allow_unicode=True
    )
