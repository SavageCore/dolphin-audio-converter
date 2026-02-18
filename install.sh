#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  install.sh - Dolphin Audio Converter installer
#  Installs the backend + KDE service-menu .desktop for the current user.
#  No root required.
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

RED=$'\033[0;31m' GREEN=$'\033[0;32m' YELLOW=$'\033[1;33m'
CYAN=$'\033[0;36m' BOLD=$'\033[1m' RESET=$'\033[0m'

info()    { echo "${CYAN}▸${RESET} $*"; }
success() { echo "${GREEN}✔${RESET} $*"; }
warn()    { echo "${YELLOW}⚠${RESET} $*"; }
error()   { echo "${RED}✘${RESET} $*" >&2; }
header()  { echo; echo "${BOLD}${CYAN}$*${RESET}"; printf '─%.0s' {1..60}; echo; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_SRC="$SCRIPT_DIR/dolphin-audio-converter.py"
DESKTOP_SRC="$SCRIPT_DIR/dolphin-audio-converter.desktop"

BIN_DIR="$HOME/.local/bin"
BIN_DEST="$BIN_DIR/dolphin-audio-converter"

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


# ══════════════════════════════════════════════════════════════════════════════
#  Commands
# ══════════════════════════════════════════════════════════════════════════════

install_app() {
    header "Dolphin Audio Converter - Installer"

    header "Checking dependencies"

    MISSING=()

    if ! command -v python3 &>/dev/null; then
        MISSING+=("python3")
    else
        success "python3 $(python3 --version 2>&1 | grep -oP '[\d.]+')"
    fi

    if ! command -v ffmpeg &>/dev/null; then
        warn "ffmpeg not found - install it for conversions to work:"
        warn "  sudo apt install ffmpeg      (Debian/Ubuntu/Mint)"
        warn "  sudo dnf install ffmpeg      (Fedora)"
        warn "  sudo pacman -S ffmpeg        (Arch/Manjaro)"
        warn "  sudo zypper install ffmpeg   (openSUSE)"
    else
        success "ffmpeg $(ffmpeg -version 2>&1 | grep -oP 'ffmpeg version \S+')"
    fi

    if ! command -v kdialog &>/dev/null; then
        MISSING+=("kdialog")
    else
        success "kdialog found"
    fi

    QDBUS_BIN=""
    for q in qdbus-qt5 qdbus qdbus6; do
        if command -v "$q" &>/dev/null; then
            QDBUS_BIN="$q"
            break
        fi
    done
    if [[ -z "$QDBUS_BIN" ]]; then
        warn "qdbus not found - progress bar will open but won't animate"
        warn "Install: sudo apt install qdbus  (or qt6-tools)"
    else
        success "$QDBUS_BIN found"
    fi

    if ! command -v notify-send &>/dev/null; then
        warn "notify-send not found - completion popups will be skipped"
        warn "Install: sudo apt install libnotify-bin"
    else
        success "notify-send found"
    fi

    if [[ ${#MISSING[@]} -gt 0 ]]; then
        error "Required tools missing: ${MISSING[*]}"
        error "Install them and re-run."
        exit 1
    fi

    header "Verifying source files"
    [[ -f "$BACKEND_SRC" ]] || { error "Backend not found: $BACKEND_SRC"; exit 1; }
    [[ -f "$DESKTOP_SRC" ]] || { error "Desktop file not found: $DESKTOP_SRC"; exit 1; }
    success "Source files OK"

    header "Installing backend"
    mkdir -p "$BIN_DIR"
    cp "$BACKEND_SRC" "$BIN_DEST"
    chmod +x "$BIN_DEST"
    success "Backend → $BIN_DEST"

    header "Installing service menu"
    mkdir -p "$SERVICEMENU_DIR"
    sed "s|__USER_HOME__|${HOME}|g" "$DESKTOP_SRC" > "$DESKTOP_DEST"
    chmod +x "$DESKTOP_DEST"
    success "Service menu → $DESKTOP_DEST"

    header "Applying saved quality settings"
    CONFIG_FILE="$HOME/.config/dolphin-audio-converter/config.json"
    if [[ -f "$CONFIG_FILE" ]]; then
        python3 - <<'PYEOF'
import json, re
from pathlib import Path

CONFIG_FILE = Path.home() / ".config" / "dolphin-audio-converter" / "config.json"
DESKTOP_PATHS = [
    Path.home() / ".local" / "share" / "kio"        / "servicemenus" / "dolphin-audio-converter.desktop",
    Path.home() / ".local" / "share" / "kservices5" / "ServiceMenus"  / "dolphin-audio-converter.desktop",
]

def find_desktop():
    for p in DESKTOP_PATHS:
        if p.exists(): return p
    return None

if CONFIG_FILE.exists():
    cfg = json.loads(CONFIG_FILE.read_text())
    def ql(q): return f" ({q})" if q != "lossless" else ""
    name_map = {
        "convertToMp3":  f"Convert to MP3{ql(cfg.get('mp3','V0'))}",
        "convertToOgg":  f"Convert to OGG{ql(cfg.get('ogg','Q6'))}",
        "convertToFlac": "Convert to FLAC",
        "convertToWav":  "Convert to WAV",
        "convertToM4a":  f"Convert to M4A (AAC){ql(cfg.get('m4a','192k'))}",
        "convertToOpus": f"Convert to Opus{ql(cfg.get('opus','128k'))}",
        "convertToAlac": "Convert to ALAC (M4A)",
    }
    desktop = find_desktop()
    if desktop:
        lines = desktop.read_text().splitlines()
        cur, out = None, []
        for line in lines:
            m = re.match(r'^\[Desktop Action (\w+)\]', line)
            if m: cur = m.group(1)
            if cur and line.startswith("Name=") and cur in name_map:
                line = f"Name={name_map[cur]}"
            out.append(line)
        desktop.write_text("\n".join(out) + "\n")
        print("  Menu labels updated from saved config.")
PYEOF
        success "Labels applied"
    else
        success "No existing config - using defaults"
    fi

    header "Refreshing KDE service menus"
    if command -v kbuildsycoca6 &>/dev/null; then
        kbuildsycoca6 --noincremental 2>/dev/null && success "kbuildsycoca6 cache rebuilt"
    elif command -v kbuildsycoca5 &>/dev/null; then
        kbuildsycoca5 --noincremental 2>/dev/null && success "kbuildsycoca5 cache rebuilt"
    else
        warn "kbuildsycoca not found - you may need to log out/in for the menu to appear"
    fi

    echo
    echo "${BOLD}${GREEN}════════════════════════════════════════════════════════════${RESET}"
    echo "${BOLD}${GREEN}  Installation complete!${RESET}"
    echo "${BOLD}${GREEN}════════════════════════════════════════════════════════════${RESET}"
    echo
    echo "  Right-click any audio/video file in Dolphin → ${BOLD}Audio Converter${RESET}"
    echo "  Use ${BOLD}Configure…${RESET} to change quality (labels update live)"
    echo
    echo "  Backend : $BIN_DEST"
    echo "  Menu    : $DESKTOP_DEST"
    echo "  Config  : $HOME/.config/dolphin-audio-converter/config.json"
    echo
}

uninstall_app() {
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
}

# Main
if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall_app
else
    install_app
fi

