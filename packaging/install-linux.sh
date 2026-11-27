#!/usr/bin/env bash
# Install Arena Desktop for the current Linux user.
#
# Run from the extracted release folder (next to Arena-Desktop and
# arena-desktop.desktop):
#   ./install-linux.sh
#
# BIN_DIR / APP_DIR / ICON_DIR can be overridden.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
APP_DIR="${APP_DIR:-$HOME/.local/share/applications}"
ICON_DIR="${ICON_DIR:-$HOME/.local/share/icons/hicolor/256x256/apps}"

BIN="$HERE/Arena-Desktop"
if [ ! -x "$BIN" ]; then
  if [ -x "$HERE/../dist/Arena-Desktop" ]; then
    BIN="$HERE/../dist/Arena-Desktop"
  else
    echo "Arena-Desktop binary not found next to this script." >&2
    exit 1
  fi
fi

mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"
install -m 755 "$BIN" "$BIN_DIR/Arena-Desktop"

ICON="$HERE/arena-desktop.png"
if [ ! -f "$ICON" ] && [ -f "$HERE/logo.png" ]; then ICON="$HERE/logo.png"; fi
if [ ! -f "$ICON" ] && [ -f "$HERE/../assets/logo.png" ]; then ICON="$HERE/../assets/logo.png"; fi
if [ -f "$ICON" ]; then
  install -m 644 "$ICON" "$ICON_DIR/arena-desktop.png"
fi

TEMPLATE="$HERE/arena-desktop.desktop"
if [ ! -f "$TEMPLATE" ]; then
  echo "arena-desktop.desktop not found next to this script." >&2
  exit 1
fi
EXEC_ESCAPED="\"$(printf '%s' "$BIN_DIR" | sed 's/\\/\\\\/g; s/"/\\"/g')/Arena-Desktop\""
EXEC_VAL="$EXEC_ESCAPED" awk '
  BEGIN { r = ENVIRON["EXEC_VAL"] }
  {
    while ((i = index($0, "@EXEC@")) > 0) {
      printf "%s%s", substr($0, 1, i - 1), r
      $0 = substr($0, i + 6)
    }
    print $0
  }
' "$TEMPLATE" > "$APP_DIR/arena-desktop.desktop"
chmod 644 "$APP_DIR/arena-desktop.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

cat <<EOF
Installed Arena Desktop:
  binary:  $BIN_DIR/Arena-Desktop
  entry:   $APP_DIR/arena-desktop.desktop
  icon:    $ICON_DIR/arena-desktop.png

Start it from your application menu or run: $BIN_DIR/Arena-Desktop
If $BIN_DIR is not on PATH, add it to your shell profile.
EOF
