## Arena Desktop v7.0.0 (Native Linux)

Major milestone transforming Arena Desktop into a pure, first-class **native Linux** desktop application (WebKitGTK 4.1 + GTK 3).

### Highlights in 7.0.0

- **Pure Linux codebase**: Dropped all Windows-only launchers, CI jobs, Win32/DPAPI/COM/WebView2/winget code paths, and removed the stale 25.5 MB archive from the repository.
- **Linux Secret Service encryption at rest**: Session cookies (`cookies.txt` and `accounts.json`) are encrypted with AES-256-GCM (`ARENA-SECRET-v1:`) using a master key stored in GNOME Keyring / KDE KWallet via `secret-tool`, with opportunistic migration from plaintext and `0600` fallback on headless systems.
- **Chromium `v11` & Chromium 130+ cookie import**: Fixed Linux Chromium AES-128-CBC decryption (16-byte space IV + 32-byte host-hash prefix) and added Secret Service Safe Storage password lookup via `secret-tool`.
- **One-click Linux `cloudflared` installer**: Settings → Components downloads the pinned official Cloudflare Linux static binary (`x86_64` / `arm64`), verifies its SHA-256 checksum, installs to `~/.arena-personal/bin/cloudflared` (`0755`), and detects system `WebKitGTK 4.1` and `python3`.
- **Native desktop notifications & clipboard**: Added `notify-send` notifications (works even when the system tray is off) and Wayland/X11 clipboard detection (`wl-paste`, `xclip`, `xsel`).
- **Bug fixes & CI**:
  - Fixed frozen Linux startup readiness check attempting to import Windows `pythonnet` (`clr`).
  - Fixed single-account sub-agent child chats disappearing from `/api/chats` (and bypassing the browser engine) due to the `"current"` scheduler sentinel leaking as `account_id`.
  - Added push/PR CI workflow (`.github/workflows/ci.yml`), isolated all pytest config paths from `~/.arena-personal`, added SHA-256 verification for `nfpm` in `packaging/build-packages.sh`, and fixed `SKIP_APPIMAGE=1` checksum exit behavior.

### Release Formats

Every Linux release ships five formats with `SHA256SUMS`:
- `.deb` (Debian / Ubuntu)
- `.rpm` (Fedora / openSUSE)
- `.pkg.tar.zst` (Arch / CachyOS / Manjaro)
- `.AppImage` (portable)
- `.tar.gz` (manual archive + `install-linux.sh`)

### Verification

- Static checks (`bash -n`, `node --check`, `python -m compileall`, static UI hygiene) pass.
- Non-browser test suite: **454 passed** on Linux with fully isolated test storage.
- Single-file ELF binary and `.deb` / `.rpm` / `.pkg.tar.zst` / `.tar.gz` packages verified free of host GTK icon themes.

Released under the [MIT License](LICENSE). Not affiliated with Arena AI Inc.
