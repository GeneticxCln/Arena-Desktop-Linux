# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Arena Desktop (native Linux).

Native window via pywebview + WebKitGTK 4.1, bundled as one ELF binary.
PyGObject/GTK are collected from the build host; WebKit2GTK stays a
system runtime package (see README_LINUX.md).
"""

import os
import platform

_HOST_ARCH = (os.environ.get("ARENA_TARGET_ARCH") or platform.machine() or "").lower()
_IS_ARM64 = _HOST_ARCH in ("arm64", "aarch64")
_EXE_NAME = "Arena-Desktop-arm64" if _IS_ARM64 else "Arena-Desktop"

hiddenimports = [
    'arena',
    'arena.cli',
    'arena.api',
    'arena.auth',
    'arena.config',
    'arena.console',
    'arena.menu',
    'arena.models',
    'arena.desktop',
    'arena.google_ai',
    'arena.google_http',
    'arena.deep_links',
    'arena.web',
    'arena.web.app',
    'arena.contracts',
    'arena.crypto',
    'arena.connectors',
    'arena.history_sync',
    'arena.native_agent',
    'arena.native_subagents',
    'arena.permissions',
    'arena.agent',
    'arena.workspace',
    'arena.engine',
    'arena.diag',
    'arena.appcache',
    'arena.components',
    'arena.gateway',
    'arena.terminal',
    'arena.providers',
    'arena.providers.base',
    'arena.providers.web_engine',
    'arena.providers.deepseek',
    'arena.providers.claude',
    'arena.providers.chatgpt',
    'arena.providers.qwen',
    'arena.providers.kimi',
    'arena.providers.zai',
    'arena.providers.minimax',
    'arena.providers.antigravity',
    'arena.tools',
    'arena.tools.base',
    'arena.tools.file_tools',
    'arena.tools.shell_tools',
    'arena.tools.browser_mcp',
    'arena.tools.mcp',
    'arena.ui',
    'arena.ui.banner',
    'arena.ui.chat_box',
    'arena.ui.markdown',
    'arena.ui.theme',
    'rich',
    'rich.panel',
    'rich.table',
    'rich.text',
    'rich.markdown',
    'rich.syntax',
    'rich.console',
    'requests',
    'websocket',
    'flask',
    'flask.json',
    'flask.templating',
    'jinja2',
    'markupsafe',
    'werkzeug',
    'webview',
    'webview.platforms.gtk',
    'gi',
    'gi.repository.Gtk',
    'gi.repository.Gdk',
    'gi.repository.GdkPixbuf',
    'gi.repository.GLib',
    'gi.repository.Gio',
    'gi.repository.GObject',
    'gi.repository.JavaScriptCore',
    'gi.repository.Soup',
    'gi.repository.WebKit2',
    'pystray',
    'pystray._appindicator',
    'pystray._gtk',
    'pystray._xorg',
    'PIL',
    'PIL.Image',
    'cryptography.hazmat.primitives.ciphers',
    'cryptography.hazmat.primitives.ciphers.aead',
    'cryptography.hazmat.primitives.ciphers.algorithms',
    'cryptography.hazmat.primitives.ciphers.modes',
    'cryptography.hazmat.primitives.hashes',
    'cryptography.hazmat.primitives.kdf.pbkdf2',
]

# Linux relies on the system WebKit2GTK runtime (its web/network helper
# processes cannot be relocated into a one-file bundle). PyInstaller bundles
# PyGObject and GTK; see README_LINUX.md for the runtime packages.

a = Analysis(
    ['arena-cli.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('arena/web/static', 'arena/web/static'),
        ('assets/logo.svg', 'assets'),
        ('assets/logo.png', 'assets'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    # PyInstaller's GTK hook copies the build host's whole icon theme tree
    # (~200k files). Symlinked icons are not preserved, so a partially copied
    # theme makes GTK abort when a dialog requests a missing icon (e.g. the
    # workspace folder chooser). Ship no icon themes and let GTK use the
    # desktop's own icons through the normal XDG search path instead.
    hooksconfig={'gi': {'icons': []}},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy',
              'PySide6', 'PyQt5', 'PyQt6', 'qtpy'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=_EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
