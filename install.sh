#!/usr/bin/env bash
# Installs the runtime to ~/indicators and the plugins to SwiftBar's plugin folder.
#
#   ./install.sh              install or update
#   ./install.sh --uninstall  remove the runtime, the plugins and the caches
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$HOME/indicators"
PLUGIN_DIR="${SWIFTBAR_PLUGIN_DIR:-$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || echo "$HOME/Library/Application Support/SwiftBar/Plugins")}"
PLUGINS=(1-claude.10s.py 2-copilot.10s.py)

restart_swiftbar() {
  if pgrep -x SwiftBar >/dev/null; then
    pkill -x SwiftBar || true
    sleep 1
  fi
  open -a SwiftBar 2>/dev/null || echo "SwiftBar is not installed: brew install --cask swiftbar"
}

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$RUNTIME_DIR/claude-usage" "$RUNTIME_DIR/copilot-indicator" \
         "$RUNTIME_DIR/pill_render" "$RUNTIME_DIR/pill_render.swift"
  rmdir "$RUNTIME_DIR" 2>/dev/null || true
  for plugin in "${PLUGINS[@]}"; do rm -f "$PLUGIN_DIR/$plugin"; done
  rm -f "$HOME/.cache/claude-usage.json" "$HOME/.cache/claude-usage.next" "$HOME/.cache/copilot-usage.json"
  echo "Uninstalled."
  restart_swiftbar
  exit 0
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: this project only runs on macOS." >&2
  exit 1
fi

command -v python3 >/dev/null || { echo "error: python3 not found." >&2; exit 1; }
command -v swiftc >/dev/null || { echo "error: swiftc not found. Run: xcode-select --install" >&2; exit 1; }

echo "==> Building pill_render"
swiftc -O "$REPO_DIR/indicators/pill_render.swift" -o "$REPO_DIR/indicators/pill_render"

echo "==> Installing runtime to $RUNTIME_DIR"
mkdir -p "$RUNTIME_DIR"
cp -R "$REPO_DIR/indicators/." "$RUNTIME_DIR/"
chmod +x "$RUNTIME_DIR/pill_render"

echo "==> Installing plugins to $PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
for plugin in "${PLUGINS[@]}"; do
  cp "$REPO_DIR/plugins/$plugin" "$PLUGIN_DIR/"
  chmod +x "$PLUGIN_DIR/$plugin"
done

command -v gh >/dev/null || echo "note: GitHub CLI not found; the Copilot pill needs it (brew install gh && gh auth login)."

echo "==> Restarting SwiftBar"
restart_swiftbar
echo "Done."
