#!/bin/sh
# Remove leader hooks from Claude Code settings.json (backs up first).
# Usage: uninstall-hooks.sh [--dry-run] [--settings PATH]
exec python3 "$(dirname "$0")/install_hooks.py" uninstall "$@"
