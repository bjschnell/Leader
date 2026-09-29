"""Merge / remove leader hook entries in a Claude Code settings.json.

Our entries are recognised by their command string (the absolute path of
claude-hook.sh). Nothing else in the file is touched. Every write is preceded
by a timestamped backup and done atomically.
"""

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
import time

HOOK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude-hook.sh")
HOOK_TIMEOUT_S = 5

# event -> matcher (None = all). See docs/findings.md section 8.
EVENTS = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": "AskUserQuestion",
    "PermissionRequest": None,
    "PostToolUse": None,
    "PostToolUseFailure": None,
    "Notification": None,
    "Stop": None,
    "StopFailure": None,
    "SessionEnd": None,
}


def default_settings_path():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(base, "settings.json")


def quote_command(path):
    return "'" + path.replace("'", "'\"'\"'") + "'"


def our_entry(matcher, command):
    entry = {"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}]}
    if matcher:
        entry["matcher"] = matcher
    return entry


def is_ours(hook, command):
    return isinstance(hook, dict) and hook.get("command") == command


def remove_ours(settings, command):
    """Return a copy of settings with our hook commands removed; drop emptied groups/events."""
    out = copy.deepcopy(settings)
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                kept = [h for h in group["hooks"] if not is_ours(h, command)]
                if not kept and group["hooks"]:
                    continue  # the group held only our hooks
                group = dict(group, hooks=kept)
            new_groups.append(group)
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    if not hooks:
        del out["hooks"]
    return out


def add_ours(settings, command):
    out = remove_ours(settings, command)
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.json 'hooks' is not an object; refusing to modify")
    for event, matcher in EVENTS.items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"settings.json hooks.{event} is not an array; refusing to modify")
        groups.append(our_entry(matcher, command))
    return out


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def write(path, data):
    path = os.path.realpath(path)  # keep a dotfiles-managed symlink pointing at the real file
    if os.path.exists(path):
        backup = f"{path}.bak.leader.{time.strftime('%Y%m%d-%H%M%S')}.{time.time_ns() % 1_000_000_000:09d}"
        shutil.copy2(path, backup)
        print(f"backup: {backup}")
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install or remove leader Claude Code hooks.")
    ap.add_argument("action", choices=["install", "uninstall"])
    ap.add_argument("--settings", default=default_settings_path(), help="settings.json to edit")
    ap.add_argument("--dry-run", action="store_true", help="print the resulting JSON, write nothing")
    args = ap.parse_args(argv)

    command = quote_command(HOOK_SCRIPT)
    try:
        before = load(args.settings)
        after = add_ours(before, command) if args.action == "install" else remove_ours(before, command)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if after == before:
        print(f"{args.settings}: already {'installed' if args.action == 'install' else 'absent'}; nothing to do")
        return 0
    if args.dry_run:
        print(json.dumps(after, indent=2))
        return 0
    write(args.settings, after)
    print(f"{args.settings}: {args.action}ed leader hooks ({HOOK_SCRIPT})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
