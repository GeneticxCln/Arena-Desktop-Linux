#!/usr/bin/env bash
# Build Linux distribution packages from an already-built one-file binary.
#
#   ./packaging/build-packages.sh                 # deb + rpm + Arch + AppImage
#   VERSION=6.0.1 ./packaging/build-packages.sh
#   SKIP_APPIMAGE=1 ./packaging/build-packages.sh
#   SKIP_ARCH=1 ./packaging/build-packages.sh
#
# The binary is produced by ./build-linux.sh. nFPM and appimagetool are
# downloaded into build/tools on first use (both are static tools).
set -euo pipefail
cd "$(dirname "$0")/.."

ROOT="$(pwd)"
MACHINE="$(uname -m)"
case "$MACHINE" in
  x86_64|amd64)
    NFPM_ARCH="amd64"; RPM_ARCH="x86_64"; APPIMAGE_ARCH="x86_64"; NFPM_MACHINE="x86_64"
    DEFAULT_BINARY="dist/Arena-Desktop" ;;
  aarch64|arm64)
    NFPM_ARCH="arm64"; RPM_ARCH="aarch64"; APPIMAGE_ARCH="aarch64"; NFPM_MACHINE="arm64"
    DEFAULT_BINARY="dist/Arena-Desktop-arm64" ;;
  *)
    echo "Unsupported architecture: $MACHINE" >&2
    exit 1 ;;
esac
BINARY="${BINARY:-$DEFAULT_BINARY}"
VERSION="${VERSION:-$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' arena/web/app.py | head -1)}"
VERSION="${VERSION:-7.4.1}"
MAINTAINER="${MAINTAINER:-Quinton Titus <93550885+GeneticxCln@users.noreply.github.com>}"
VENDOR="${VENDOR:-Quinton Titus}"
HOMEPAGE="${HOMEPAGE:-https://github.com/GeneticxCln/Arena-Desktop-Linux}"
NFPM_VERSION="${NFPM_VERSION:-2.49.0}"
NFPM_SHA256_X86_64="2afaa5ffb5e3493402e31e7c67a5a1fb42133e3f66fc315ca0051934125561c7"
NFPM_SHA256="${NFPM_SHA256:-}"
APPIMAGETOOL_SHA256="${APPIMAGETOOL_SHA256:-}"

verify_sha256() {
  local file="$1"
  local expected="$2"
  local label="$3"
  local actual
  actual="$(sha256sum "$file" | awk '{print $1}')"
  if [ "$actual" != "$expected" ]; then
    echo "SHA-256 mismatch for $label: expected $expected, got $actual" >&2
    rm -f "$file"
    exit 1
  fi
}

STAGE="release/Arena-Desktop-linux-${MACHINE}"
TOOLS="build/tools"
mkdir -p "$STAGE" "$TOOLS"

if [ ! -x "$BINARY" ]; then
  echo "Missing $BINARY — run ./build-linux.sh first." >&2
  exit 1
fi

# --- nFPM (deb + rpm) -------------------------------------------------------
NFPM="${NFPM:-$TOOLS/nfpm}"
if [ ! -x "$NFPM" ]; then
  echo "Downloading nFPM $NFPM_VERSION..."
  curl -fsSL -o "$TOOLS/nfpm.tar.gz" \
    "https://github.com/goreleaser/nfpm/releases/download/v${NFPM_VERSION}/nfpm_${NFPM_VERSION}_Linux_${NFPM_MACHINE}.tar.gz"
  EXPECTED_NFPM_SHA="$NFPM_SHA256"
  if [ -z "$EXPECTED_NFPM_SHA" ] && [ "$NFPM_VERSION" = "2.49.0" ] && [ "$NFPM_MACHINE" = "x86_64" ]; then
    EXPECTED_NFPM_SHA="$NFPM_SHA256_X86_64"
  fi
  if [ -n "$EXPECTED_NFPM_SHA" ]; then
    verify_sha256 "$TOOLS/nfpm.tar.gz" "$EXPECTED_NFPM_SHA" "nfpm_${NFPM_VERSION}_Linux_${NFPM_MACHINE}.tar.gz"
  fi
  tar -xzf "$TOOLS/nfpm.tar.gz" -C "$TOOLS" nfpm
  chmod +x "$NFPM"
fi

# Package desktop entry: the binary is on PATH as /usr/bin/Arena-Desktop.
sed 's|@EXEC@|/usr/bin/Arena-Desktop|g' packaging/arena-desktop.desktop > build/arena-desktop.desktop
chmod 644 build/arena-desktop.desktop

sed \
  -e "s|__ROOT__|$ROOT|g" \
  -e "s|__BINARY__|$ROOT/$BINARY|g" \
  -e "s|__DESKTOP_FILE__|$ROOT/build/arena-desktop.desktop|g" \
  -e "s|__NFPM_ARCH__|$NFPM_ARCH|g" \
  -e "s|__VERSION__|$VERSION|g" \
  -e "s|__MAINTAINER__|$MAINTAINER|g" \
  -e "s|__VENDOR__|$VENDOR|g" \
  -e "s|__HOMEPAGE__|$HOMEPAGE|g" \
  packaging/nfpm.yaml > build/nfpm.yaml

