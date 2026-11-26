# Arena Desktop on Linux

Arena Desktop now has a Linux desktop shell: the same local UI and Flask API,
the same arena.ai engine window, and the same workspace/tool features as the
Windows build. The window is drawn with **pywebview + WebKitGTK 4.1** instead
of Edge WebView2.

> Arena Desktop is an unofficial client. All generation happens on arena.ai
> with your own account, and human checks are completed by you.

## Status

Verified on **CachyOS (Arch-based), KDE Plasma Wayland, Python 3.14,
WebKitGTK 2.54** (`v7.1.0`):

- a ~86 MB single-file ELF binary starts, serves the local UI and loads
  `arena.ai` in the engine window;
- the non-browser test suite passes (`461 passed`);
- XDG deep links (`arenadesktop://open`, `new`, `ask`, `chat`) register on
  first launch of the built binary.

## Packages

Every release ships the same build in five Linux formats:

| Format | File | Install |
| --- | --- | --- |
| Debian / Ubuntu | `arena-desktop_<version>_amd64.deb` | `sudo apt install ./arena-desktop_<version>_amd64.deb` |
| Fedora / openSUSE | `arena-desktop-<version>-1.x86_64.rpm` | `sudo dnf install ./arena-desktop-<version>-1.x86_64.rpm` |
| Arch / CachyOS / Manjaro | `arena-desktop-<version>-1-x86_64.pkg.tar.zst` | `sudo pacman -U arena-desktop-<version>-1-x86_64.pkg.tar.zst` |
| Any distribution (portable) | `Arena-Desktop-<version>-linux-x86_64.AppImage` | `chmod +x ...AppImage && ./...AppImage` |
| Any distribution (manual) | `Arena-Desktop-linux-x86_64.tar.gz` | see below |

All packages declare WebKitGTK 4.1 and GTK 3 as dependencies, install the
binary to `/usr/bin/Arena-Desktop` and register the application-menu entry,
the icon and the `arenadesktop://` handler. `SHA256SUMS` in the release
covers every asset.

## Run the built binary

```bash
tar -xzf Arena-Desktop-linux-x86_64.tar.gz
cd Arena-Desktop-linux-x86_64
./Arena-Desktop
```

Or install it for the current user (binary, icon, application-menu entry and
`arenadesktop://` handler):

```bash
./install-linux.sh
# then start "Arena Desktop" from the application menu
```

The first launch creates `~/.arena-personal/` with settings, chats and the
WebKit profile. The application log is
`~/.arena-personal/desktop.log`.

### Runtime requirements

The binary bundles Python, PyGObject, GTK 3 and the rest of the app, but
WebKitGTK is a system component (its web/network helper processes live outside
the binary). Install it if the app reports a missing browser window:

| Distribution | Packages |
| --- | --- |
| Arch / CachyOS / Manjaro | `webkit2gtk-4.1` and `python-gobject` |
| Debian / Ubuntu | `python3-gi gir1.2-webkit2-4.1 gir1.2-gtk-3.0` |
| Fedora | `python3-gobject gtk3 webkit2gtk4.1` |
| openSUSE | `python3-gobject WebKit2GTK-4.1` |

## Run from source

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python arena-cli.py
```

`--system-site-packages` is required because the distro provides PyGObject
(`gi`). The terminal CLI still works too:

```bash
.venv/bin/python arena-cli.py --cli
```

## Build the Linux binary

```bash
./build-linux.sh                # tests + PyInstaller build
SKIP_TESTS=1 ./build-linux.sh   # skip the test run
```

The script checks the GTK/WebKit prerequisites, prepares `.venv`, runs the
non-browser tests and produces:

```
dist/Arena-Desktop                          # single-file ELF
release/Arena-Desktop-linux-x86_64/         # binary + installer + README
release/Arena-Desktop-linux-x86_64.tar.gz
```

The PyInstaller spec (`arena-cli.spec`) builds exclusively for Linux using the
WebKitGTK backend.

## Linux Integration & Behavior

- **Sign-in / human-check window**: The embedded `arena.ai` WebKit view opens
  as a separate window in front of the app whenever sign-in or a human check
  needs attention.
- **System tray**: Off by default. If enabled in Settings, uses `pystray`'s
  AppIndicator/GTK backend (requires StatusNotifier/AppIndicator support) and
  offers Quick Ask from the tray menu.
- **Cookie encryption at rest**: When a Linux Secret Service keyring (GNOME
  Keyring or KDE KWallet with `secret-tool`) is active, `cookies.txt` and
  `accounts.json` are encrypted with AES-256-GCM using a key stored in your
  keyring (`ARENA-SECRET-v1:`). In headless or keyring-less environments they
  fall back to owner-only (`0600`) files under `~/.arena-personal/`.
- **Browser cookie import**: Linux Chromium-family profiles (Chrome, Chromium,
  Brave, Edge, Vivaldi, plus Flatpak Chrome/Brave) are detected and decrypted
  using both the desktop keyring Safe Storage password (`v11`, via `secret-tool`)
  and the fallback key (`v10`), including Chromium 130+ domain-hash prefixes.
- **Agent Gateway (`cloudflared`)**: Settings → Components supports one-click,
  SHA-256-verified download of the official Linux `cloudflared` binary
  (`x86_64` and `arm64`) into `~/.arena-personal/bin`, or uses a system install.
- **Desktop notifications & clipboard**: Uses `notify-send` natively (even when
  the system tray is off) and Wayland/X11 clipboard tools (`wl-paste`, `xclip`,
  `xsel`) for CLI cookie capture.
- **Folder opening**: the Workspace “open folder” action uses `xdg-open`.
- **Icons in dialogs**: the bundle does not ship GTK icon themes (the build
  host's themes contain symlinks PyInstaller cannot preserve, which made GTK
  abort when a file dialog requested a missing icon). File and folder dialogs
  use the icon theme installed on your desktop instead.

## Troubleshooting

- **No window / “could not start a browser window”**: install WebKitGTK 4.1
  (table above). The built binary can also print the same message to stderr
  when started from a terminal.
- **Blank arena.ai window on a corporate network**: the embedded browser is a
  real browser; proxy/CA settings must be available to WebKitGTK. Try the
  system browser first to see whether arena.ai itself is reachable.
- **Debug output**: launch with `ARENA_DEBUG=1` to enable the WebKit inspector
  and more logging; inspect `~/.arena-personal/desktop.log`.
- **Portable data**: put a `portable.txt` next to the binary (or the repo root
  when running from source) to keep all data in `./data` beside it.
