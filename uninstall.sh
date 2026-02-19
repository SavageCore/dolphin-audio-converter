#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  uninstall.sh - Dolphin Audio Converter uninstaller
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

RED=$'\033[0;31m' GREEN=$'\033[0;32m' YELLOW=$'\033[1;33m'
CYAN=$'\033[0;36m' BOLD=$'\033[1m' RESET=$'\033[0m'

success() { echo "${GREEN}✔${RESET} $*"; }
warn()    { echo "${YELLOW}⚠${RESET} $*"; }
header()  { echo; echo "${BOLD}${CYAN}$*${RESET}"; printf '─%.0s' {1..60}; echo; }

BIN_DEST="$HOME/.local/bin/dolphin-audio-converter"

# Plasma 6 → kio/servicemenus  |  Plasma 5 → kservices5/ServiceMenus
detect_servicemenu_dir() {
    local ver
    ver=$(plasmashell --version 2>/dev/null | grep -oP '\d+' | head -1 || echo "5")
    if [[ "$ver" -ge 6 ]]; then
        echo "$HOME/.local/share/kio/servicemenus"
    else
        echo "$HOME/.local/share/kservices5/ServiceMenus"
    fi
}

SERVICEMENU_DIR="$(detect_servicemenu_dir)"
DESKTOP_DEST="$SERVICEMENU_DIR/dolphin-audio-converter.desktop"

header "Dolphin Audio Converter - Uninstall"

if [[ -f "$BIN_DEST" ]]; then
    rm "$BIN_DEST"
    success "Removed backend: $BIN_DEST"
else
    warn "Backend not found: $BIN_DEST"
fi

if [[ -f "$DESKTOP_DEST" ]]; then
    rm "$DESKTOP_DEST"
    success "Removed service menu: $DESKTOP_DEST"
else
    warn "Service menu not found: $DESKTOP_DEST"
fi

CONFIG_DIR="$HOME/.config/dolphin-audio-converter"
if [[ -d "$CONFIG_DIR" ]]; then
    rm -rf "$CONFIG_DIR"
    success "Removed config: $CONFIG_DIR"
fi

header "Refreshing KDE service menus"
if command -v kbuildsycoca6 &>/dev/null; then
    kbuildsycoca6 --noincremental 2>/dev/null && success "kbuildsycoca6 cache rebuilt"
elif command -v kbuildsycoca5 &>/dev/null; then
    kbuildsycoca5 --noincremental 2>/dev/null && success "kbuildsycoca5 cache rebuilt"
fi

echo
success "Uninstalled successfully."
