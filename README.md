# SwiftBar Usage Indicators

Menu bar pills for macOS that show your **Claude Code** and **GitHub Copilot** usage at a
glance, rendered as native images by [SwiftBar](https://swiftbar.github.io/).

- **Claude pill**: orange that turns red as you approach your limit
- **Copilot pill**: green that turns red as premium interactions fill up
- **Dropdown**: unicode progress bar, used vs total, remaining, reset time, last update
- **Stale marker**: a `!` suffix whenever the data could not be refreshed

## Requirements

- macOS on **Apple Silicon** (the bundled `pill_render` binary is arm64; on Intel, build it
  from source: `swiftc pill_render.swift -o pill_render`)
- [SwiftBar](https://swiftbar.github.io/) (`brew install swiftbar`)
- Python 3.9+
- For Copilot: [GitHub CLI](https://cli.github.com/) authenticated (`gh auth login`)
- For Claude: Claude Code credentials on this machine (credentials file or macOS Keychain)

## Install

```bash
git clone https://github.com/<your-user>/swiftbar-usage-indicators.git
cd swiftbar-usage-indicators

# 1. Runtime (fetchers + pill renderer) -> ~/indicators
mkdir -p ~/indicators
cp -R indicators/. ~/indicators/
chmod +x ~/indicators/pill_render

# 2. SwiftBar plugins
PLUGINS="$HOME/Library/Application Support/SwiftBar/Plugins"
mkdir -p "$PLUGINS"
cp plugins/*.py "$PLUGINS/"
chmod +x "$PLUGINS"/*.py

# 3. Restart SwiftBar
pkill SwiftBar; open -a SwiftBar
```

The plugins look for the runtime at exactly `~/indicators`. If you want another location,
edit the `sys.path.insert(...)` and `PILL_RENDER` lines at the top of each plugin.

## How it works

Each plugin refreshes every 10s and always exits with code 0, so SwiftBar never hides it.

- **Data**: small stdlib-only fetchers with aggressive caching.
  - `indicators/copilot-indicator/fetcher.py` reads the token via `gh auth token`
    (memory only, never written to disk), calls
    `api.github.com/copilot_internal/user`, and caches to
    `~/.cache/copilot-usage.json` (30s).
  - `indicators/claude-usage/fetcher.py` calls the Claude Code OAuth usage endpoint with
    a token from the credentials file or macOS Keychain (memory only), throttled to one
    network call per 120s with 429 backoff, caching to `~/.cache/claude-usage.json`.
    Falls back to a rolling 5h estimate from local transcripts (always marked stale).
- **Pill**: `indicators/pill_render` (Swift + AppKit) draws the rounded pill with SF Bold
  white text and outputs a base64 PNG that SwiftBar embeds in line 1. If the renderer
  fails, the plugins fall back to ANSI 256-color text.

## Screenshots

<!-- Generated with Playwright from a local demo page; output lives in screenshots/ -->

_Coming soon._

## Colors

| Service  | Ramps                                              |
| -------- | -------------------------------------------------- |
| Claude   | `<80%` `#D97757` · `80-89%` `#E06A45` · `>=90%` `#E5484D` |
| Copilot  | `<50%` `#2E7D4F` · `50-69%` `#6B8E3D` · `70-84%` `#C77B3F` · `>=85%` `#E5484D` |
