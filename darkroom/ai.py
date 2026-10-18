"""KI-Anbindung: erzeugt aus einer Beschreibung ein Script, testet es in der Sandbox und korrigiert Fehler.

Gesendet werden nur: dein Text, die Parameter (YAML), ein vorhandenes Script und technische Eckdaten.
Das Foto selbst und seine Metadaten verlassen den Server nicht.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Callable, Protocol

import numpy as np

from . import policy
from .checks import SandboxError, ScriptError, check_script
from .sandbox import run_script

MAX_ATTEMPTS = 3


class AIError(Exception):
    pass


class Provider(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict]) -> str: ...


def _post(url: str, headers: dict, body: dict, timeout: float = 180) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise AIError(f"KI-Dienst antwortet mit Fehler {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise AIError(f"KI-Dienst nicht erreichbar: {e}") from None


class ClaudeProvider:
    name = "claude"

    def __init__(self, api_key: str, model: str):
        self.api_key, self.model = api_key, model

    def complete(self, system: str, messages: list[dict]) -> str:
        data = _post(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            {"model": self.model, "max_tokens": 4096, "system": system, "messages": messages},
        )
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, model: str):
        self.base_url, self.model = base_url.rstrip("/"), model

    def complete(self, system: str, messages: list[dict]) -> str:
        data = _post(f"{self.base_url}/api/chat", {}, {
            "model": self.model, "stream": False, "messages": [{"role": "system", "content": system}, *messages],
        }, timeout=600)
        return data.get("message", {}).get("content", "")


def get_provider() -> Provider:
    kind = os.environ.get("DARKROOM_AI_PROVIDER", "claude").lower()
    if kind == "ollama":
        return OllamaProvider(os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434"),
                              os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:14b"))
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AIError("ANTHROPIC_API_KEY ist nicht gesetzt (siehe README, Abschnitt KI).")
    return ClaudeProvider(key, os.environ.get("DARKROOM_AI_MODEL", "claude-sonnet-5-5"))


def provider_status() -> dict:
    kind = os.environ.get("DARKROOM_AI_PROVIDER", "claude").lower()
    return {"provider": kind, "configured": kind == "ollama" or bool(os.environ.get("ANTHROPIC_API_KEY"))}


SYSTEM_TEMPLATE = """You write short Python scripts for a photo-editing app. Reply with exactly ONE ```python code block, \
followed by at most two sentences of explanation in German.

CONTRACT
- The script defines: def process(img, p):
- img: numpy float32 array, shape (H, W, 3), RGB, values 0..1, sRGB-encoded (gamma applied). It has already been developed from the \
RAW file with the user's basic parameters (exposure, white balance, crop, sharpening ...).
- p: dict with the current parameters (information only, e.g. p["exposure"]) and p["_scale"], a float in (0, 1]: 1.0 means full \
resolution. The user first sees a downscaled preview, so EVERY size in pixels (blur sigma, kernel size, radius) MUST be multiplied by \
p["_scale"], otherwise preview and export look different.
- Return a numpy array (H2, W2, 3), same layout and value range 0..1 (not 0..255), finite values only, H2 <= H and W2 <= W. \
Cropping is allowed, upscaling is not. Do not change the color space; the app converts to the target color space on export.
- np (numpy) and cv2 are already imported. Import anything else yourself, from the allowed list only.

RULES (checked automatically, scripts that break them are rejected)
{rules}

PRACTICAL
- Images can be up to about 24 megapixels. Use vectorized numpy/cv2/scikit-image; no per-pixel Python loops. Time limit {t} seconds \
for export, {m} MB memory.
- Deterministic results; fix random seeds if you need randomness.
- Put tunable values as named constants at the top of the script, with a short German comment each.
- If the user gives an existing script, change as little as needed and keep its structure and constants.
- If the request cannot be done within these rules, deliver the closest allowed solution and say in the explanation what is missing. \
Never try to work around the rules.
"""


def build_system_prompt() -> str:
    return SYSTEM_TEMPLATE.format(rules=policy.rules_text(), t=policy.EXPORT_TIMEOUT, m=policy.MEMORY_MB)


def build_user_message(request: str, yaml_text: str, current_code: str | None) -> str:
    parts = [f"Auftrag: {request.strip()}", "", "Aktuelle Parameter (YAML, nur zur Information):", yaml_text.strip() or "(keine)"]
    if current_code and current_code.strip():
        parts += ["", "Bestehendes Script (anpassen, nicht neu schreiben):", "```python", current_code.strip(), "```"]
    else:
        parts += ["", "Es gibt noch kein Script."]
    return "\n".join(parts)


_CODE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def extract_code(text: str) -> tuple[str, str]:
    """Gibt (Code, Erklärung) zurück. Leerer Code, wenn kein Codeblock gefunden wurde."""
    m = _CODE.search(text)
    if not m:
        return "", text.strip()
    explanation = (text[: m.start()] + text[m.end():]).strip()
    return m.group(1).strip() + "\n", explanation


def _header(request: str) -> str:
    one_line = " ".join(request.split())[:200]
    return f"# Auftrag: {one_line}\n"


def generate_script(provider: Provider, request: str, yaml_text: str, current_code: str | None, test_img: np.ndarray,
                    params: dict, max_attempts: int = MAX_ATTEMPTS,
                    runner: Callable[..., np.ndarray] = run_script) -> dict:
    """Fragt die KI, prüft statisch, testet in der Sandbox und gibt Fehler an die KI zurück.
    Ergebnis: {ok, code, explanation, attempts, log}. Bei Misserfolg steht der letzte Versuch in 'code'."""
    system = build_system_prompt()
    messages = [{"role": "user", "content": build_user_message(request, yaml_text, current_code)}]
    log: list[str] = []
    code, explanation = "", ""
    for attempt in range(1, max_attempts + 1):
        reply = provider.complete(system, messages)
        code, explanation = extract_code(reply)
        if not code:
            error = "Die Antwort enthielt keinen Python-Codeblock."
        else:
            problems = check_script(code)
            if problems:
                error = "Regelverstösse:\n" + "\n".join(problems)
            else:
                try:
                    runner(code, test_img, params, timeout=policy.PREVIEW_TIMEOUT)
                    log.append(f"Versuch {attempt}: Test erfolgreich.")
                    if not code.startswith("# Auftrag:"):
                        code = _header(request) + code
                    return {"ok": True, "code": code, "explanation": explanation, "attempts": attempt, "log": log}
                except SandboxError as e:  # Problem der Umgebung, nicht des Scripts: nicht erneut versuchen
                    log.append(f"Versuch {attempt}: Sandbox-Fehler, Abbruch ohne weitere Versuche.")
                    return {"ok": False, "infrastructure": True, "code": code, "explanation": explanation,
                            "attempts": attempt, "log": log, "error": str(e)}
                except ScriptError as e:
                    error = str(e)
        log.append(f"Versuch {attempt}: {error.splitlines()[0]}")
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": f"Das Script wurde abgelehnt oder ist fehlgeschlagen:\n{error}\n\n"
                                         "Korrigiere es. Antworte wieder mit genau einem ```python Codeblock."},
        ]
    if code and not code.startswith("# Auftrag:"):
        code = _header(request) + code
    return {"ok": False, "code": code, "explanation": explanation, "attempts": max_attempts, "log": log,
            "error": f"Nach {max_attempts} Versuchen ohne gültiges Script. Letzter Fehler: {error.splitlines()[0]}"}
