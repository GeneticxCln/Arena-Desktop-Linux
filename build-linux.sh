#!/usr/bin/env bash
# Build the single-file Linux version of Arena Desktop (WebKitGTK backend).
#
# Usage:
#   ./build-linux.sh                # tests + build
#   SKIP_TESTS=1 ./build-linux.sh   # build only
#   PYTHON=python3.12 ./build-linux.sh
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV="${VENV:-.venv}"
MACHINE="$(uname -m)"
case "$MACHINE" in
  aarch64|arm64) NAME="Arena-Desktop-arm64" ;;
  *)             NAME="Arena-Desktop" ;;
esac

say() { printf '\n== %s ==\n' "$*"; }

say "Checking build prerequisites"
if ! "$PYTHON" - <<'PY'
import sys
try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gtk, WebKit2  # noqa: F401
except Exception as exc:
    print(f"WebKitGTK 4.1 / PyGObject not usable: {exc}", file=sys.stderr)
    raise SystemExit(1)
print(f"WebKitGTK 4.1 + PyGObject OK (Python {sys.version.split()[0]})")
PY
then
  cat >&2 <<'MSG'

Install the GTK/WebKitGTK runtime and PyGObject first:
  Arch/CachyOS:   sudo pacman -S --needed webkit2gtk-4.1 python-gobject
  Debian/Ubuntu:  sudo apt install python3-gi gir1.2-webkit2-4.1 gir1.2-gtk-3.0
  Fedora:         sudo dnf install python3-gobject gtk3 webkit2gtk4.1

Then run ./build-linux.sh again.
MSG
  exit 1
fi

say "Preparing Python environment"
if [ ! -x "$VENV/bin/python" ]; then
  # --system-site-packages is required: distro PyGObject lives outside the venv.
  "$PYTHON" -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r requirements.txt "pyinstaller>=6,<7"

if [ "${SKIP_TESTS:-0}" != "1" ]; then
  say "Running tests"
  "$VENV/bin/python" -m pip install "pytest>=8"
  "$VENV/bin/python" -m pytest -q tests --ignore=tests/test_ui_browser.py --ignore=tests/test_v58_browser.py
fi

say "Building $NAME"
"$VENV/bin/python" -m PyInstaller --clean --noconfirm arena-cli.spec

if [ ! -x "dist/$NAME" ]; then
  echo "Build finished without dist/$NAME" >&2
  exit 1
fi

# PyInstaller's Gtk hook would copy the build host's icon themes, but it does
# not preserve symlinks; a broken theme makes GTK abort in file dialogs.
if "$VENV/bin/pyi-archive_viewer" -l "dist/$NAME" 2>/dev/null | grep -q "'share/icons/"; then
  echo "dist/$NAME unexpectedly contains host icon themes; see arena-cli.spec." >&2
  exit 1
fi

say "Staging release"
STAGE="release/${NAME}-linux-${MACHINE}"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp "dist/$NAME" "$STAGE/Arena-Desktop"
chmod +x "$STAGE/Arena-Desktop"
cp README_LINUX.md "$STAGE/README.md"
cp LICENSE "$STAGE/LICENSE"
cp assets/logo.png "$STAGE/arena-desktop.png"
cp packaging/arena-desktop.desktop "$STAGE/arena-desktop.desktop"
cp packaging/install-linux.sh "$STAGE/install-linux.sh"
chmod +x "$STAGE/install-linux.sh"
tar -C release -czf "${STAGE}.tar.gz" "$(basename "$STAGE")"

printf '\nBuilt: %s (%s)\n' "dist/$NAME" "$(du -h "dist/$NAME" | cut -f1)"
printf 'Release folder: %s\n' "$STAGE"
printf 'Archive: %s\n' "${STAGE}.tar.gz"
