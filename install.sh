#!/usr/bin/env bash
# Idempotent user-local install. No root; everything lands under $HOME.
# Re-running is safe: each item is written only when it differs from what is
# already there, and the script reports which ones it touched.
set -uo pipefail

APP_ID="com.hawwwran.pauzak"
ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
BIN_DIR="$HOME/.local/bin"

DESKTOP="$APPS_DIR/$APP_ID.desktop"
ICON="$ICON_DIR/$APP_ID.svg"
LAUNCHER="$BIN_DIR/pauzak"

changed=0

say()     { printf '  %-9s %s\n' "$1" "$2"; }
changed() { changed=1; say "$1" "$2"; }

check_deps() {
    local missing=()
    python3 - <<'PY' 2>/dev/null || missing+=("python3-gi gir1.2-gtk-4.0 gir1.2-adw-1")
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: F401
PY
    python3 -c 'import Xlib' 2>/dev/null || missing+=("python3-xlib")
    command -v xdg-open >/dev/null 2>&1 || true

    # Smart snap and the visual diff need these two; everything else works
    # without them, so they are reported rather than treated as missing.
    local optional=()
    python3 -c 'import numpy' 2>/dev/null || optional+=("python3-numpy")
    command -v gnome-screenshot >/dev/null 2>&1 || optional+=("gnome-screenshot")
    if [ ${#optional[@]} -gt 0 ]; then
        echo "Smart snap and visual diff need: sudo apt install ${optional[*]}" >&2
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        echo "Missing dependencies. Install them with:" >&2
        echo "    sudo apt install ${missing[*]}" >&2
        return 1
    fi
    return 0
}

# Writes stdin to $1 only when the contents differ, so mtimes stay put on a
# re-run and the summary tells the truth.
install_file() {
    local target="$1" label="$2" tmp
    tmp="$(mktemp)"
    cat > "$tmp"
    if [ -f "$target" ] && cmp -s "$tmp" "$target"; then
        rm -f "$tmp"
        say "current" "$label"
    else
        local verb="installed"
        [ -e "$target" ] && verb="updated"
        mkdir -p "$(dirname "$target")"
        mv "$tmp" "$target"
        chmod 644 "$target"
        changed "$verb" "$label"
    fi
}

echo "pauzak install"
echo "  source: $ROOT"

check_deps || exit 1

sed "s|@EXEC@|$ROOT/bin/pauzak|g" "$ROOT/data/$APP_ID.desktop.in" | install_file "$DESKTOP" "$DESKTOP"
install_file "$ICON" "$ICON" < "$ROOT/data/$APP_ID.svg"

chmod +x "$ROOT/bin/pauzak"

mkdir -p "$BIN_DIR"
if [ "$(readlink -f "$LAUNCHER" 2>/dev/null)" = "$ROOT/bin/pauzak" ]; then
    say "current" "$LAUNCHER"
elif [ -e "$LAUNCHER" ] && [ ! -L "$LAUNCHER" ]; then
    say "skipped" "$LAUNCHER exists and is not a symlink; left alone"
else
    ln -sfn "$ROOT/bin/pauzak" "$LAUNCHER"
    changed "linked" "$LAUNCHER"
fi

if [ "$changed" -eq 1 ]; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
    gtk-update-icon-cache -f -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
    say "refreshed" "desktop and icon caches"
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "  note      $BIN_DIR is not on your PATH; run $ROOT/bin/pauzak directly" ;;
esac

echo "Done. Launch it from the app grid, or run: pauzak [image...]"
