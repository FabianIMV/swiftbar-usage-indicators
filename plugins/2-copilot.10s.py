#!/usr/bin/env python3
"""SwiftBar plugin: indicador de uso de Copilot premium interactions.

Lee el cache ~/.cache/copilot-usage.json. Solo consulta la red si la cache
tiene mas de 30s (usa la key updated_at, formato ISO). Linea 1 como pildora
redondeada (pill_render via image= base64) en verde suave y oscuro con
tramos a oliva/naranja/rojo; fallback a fondo ANSI 256 colores si el
renderer falla. Siempre sale con codigo 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.expanduser("~/indicators/copilot-indicator"))
import fetcher  # noqa: E402

CACHE_PATH = os.path.expanduser("~/.cache/copilot-usage.json")
MAX_AGE_SECONDS = 30
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


def cache_age_seconds(cached: dict) -> float | None:
    """Edad del cache en segundos, None si updated_at no es parseable."""
    updated = parse_iso(cached.get("updated_at"))
    if updated is None:
        return None
    return (datetime.now(timezone.utc) - updated).total_seconds()


def color_for(percent: float) -> int:
    """Gradiente verde→rojo en 256 colores sobre tramos fijos de percent_used.

    0-49 verde 46, 50-59 verde-amarillo 148, 60-69 amarillo 226,
    70-84 naranja 208, 85-100 rojo 196. Devuelve el codigo 256 de fondo.
    Solo se usa como fallback ANSI.
    """
    if percent < 50:
        return 46
    if percent < 60:
        return 148
    if percent < 70:
        return 226
    if percent < 85:
        return 208
    return 196


def hex_for(percent: float) -> str:
    """Hex de fondo de la pildora por tramo: verde suave oscuro base,
    oliva apagado, naranja apagado, rojo."""
    if percent < 50:
        return "#2E7D4F"
    if percent < 70:
        return "#6B8E3D"
    if percent < 85:
        return "#C77B3F"
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
    print("Copilot: " + message + " | color=red")


def main() -> int:
    cached = read_cache()
    stale = False
    data: dict | None = None

    # Fetch solo si no hay cache o tiene mas de 30s; si falla, cache previa.
    age = cache_age_seconds(cached) if cached else None
    needs_fetch = cached is None or age is None or age > MAX_AGE_SECONDS
    if needs_fetch:
        try:
            data = fetcher.get_usage()
        except Exception as exc:
            if cached is not None:
                data = dict(cached)
                data["stale"] = True
                stale = True
            else:
                emit_error("sin datos")
                sys.stderr.write("copilot plugin: fetch fallo: %s\n" % exc)
                return 0
    else:
        data = dict(cached)

    if data is None:
        emit_error("sin datos")
        return 0

    if data.get("stale"):
        stale = True

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
    # Barra unicode pintada con hex para combinar con el indicador.
    print(bar_for(percent) + " | color=#2ecc71")
    print("Usado: %s / %s" % (used, entitlement))
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
        print("Copilot: sin datos | color=red")
        sys.stderr.write("copilot plugin: error inesperado: %s\n" % exc)
        sys.exit(0)
