#!/usr/bin/env bash
# Removes everything install.sh puts outside this directory, plus the traces
# the desktop environment adds on its own. Safe to run twice, and safe to run
# when pauzak was never installed. The checkout itself is left alone.
set -uo pipefail

APP_ID="com.hawwwran.pauzak"
ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
APPS_DIR="$DATA_HOME/applications"
HISTORY_DIR="$DATA_HOME/pauzak"

DESKTOP="$APPS_DIR/$APP_ID.desktop"
ICON="$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
LAUNCHER="$HOME/.local/bin/pauzak"

removed=0

say()     { printf '  %-9s %s\n' "$1" "$2"; }
dropped() { removed=1; say "removed" "$1"; }

remove_file() {
    if [ -e "$1" ]; then
        rm -f "$1" && dropped "$1"
    else
        say "absent" "$1"
    fi
}

echo "pauzak uninstall"

remove_file "$DESKTOP"
remove_file "$ICON"

# Only our own symlink: a file someone else put there keeps its place.
if [ -L "$LAUNCHER" ] && [ "$(readlink -f "$LAUNCHER" 2>/dev/null)" = "$ROOT/bin/pauzak" ]; then
    rm -f "$LAUNCHER" && dropped "$LAUNCHER"
elif [ -e "$LAUNCHER" ]; then
    say "skipped" "$LAUNCHER does not point at this checkout"
else
    say "absent" "$LAUNCHER"
fi

# The desktop entry declares MimeType, so associating pauzak with an image type
# (even once, from "Open With") writes our id into these lists and it survives
# deleting the .desktop file.
for list in "$CONFIG_HOME/mimeapps.list" "$APPS_DIR/mimeapps.list"; do
    [ -f "$list" ] || continue
    if python3 - "$list" "$APP_ID.desktop" <<'PY'
import sys

path, entry = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    lines = handle.readlines()

out, touched = [], False
for line in lines:
    stripped = line.strip()
    if "=" not in stripped or stripped.startswith(("#", "[")):
        out.append(line)
        continue
    key, _, value = stripped.partition("=")
    handlers = [v for v in value.split(";") if v]
    if entry not in handlers:
        out.append(line)
        continue
    touched = True
    kept = [v for v in handlers if v != entry]
    if kept:  # a key left with no handlers goes away entirely
        out.append(f"{key}={';'.join(kept)};\n")

if touched:
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(out)
sys.exit(0 if touched else 1)
PY
    then
        dropped "$list (mime associations)"
    else
        say "clean" "$list"
    fi
done

# The history strip's list of recently used images: written by the app rather
# than by install.sh, and the only state pauzak keeps.
remove_file "$HISTORY_DIR/history.json"
rmdir "$HISTORY_DIR" 2>/dev/null && dropped "$HISTORY_DIR"

# Byte-code caches are the only thing running the app leaves inside the tree.
if find "$ROOT" -name __pycache__ -type d -print -quit | grep -q .; then
    find "$ROOT" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
    dropped "__pycache__ directories in $ROOT"
else
    say "absent" "__pycache__ directories"
fi

if [ "$removed" -eq 1 ]; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
    gtk-update-icon-cache -f -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
    say "refreshed" "desktop and icon caches"
fi

echo "Done. Apart from that history list, pauzak stores no settings or state."
echo "The checkout at $ROOT is untouched; delete it to finish removing pauzak."
