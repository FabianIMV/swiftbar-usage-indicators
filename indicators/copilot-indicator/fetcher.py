"""GitHub Copilot premium-interactions usage fetcher. Standard library only.

Caches to ~/.cache/copilot-usage.json with the keys:
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
    """Get a token via `gh auth token`. Kept in memory only, never written to disk."""
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError("gh is not installed or not on PATH. Install gh and run `gh auth login`.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("`gh auth token` timed out (15s).")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            "Could not get a token: `gh auth token` failed. "
            "Run `gh auth login` / `gh auth refresh`. Details: " + (err or f"exit {proc.returncode}")
        )
    token = proc.stdout.strip()
    if not token:
        raise RuntimeError("`gh auth token` returned no token. Run `gh auth login` again.")
    return token


def fetch_raw(token: str) -> dict:
    """GET the internal Copilot endpoint and return the parsed dict."""
    if not token:
        raise RuntimeError("fetch_raw received an empty token.")
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
        raise RuntimeError(f"Network error requesting {API_URL}: {exc}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response from the Copilot endpoint: {exc}")
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected response from the Copilot endpoint (not a JSON object).")
    return data


def parse_usage(data: dict) -> dict:
    """Extract premium_interactions and build the cache-contract dict."""
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
    """Token + fetch + parse, then write the cache atomically.

    On a network/API failure, return the last cache with stale=true and its
    original updated_at. Without a previous cache, the error propagates.
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
        raise RuntimeError(f"Could not fetch Copilot usage and there is no previous cache: {exc}")


if __name__ == "__main__":
    print(json.dumps(get_usage()))
