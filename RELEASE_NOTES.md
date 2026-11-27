## Arena Desktop v7.4.1 (Native Linux)

Comprehensive security, reliability, streaming, and UI bug-fix release covering all Critical, Medium, and Low findings from the full codebase audit.

### Highlights in 7.4.1

- **Security & Permission Hardening**:
  - Added per-launch desktop `?boot=<nonce>` cookie gate on `GET /` so unauthenticated local processes cannot scrape `<meta name="api-token">`.
  - Closed Auto-mode safe-command bypasses (newline/NUL injection, `git diff --output`/`--ext-diff`/`--textconv`, `git branch -[dDmMcCf]`, and sensitive path arguments), expanded catastrophic `rm` detection, scoped `PermissionBroker._always` per chat (excluding Agent Gateway), and defaulted Agent Gateway to off (`gateway_enabled is True`).
  - Protected `.git/config`, `.git/hooks/*`, and `.arena-artifacts/checkpoints/*` from agent writes, prevented symlink deletion from `rmtree`-ing external targets, validated checkpoint restore paths and blob names, hardened `_git_run` flags and symlink staging, locked/encrypted Google cookies at rest, stripped MCP `env` secrets on settings export, scoped local preview server routes by unguessable project tokens with directory listing disabled, and tightened `/api/media/save` SSRF redirect/IPv6/CGNAT checks.
  - Hardened CLI tools with workspace boundary enforcement, gated `browser_exec_js` behind explicit opt-in, stopped auto-loading Brave MCP from untrusted `cwd` (passing `BRAVE_API_KEY` via `env`), stripped fenced code blocks from XML tool parsers, and escaped Rich markup in tool output.
- **Engine, Providers & Backend Streaming**:
  - Aborted in-flight streams cleanly on page navigation/stop and tagged `_drive_queue` probes with `probeId`.
  - Added non-destructive cursor-based stream draining, inactivity deadline resets, `text_replace` forwarding, observed-SSE vs DOM deduplication, OAuth popup host titles, and WAF cookie filtering in `WebChatEngine`.
  - Fixed Claude model slug resolution and mid-stream/empty error handling, DeepSeek `response/fragments` routing, Kimi incremental Connect JSON parsing, ChatGPT duplicate stub removal, explicit `bool` returns from `_apply_model_selection`, and Antigravity SDK `permission_mode`/cancellation + CLI timeouts/non-zero exit reporting.
  - Fixed chat refresh race conditions, full-catalog sub-agent/fan-out model resolution beyond the first 14 models, duplicate preservation and ISO timestamp sorting in `history_sync`, empty-catalog cache clobbering, `RateLimit-Reset` parsing, lock-free MCP startup with failure backoff, Battle/Side-by-Side per-lane caches, strict UTF-8 SSE line splitting, and `shutdown_runtime()` shell cleanup.
- **Frontend Fixes**:
  - Defined missing frontend helpers (`esc`, `isUuidText`, `closeModal`), fixed `saveDraft()` and `showToast()` call sites, bound `sendMessage`, `submitVote`, and `addArtifactRecord` to their originating chat, preserved Arena mode across track switches, bounded Google sign-in polling, prevented relative markdown links from navigating away from the SPA, fixed single-lane Battle error handling, fixed emoji-safe retry trimming, added sequence guards to `loadChats`, and removed dead `#bootGate` code.

### Verification

- Static checks (`ruff check --select F821,F811,F823,B006,B023`, `node --check`, `bash -n`, `python -m compileall`, Cyrillic UI hygiene, `qwen.py` untouched) pass.
- Full non-browser test suite: **507 passed** (including 26 new audit regression tests).

---

## Arena Desktop v7.3.0 (Native Linux)

Adds pluggable **Multi-Provider Tabs** (**DeepSeek**, **Claude**, **ChatGPT**, and **Google Antigravity**), live per-provider AI model retrieval & selection, and shared-workspace read/write tools with full `PermissionBroker` controls across every provider.

### Highlights in 7.3.0

- **Multi-Provider Tabs (`arena/providers/`)**:
  - **DeepSeek (`◇`)**, **Claude (`◈`)**, and **ChatGPT (`◎`)** run inside persistent, signed-in Linux WebKitGTK windows (zero CLI tools, zero API keys, manual sign-in and manual human verification only).
  - **Google Antigravity (`▲`)** connects to your local `agy` CLI or `google-antigravity` Python SDK inside the active workspace.
  - Added **Settings → Providers** panel (`GET /api/providers`, `/api/providers/<id>/sign-in`, `/open`, `/verify`, `/models`) and expanded **`+ New tab ▾`** with all six tab providers.
