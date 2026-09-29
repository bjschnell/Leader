#!/bin/sh
# Idempotently add leader hooks to Claude Code settings.json (backs up first).
# Usage: install-hooks.sh [--dry-run] [--settings PATH]
exec python3 "$(dirname "$0")/install_hooks.py" install "$@"
