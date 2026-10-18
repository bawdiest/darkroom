"""XMP lesen (Import) und schreiben (Export). Fokus: Lightroom/Camera Raw (crs:) und Standardfelder."""
from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET

from .schema import Params

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "crs": "http://ns.adobe.com/camera-raw-settings/1.0/",
    "darktable": "http://darktable.sf.net/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
}
for k, v in NS.items():
    ET.register_namespace(k, v)

RDF_DESC = f"{{{NS['rdf']}}}Description"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _attrs(desc: ET.Element, prefix: str) -> dict:
    ns = f"{{{NS[prefix]}}}"
    return {k[len(ns):]: v for k, v in desc.attrib.items() if k.startswith(ns)}


def _bag(desc: ET.Element, prefix: str, name: str) -> list[str]:
    el = desc.find(f"{{{NS[prefix]}}}{name}")
    if el is None:
        return []
    return [li.text for li in el.iter(f"{{{NS['rdf']}}}li") if li.text]


def read_xmp(path: Path) -> tuple[Params, dict]:
    """Liest eine XMP-Datei. Gibt (Params, Bericht) zurueck.
    Bericht: {'quelle': 'lightroom'|'darktable'|'unbekannt', 'unmapped': {...}}"""
    tree = ET.parse(path)
    desc = next(tree.getroot().iter(RDF_DESC), None)
    if desc is None:
        raise ValueError("Kein rdf:Description gefunden")
    crs = _attrs(desc, "crs")
    dt = _attrs(desc, "darktable")
    xmp = _attrs(desc, "xmp")
    quelle = "darktable" if dt else ("lightroom" if crs else "unbekannt")

    data: dict = {}
    unmapped: dict = {}

    if (r := _f(xmp.get("Rating"))) is not None and r >= 0:
        data["rating"] = int(r)
    if xmp.get("Label"):
        data["label"] = xmp["Label"]
    tags = _bag(desc, "dc", "subject") + _bag(desc, "lr", "hierarchicalSubject")
    data["tags"] = sorted({t.split("|")[-1] for t in tags})

    # Lightroom / Camera Raw
    mapping = {
        "Exposure2012": "exposure",
        "Exposure": "exposure",
        "Contrast2012": "contrast",
        "Highlights2012": "highlights",
        "Shadows2012": "shadows",
        "Whites2012": "whites",
        "Blacks2012": "blacks",
    }
    for key, target in mapping.items():
        if key in crs and (v := _f(crs[key])) is not None and target not in data:
            data[target] = v
    # Alte Prozessversionen (2003/2010): Contrast/Brightness haben andere Skalen, markieren
    pv = crs.get("ProcessVersion")
    if pv and not pv.startswith(("11", "15")):  # 11.0 = PV2012
        unmapped["process_version"] = pv

    temp, tint = _f(crs.get("Temperature")), _f(crs.get("Tint"))
    if crs.get("WhiteBalance") == "As Shot":
        pass
    elif temp is not None:
        data["wb"] = {"temp": temp, "tint": tint or 0}

    crop = {}
    for src, dst in (("CropLeft", "left"), ("CropTop", "top"), ("CropRight", "right"), ("CropBottom", "bottom")):
        if (v := _f(crs.get(src))) is not None:
            crop[dst] = v
    if (a := _f(crs.get("CropAngle"))) is not None:
        crop["angle"] = -a  # crs: positiv = gegen den Uhrzeigersinn
    if crs.get("HasCrop", "").lower() == "true" or crop:
        data["crop"] = crop

    if (amt := _f(crs.get("Sharpness"))) is not None:
        data["sharpen"] = {
            "amount": min(round(amt / 50.0, 3), 3.0),
            "radius": min(max(_f(crs.get("SharpenRadius")) or 1.0, 0.3), 5.0),
        }
    lum, col = _f(crs.get("LuminanceSmoothing")), _f(crs.get("ColorNoiseReduction"))
    if lum or col:
        data["denoise"] = {"luminance": (lum or 0) / 100.0, "color": (col or 0) / 100.0}
    if crs.get("LensProfileEnable") == "1":
        data["lens"] = {"enabled": True}

    # Alles uebrige in crs: erhalten, aber nicht interpretiert
    handled = set(mapping) | {
        "ProcessVersion", "Temperature", "Tint", "WhiteBalance", "CropLeft", "CropTop", "CropRight",
        "CropBottom", "CropAngle", "HasCrop", "Sharpness", "SharpenRadius", "SharpenDetail",
        "LuminanceSmoothing", "ColorNoiseReduction", "LensProfileEnable",
    }
    extra = {k: v for k, v in crs.items() if k not in handled}
    if extra:
        unmapped["crs"] = extra
    if dt:
        unmapped["darktable"] = {k: v for k, v in dt.items() if k in ("xmp_version", "raw_params", "auto_presets_applied", "history_end")}
        hist = desc.find(f"{{{NS['darktable']}}}history")
        if hist is not None:
            unmapped["darktable_history_ops"] = [
                li.attrib.get(f"{{{NS['darktable']}}}operation")
                for li in hist.iter(f"{{{NS['rdf']}}}li")
            ]
    if unmapped:
        data["_unmapped"] = unmapped
    return Params.model_validate(data), {"quelle": quelle, "unmapped": unmapped}