- **Silent Background Streaming & Live Model Discovery**:
  - Native WebKit2GTK `evaluate_javascript` calls with exact UTF-8 byte lengths (fixing both strict-CSP `EvalError` on ChatGPT/Claude and `pywebview`'s UTF-8 script truncation bug).
  - **Claude** streams directly through `claude.ai`'s signed-in same-origin SSE API (`/api/organizations` → `/chat_conversations` → `/completion`) silently in the background, and retrieves real account models from `/api/bootstrap`, `/api/account`, and the live selector.
  - **ChatGPT** retrieves real account models from `/backend-api/models` and captures live SSE deltas via an observation-only `fetch()` clone hook + `/backend-api/conversation/<id>` hidden-window fallback.
  - **DeepSeek** discovers live `chat.deepseek.com` modes (Default, DeepThink, Search), uses composer-scoped submission, and captures live SSE reasoning/answer streams + `/api/v0/chat/history_messages` fallback.
  - **Antigravity** retrieves the full live local model catalog from `agy models` and forwards `--model` and permission-mode flags (`--mode plan`, `--mode accept-edits`, `--mode auto`, `--dangerously-skip-permissions`).
  - Added an interactive **per-provider model selector chip** in the bottom composer with search, org icons, live indicator, and on-demand refresh.
- **Shared Workspace Read/Write & Permissions Across All Providers**:
  - All providers inside a workspace folder share `MEMORY.md`, `AGENTS.md`, and live cross-tab sibling summaries (`[DeepSeek]`, `[Claude]`, `[ChatGPT]`, `[Antigravity]`, `[Arena]`, `[Google]`).
  - DeepSeek, Claude, and ChatGPT tabs inside a workspace folder now run Arena Desktop's multi-round `agent.LocalTools` loop (`ls`, `read_file`, `read_files`, `search`, `write_file`, `edit_file`, `mkdir`, `delete`, `run_cmd`, `set_todos`, plus local MCP connectors) governed by **`PermissionBroker`** (`Plan`, `Ask`, `Accept edits`, `Auto`, `Bypass`) with live tool cards, diffs, and checkpoints.

### Verification

- Static checks (`bash -n`, `node --check`, `python -m compileall`, static UI hygiene) pass.
- Non-browser test suite: **474 passed** on Linux with isolated test storage.

---

## Arena Desktop v7.2.0 (Native Linux)

Introduces the new Workspace-First UI (left Workspaces sidebar + IDE-style top tab bar with per-tab Arena/Google mode selection) and fixes Linux WebKitGTK Google AI mode sign-in & cookie export end-to-end.

### Highlights in 7.2.0

- **Workspace-First UI & Per-Tab Mode Selection**:
  - **Left Workspaces sidebar**: Manage multiple open project folders alongside the built-in **General** workspace (`GET/POST /api/workspaces`, `/api/workspaces/switch`, `/api/workspaces/<id>/remove`), with live tab counts and native folder picker support.
  - **IDE/Browser-style top tab bar**: One workspace holds multiple open tabs side-by-side — each tab carries its own **Arena (`✦`)** or **Google AI (`G`)** mode badge, close button, and live streaming indicator.
  - **Per-tab mode picker**: Clicking **`+ New tab ▾`** lets you choose **Arena mode** or **Google AI mode** for each new tab; switching tabs automatically adapts the composer without hiding sibling tabs.
  - **Shared cross-tab memory across modes**: Arena and Google tabs inside the same workspace share `MEMORY.md`, `AGENTS.md`, and live `[Arena]` / `[Google]` sibling-tab summaries.
  - **UI polish**: Removed the legacy top-bar chat bubble & global mode switcher in favor of a dedicated **History** button in the Workspaces sidebar footer, and kept the chat composer pinned cleanly at the bottom at a consistent width across both welcome and active chat views.
- **Linux WebKitGTK Google AI Mode Fixes**:
  - Fixed `GoogleEngine.show()` and `open_sign_in()` on Linux GTK (where `pywebview` sets `events.shown` during hidden startup), ensuring the Google sign-in window reliably pops up and raises to the front.
  - Fixed Linux WebKitGTK cookie export (`http.cookies.Morsel` value extraction + direct `WebKit2.CookieManager.get_cookies()` queries across `www.google.com` and `accounts.google.com`, plus automatic cookie export on `/api/google/status` and `/api/google/verify`).
  - Fixed `ReferenceError: Can't find variable: googleDone` in Google-mode account rendering (which previously caused false *"Local server is unavailable"* banners), kept the top-right Google profile pill visible, pinned `gi.require_version("Gtk", "3.0")` in `arena/diag.py`, and eliminated Windows `System.Windows.Forms` log spam on Linux.

### Verification

- Static checks (`bash -n`, `node --check`, `python -m compileall`, static UI hygiene) pass.
- Non-browser test suite: **465 passed** on Linux with fully isolated test storage.

---

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
