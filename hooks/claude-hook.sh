#!/bin/sh
# herdr-queue: Claude Code hook -> herdr pane lifecycle state.
# Silent, always exits 0, no-op outside herdr. Installed by install-hooks.sh.
[ "${HERDR_ENV:-}" = "1" ] || exit 0
[ -n "${HERDR_PANE_ID:-}" ] || exit 0
[ -n "${HERDR_SOCKET_PATH:-}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
python3 "$(dirname "$0")/claude_hook.py" >/dev/null 2>&1
exit 0
