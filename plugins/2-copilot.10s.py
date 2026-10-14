#!/usr/bin/env python3
"""SwiftBar plugin: GitHub Copilot premium interactions usage indicator.

Reads ~/.cache/copilot-usage.json and only hits the network when the cache is
older than 30s (based on its ISO updated_at key). Line 1 is a rounded pill
(pill_render, embedded as base64 via image=) in a muted dark green that steps
through olive, orange and red, with an ANSI 256-color fallback if the
renderer fails. Always exits 0.
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
    """Return the cache if it is a valid dict, otherwise None."""
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        return cached if isinstance(cached, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def parse_iso(value: object) -> datetime | None:
    """Parse ISO 8601, accepting a trailing Z and naive values (assumed UTC)."""
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
    """Cache age in seconds, or None if updated_at cannot be parsed."""
    updated = parse_iso(cached.get("updated_at"))
    if updated is None:
        return None
    return (datetime.now(timezone.utc) - updated).total_seconds()


def color_for(percent: float) -> int:
    """Green-to-red ramp in the 256-color palette over fixed percent_used bands.

    0-49 green 46, 50-59 yellow-green 148, 60-69 yellow 226,
    70-84 orange 208, 85-100 red 196. Returns the 256-color background code.
    Only used by the ANSI fallback.
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
    """Pill background hex per band: muted dark green, muted olive,
    muted orange, red."""
    if percent < 50:
        return "#2E7D4F"
    if percent < 70:
        return "#6B8E3D"
    if percent < 85:
        return "#C77B3F"
    return "#E5484D"


def pill_image_b64(text: str, base_hex: str) -> str | None:
    """Render the pill with pill_render; None on failure."""
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
    """Line 1: image pill, or ANSI text if the renderer fails."""
    b64 = pill_image_b64(text, base_hex)
    if b64 is not None:
        print("  | image=%s height=%s" % (b64, PILL_HEIGHT))
    else:
        print("\x1b[97;48;5;%dm %s \x1b[0m | ansi=true" % (ansi_bg, text))


def fmt_percent(value: object) -> str:
    """Format with at most one decimal, dropping a trailing .0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}"


def bar_for(percent: float) -> str:
    """10-block unicode bar: ▓ for filled, ░ for empty."""
    filled = int(round(percent / 10))
    filled = max(0, min(10, filled))
    return "▓" * filled + "░" * (10 - filled)


def emit_error(message: str) -> None:
    """Error line 1 (the caller still exits 0 so SwiftBar keeps the plugin)."""
    print("Copilot: " + message + " | color=red")


def main() -> int:
    cached = read_cache()
    stale = False
    data: dict | None = None

    # Fetch only when there is no cache or it is older than 30s; on failure,
    # fall back to the previous cache.
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
                emit_error("no data")
                sys.stderr.write("copilot plugin: fetch failed: %s\n" % exc)
                return 0
    else:
        data = dict(cached)

    if data is None:
        emit_error("no data")
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

    # Line 1: compact pill with just the percentage; details live in the
    # dropdown.
    text = "%s%%%s" % (fmt_percent(percent), suffix)
    emit_title(text, hex_for(percent), bg)
    print("---")
    # Progress bar tinted green to match the pill.
    print(bar_for(percent) + " | color=#2ecc71")
    print("Used: %s / %s" % (used, entitlement))
    print("Remaining: %s" % remaining)
    print("Reset: %s" % reset)
    print("Updated: %s" % updated_at)
    if stale:
        print("Stale: yes | color=#e74c3c")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        # Never fail: SwiftBar hides the plugin on a non-zero exit code.
        print("Copilot: no data | color=red")
        sys.stderr.write("copilot plugin: unexpected error: %s\n" % exc)
        sys.exit(0)