echo "Building .deb and .rpm..."
"$NFPM" package -f build/nfpm.yaml -p deb -t "$STAGE/arena-desktop_${VERSION}_${NFPM_ARCH}.deb"
"$NFPM" package -f build/nfpm.yaml -p rpm -t "$STAGE/arena-desktop-${VERSION}-1.${RPM_ARCH}.rpm"

# --- Arch Linux (.pkg.tar.zst) ----------------------------------------------
if [ "${SKIP_ARCH:-0}" != "1" ]; then
  if ! command -v zstd >/dev/null 2>&1; then
    echo "zstd is required to build the Arch package." >&2
    exit 1
  fi

  echo "Building Arch package..."
  ARCHROOT="$ROOT/build/archpkg"
  rm -rf "$ARCHROOT"
  install -d "$ARCHROOT/usr/share/applications"
  install -Dm755 "$BINARY" "$ARCHROOT/usr/bin/Arena-Desktop"
  install -Dm644 assets/logo.png \
    "$ARCHROOT/usr/share/icons/hicolor/256x256/apps/arena-desktop.png"
  sed 's|@EXEC@|/usr/bin/Arena-Desktop|g' packaging/arena-desktop.desktop \
    > "$ARCHROOT/usr/share/applications/arena-desktop.desktop"
  chmod 644 "$ARCHROOT/usr/share/applications/arena-desktop.desktop"
  install -Dm644 README_LINUX.md "$ARCHROOT/usr/share/doc/arena-desktop/README.md"
  install -Dm644 LICENSE "$ARCHROOT/usr/share/doc/arena-desktop/LICENSE"

  cat > "$ARCHROOT/.PKGINFO" <<EOF
pkgname = arena-desktop
pkgbase = arena-desktop
pkgver = ${VERSION}-1
pkgdesc = Unofficial desktop client for arena.ai with a native Linux shell
url = ${HOMEPAGE}
builddate = $(date +%s)
packager = ${MAINTAINER}
size = $(du -sb "$ARCHROOT/usr" | cut -f1)
arch = ${RPM_ARCH}
license = MIT
depend = webkit2gtk-4.1
depend = gtk3
EOF

  ( cd "$ARCHROOT" && tar --zstd -cf \
      "$ROOT/$STAGE/arena-desktop-${VERSION}-1-${RPM_ARCH}.pkg.tar.zst" \
      .PKGINFO usr )
fi

# --- AppImage ---------------------------------------------------------------
if [ "${SKIP_APPIMAGE:-0}" != "1" ]; then
  APPIMAGETOOL="${APPIMAGETOOL:-$TOOLS/appimagetool}"
  if [ ! -x "$APPIMAGETOOL" ]; then
    echo "Downloading appimagetool..."
    curl -fsSL -o "$APPIMAGETOOL" \
      "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${APPIMAGE_ARCH}.AppImage"
    if [ -n "$APPIMAGETOOL_SHA256" ]; then
      verify_sha256 "$APPIMAGETOOL" "$APPIMAGETOOL_SHA256" "appimagetool-${APPIMAGE_ARCH}.AppImage"
    fi
    chmod +x "$APPIMAGETOOL"
  fi

  APPDIR="$ROOT/build/AppDir"
  rm -rf "$APPDIR"
  install -Dm755 "$BINARY" "$APPDIR/usr/bin/Arena-Desktop"
  install -Dm755 packaging/AppRun "$APPDIR/AppRun"
  install -Dm644 assets/logo.png "$APPDIR/arena-desktop.png"
  install -Dm644 assets/logo.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/arena-desktop.png"
  sed 's|@EXEC@|Arena-Desktop|g' packaging/arena-desktop.desktop > "$APPDIR/arena-desktop.desktop"
  install -Dm644 "$APPDIR/arena-desktop.desktop" "$APPDIR/usr/share/applications/arena-desktop.desktop"

  echo "Building AppImage..."
  ARCH="$APPIMAGE_ARCH" APPIMAGE_EXTRACT_AND_RUN=1 \
    "$APPIMAGETOOL" --no-appstream "$APPDIR" \
    "$ROOT/$STAGE/Arena-Desktop-${VERSION}-linux-${APPIMAGE_ARCH}.AppImage"
  chmod +x "$STAGE/Arena-Desktop-${VERSION}-linux-${APPIMAGE_ARCH}.AppImage"
fi

# --- checksums --------------------------------------------------------------
( cd "$STAGE"
  : > SHA256SUMS
  for f in *.deb *.rpm *.pkg.tar.zst *.AppImage; do
    if [ -f "$f" ]; then
      sha256sum "$f" >> SHA256SUMS
    fi
  done )

echo
echo "Packages in $STAGE:"
ls -lh "$STAGE"
