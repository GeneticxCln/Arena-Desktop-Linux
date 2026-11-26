#!/usr/bin/env python3
"""
Arena Desktop — Native Linux desktop & CLI client for arena.ai.
"""

import sys
from pathlib import Path

# Ensure package root is in sys.path
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))


def main():
    if "--cli" in sys.argv:
        from arena.cli import main as cli_main
        cli_main()
    else:
        from arena.desktop import main as desktop_main
        desktop_main()


if __name__ == "__main__":
    main()