_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="darkroom">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
   xmlns:xmp="http://ns.adobe.com/xap/1.0/"
   xmlns:dc="http://purl.org/dc/elements/1.1/"
   xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/">
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""


def write_xmp(p: Params, out: Path) -> None:
    """Schreibt Standard-crs:-XMP. Bestehende Datei wird nicht veraendert, sondern ersetzt (Aufrufer sichert)."""
    root = ET.fromstring(_TEMPLATE)
    desc = next(root.iter(RDF_DESC))

    def put(prefix: str, name: str, value):
        desc.set(f"{{{NS[prefix]}}}{name}", str(value))

    put("crs", "Version", "15.0")
    put("crs", "ProcessVersion", "11.0")
    put("xmp", "Rating", p.rating)
    if p.label:
        put("xmp", "Label", p.label)
    put("crs", "Exposure2012", f"{p.exposure:+.2f}")
    put("crs", "Contrast2012", f"{p.contrast:+.0f}")
    put("crs", "Highlights2012", f"{p.highlights:+.0f}")
    put("crs", "Shadows2012", f"{p.shadows:+.0f}")
    put("crs", "Whites2012", f"{p.whites:+.0f}")
    put("crs", "Blacks2012", f"{p.blacks:+.0f}")
    if p.wb.temp is not None:
        put("crs", "WhiteBalance", "Custom")
        put("crs", "Temperature", f"{p.wb.temp:.0f}")
        put("crs", "Tint", f"{p.wb.tint:+.0f}")
    c = p.crop
    if (c.left, c.top, c.right, c.bottom, c.angle) != (0, 0, 1, 1, 0):
        put("crs", "HasCrop", "True")
        put("crs", "CropLeft", f"{c.left:.6f}")
        put("crs", "CropTop", f"{c.top:.6f}")
        put("crs", "CropRight", f"{c.right:.6f}")
        put("crs", "CropBottom", f"{c.bottom:.6f}")
        put("crs", "CropAngle", f"{-c.angle:.4f}")
    if p.sharpen.amount:
        put("crs", "Sharpness", f"{round(p.sharpen.amount * 50)}")
        put("crs", "SharpenRadius", f"{p.sharpen.radius:+.1f}")
    if p.denoise.luminance:
        put("crs", "LuminanceSmoothing", f"{round(p.denoise.luminance * 100)}")
    if p.denoise.color:
        put("crs", "ColorNoiseReduction", f"{round(p.denoise.color * 100)}")
    if p.lens.enabled:
        put("crs", "LensProfileEnable", "1")

    if p.tags:
        subj = ET.SubElement(desc, f"{{{NS['dc']}}}subject")
        bag = ET.SubElement(subj, f"{{{NS['rdf']}}}Bag")
        for t in p.tags:
            ET.SubElement(bag, f"{{{NS['rdf']}}}li").text = t

    ET.indent(root)
    out.write_text(ET.tostring(root, encoding="unicode", xml_declaration=True), encoding="utf-8")
