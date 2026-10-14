#!/usr/bin/env python3
"""SwiftBar plugin: indicador de uso de Claude Code.

Llama fetcher.get_usage() directamente: el fetcher ya tiene throttle interno
(MIN_INTERVAL ~120s + backoff 429) y nunca expone tokens. Fallback a cache
~/.cache/claude-usage.json con stale=true si el fetch falla. Linea 1 como
pildora redondeada (pill_render via image= base64); fallback a fondo ANSI
256 colores si el renderer falla. Siempre sale 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.expanduser("~/indicators/claude-usage"))
import fetcher  # noqa: E402

CACHE_PATH = os.path.expanduser("~/.cache/claude-usage.json")
PILL_RENDER = os.path.expanduser("~/indicators/pill_render")
PILL_HEIGHT = "17"


def read_cache() -> dict | None:
    """Lee el cache si es dict valido; si no, None."""
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        return cached if isinstance(cached, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def parse_iso(value: object) -> datetime | None:
    """Parsea ISO 8601 tolerando Z y naive (asume UTC)."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def color_for(percent: float) -> int:
    """Naranja de Claude con degradado a rojo cerca del limite (256 colores).

    <80 naranja Claude 173, 80-89 rojo-naranja 208, >=90 rojo puro 196.
    Devuelve el codigo 256 de fondo. Solo se usa como fallback ANSI.
    """
    if percent < 80:
        return 173
    if percent < 90:
        return 208
    return 196


def hex_for(percent: float) -> str:
    """Hex de fondo de la pildora por tramo (<80 naranja, 80-89
    rojo-naranja, >=90 rojo)."""
    if percent < 80:
        return "#D97757"
    if percent < 90:
        return "#E06A45"
    return "#E5484D"


def pill_image_b64(text: str, base_hex: str) -> str | None:
    """Renderiza la pildora con pill_render; None si falla."""
    try:
        proc = subprocess.run(
            [PILL_RENDER, text, base_hex, "", PILL_HEIGHT],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode != 0:
            return None
        b64 = proc.stdout.strip()
        return b64 or None
    except Exception:
        return None


def emit_title(text: str, base_hex: str, ansi_bg: int) -> None:
    """Linea 1: pildora con imagen; fallback ANSI si el renderer falla."""
    b64 = pill_image_b64(text, base_hex)
    if b64 is not None:
        print("  | image=%s height=%s" % (b64, PILL_HEIGHT))
    else:
        print("\x1b[97;48;5;%dm %s \x1b[0m | ansi=true" % (ansi_bg, text))


def fmt_percent(value: object) -> str:
    """Formatea a 1 decimal max, sin .0 si es entero."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}"


def bar_for(percent: float) -> str:
    """Barra unicode de 10 bloques. Llenos usan ▓, vacios ░."""
    filled = int(round(percent / 10))
    filled = max(0, min(10, filled))
    return "▓" * filled + "░" * (10 - filled)


def emit_error(message: str) -> None:
    """Linea 1 de error y sale 0 (SwiftBar oculta el plugin si falla)."""
    print(message + " | color=red")


def main() -> int:
    # El fetcher hace su propio throttle; lo llamamos directo.
    try:
        data = fetcher.get_usage()
    except Exception as exc:
        cached = read_cache()
        if cached is not None:
            cached["stale"] = True
            data = cached
        else:
            emit_error("Claude: sin datos")
            sys.stderr.write("claude plugin: fetch fallo: %s\n" % exc)
            return 0

    if not isinstance(data, dict):
        emit_error("Claude: sin datos")
        return 0

    stale = bool(data.get("stale"))
    percent = float(data.get("percent_used") or 0)
    used = data.get("used", 0)
    entitlement = data.get("entitlement", 0)
    remaining = data.get("remaining", 0)
    reset = data.get("reset") or "-"
    updated_at = data.get("updated_at") or "-"
    bg = color_for(percent)
    suffix = " !" if stale else ""

    # Linea 1: pildora compacta con solo el porcentaje (el detalle vive
    # en el dropdown); el fallback ANSI conserva el formato anterior.
    text = "%s%%%s" % (fmt_percent(percent), suffix)
    emit_title(text, hex_for(percent), bg)
    print("---")
    # Barra unicode pintada con el naranja de Claude (#D97757) para combinar.
    print(bar_for(percent) + " | color=#D97757")
    print("Gasto: $%s / $%s" % (used, entitlement))
    print("Restante: %s" % remaining)
    print("Reset: %s" % reset)
    print("Actualizado: %s" % updated_at)
    if stale:
        print("Stale: si | color=#e74c3c")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        # Nunca fallar: SwiftBar oculta el plugin si el exit code no es 0.
        emit_error("Claude: sin datos")
        sys.stderr.write("claude plugin: error inesperado: %s\n" % exc)
        sys.exit(0)
