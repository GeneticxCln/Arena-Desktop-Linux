## Arena Desktop v7.1.0 (Native Linux)

Adds live cross-tab project memory and completes a full Linux runtime hardening pass.

### Highlights in 7.1.0

- **Live cross-tab project memory**:
  - Chats now record their canonical project `workspace` path and automatically share compact live summaries (goal, latest ask, edited files, latest outcome — capped at 5 tabs / 2,500 chars) across sibling tabs in the same project folder, account, and track.
  - Whenever Tab A finishes a turn, already-open Tab B detects the changed `memory_hash` and picks up the updated context on its next message (with zero duplicate injection when unchanged).
  - Added per-track **Share recent tabs in this project** toggle, a read-only **Live cross-tab context** preview, and a one-click **Snapshot tabs to MEMORY.md** button (`POST /api/memory/sync-tabs`) in **Settings → Memory**.
  - Fixed `_memory_block()` to read track-scoped `_global_memory_file()` (`memory.md` vs `memory-google.md`), wired memory injection into Google Supervisor runs, and included `memory_global_google` in settings export/import.
- **Linux runtime hardening**:
  - **Clean lifecycle shutdown**: Added idempotent `shutdown_runtime()` and `_shutdown_and_exit()` wired to `SIGINT`/`SIGTERM`, tray **Quit**, window close, and process exit — cleanly cancelling active chats/sub-agents, stopping Agent Gateway tunnels, shutting down MCP servers, closing preview servers, and removing `instance.json`/PID files.
  - **Native GTK main-thread marshaling**: Replaced legacy WinForms `BeginInvoke`/`CoreWebView2` wrappers in `arena/diag.py` with `GLib.idle_add` so off-main-thread `show`/`hide`/`resize`/`move`/`restore` calls execute safely on GTK instead of silently no-oping, and wrapped `engine_win` identically.
  - **WebKitGTK App Cache tracking**: Added `~/.arena-personal/webview` (`"webview"`) to `BROWSER_PROFILES` and live in-use cache protection.
  - **Security & hygiene**: Removed legacy un-permissioned `/api/terminal` and `/api/files/*` routes, moved automation scheduler startup out of module import time, and bounded `_SUBAGENT_CONTEXTS` and `_NATIVE_OUT_OVERRIDE` caches.

### Verification

- Static checks (`bash -n`, `node --check`, `python -m compileall`, static UI hygiene) pass.
- Non-browser test suite: **461 passed** on Linux with fully isolated test storage.

---

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
