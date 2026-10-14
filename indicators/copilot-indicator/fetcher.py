"""Copilot premium-interactions usage fetcher. Solo stdlib.

Cache en ~/.cache/copilot-usage.json con keys:
  used, entitlement, remaining, percent_used, reset, updated_at, stale.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.github.com/copilot_internal/user"
CACHE_PATH = Path.home() / ".cache" / "copilot-usage.json"


def get_gh_token() -> str:
    """Obtiene el token via `gh auth token`. Solo memoria, nunca disco."""
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError("gh no esta instalado o no esta en PATH. Instala gh y corre `gh auth login`.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("`gh auth token` tardo demasiado (timeout 15s).")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            "No se pudo obtener token: `gh auth token` fallo. "
            "Corre `gh auth login` / `gh auth refresh`. Detalle: " + (err or f"exit {proc.returncode}")
        )
    token = proc.stdout.strip()
    if not token:
        raise RuntimeError("`gh auth token` no devolvio token. Corre `gh auth login` de nuevo.")
    return token


def fetch_raw(token: str) -> dict:
    """GET al endpoint interno de Copilot. Devuelve dict parseado."""
    if not token:
        raise RuntimeError("fetch_raw recibio un token vacio.")
    req = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "copilot-indicator-fetcher",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
    except Exception as exc:
        raise RuntimeError(f"Fallo red consultando {API_URL}: {exc}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Respuesta no-JSON del endpoint Copilot: {exc}")
    if not isinstance(data, dict):
        raise RuntimeError("Respuesta inesperada del endpoint Copilot (no es objeto JSON).")
    return data


def parse_usage(data: dict) -> dict:
    """Extrae premium_interactions y arma el dict del contrato de cache."""
    snap = (data.get("quota_snapshots") or {}).get("premium_interactions") or {}

    used = int(snap.get("credits_used", 0) or 0)
    entitlement = int(snap.get("entitlement", 0) or 0)
    remaining = snap.get("remaining", None)
    if remaining is None:
        remaining = snap.get("quota_remaining", entitlement - used)
    remaining = int(remaining or 0)

    pct_rem = snap.get("percent_remaining", None)
    if pct_rem is not None:
        try:
            percent_used = round(100.0 - float(pct_rem), 1)
        except (TypeError, ValueError):
            percent_used = round(used / entitlement * 100, 1) if entitlement > 0 else 0.0
    elif entitlement > 0:
        percent_used = round(used / entitlement * 100, 1)
    else:
        percent_used = 0.0

    reset = data.get("quota_reset_date") or data.get("quota_reset_date_utc") or ""
    reset = str(reset)[:10]

    stale = False
    if not snap or snap.get("unlimited") is True or not snap.get("has_quota", False):
        stale = True

    return {
        "used": used,
        "entitlement": entitlement,
        "remaining": remaining,
        "percent_used": float(percent_used),
        "reset": reset,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stale": stale,
    }


def _read_cache() -> dict | None:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        return cached if isinstance(cached, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_cache_atomic(payload: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(CACHE_PATH.parent), prefix=".copilot-usage.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, CACHE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_usage() -> dict:
    """Une token + fetch + parse, escribe cache atomico.

    Si falla red/API, devuelve el ultimo cache con stale=true
    conservando su updated_at original. Sin cache previo, propaga el error.
    """
    try:
        token = get_gh_token()
        payload = parse_usage(fetch_raw(token))
        _write_cache_atomic(payload)
        return payload
    except Exception as exc:
        cached = _read_cache()
        if cached is not None:
            cached["stale"] = True
            return cached
        raise RuntimeError(f"No se pudo obtener uso de Copilot y no hay cache previo: {exc}")


if __name__ == "__main__":
    print(get_usage())
