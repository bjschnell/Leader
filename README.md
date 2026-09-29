# Leader

A local-only [herdr](https://herdr.dev) plugin that answers: **which of my agents is waiting on me, and what does it need?**
See [SPEC.md](SPEC.md) for goals and constraints and [docs/findings.md](docs/findings.md) for verified herdr / Claude Code behaviour.

No network listeners. Never sends input to panes. Python 3 stdlib only.

## Status hooks (M1)

`hooks/claude-hook.sh` turns Claude Code lifecycle hooks into authoritative herdr pane state (`working` / `blocked` / `idle`). It also publishes a one-line "what it needs" as the pane token `leader_msg`, and the last reply line as `leader_last`. Outside herdr it does nothing.

```sh
hooks/install-hooks.sh --dry-run        # show the merged ~/.claude/settings.json
hooks/install-hooks.sh                  # install (backs up settings.json first; idempotent)
hooks/uninstall-hooks.sh                # remove only our entries
hooks/install-hooks.sh --settings PATH  # target another settings file (e.g. for `claude --settings PATH`)
```

Known limitation: denying a permission prompt with Esc, or interrupting a turn with Esc, fires no Claude Code hook. The pane keeps its last state (`blocked` / `working`) until the next prompt. See docs/findings.md §11.

## Queue overlay (M2–M3)

```sh
herdr plugin link /path/to/leader     # registers the plugin (global to this herdr config dir)
```

Add a keybinding to herdr's `config.toml`, then `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+a"
type = "plugin_action"
command = "bjschnell.leader.open"
description = "agent queue"
```

`prefix+a` opens the overlay: `j`/`k` move, Enter jumps to the pane, `s`/`S` mark done rows seen, `a` toggles idle rows, `q`/Esc close.
The daemon (`queue/daemon.py`, also started by the plugin's `[[startup]]` hook) keeps `$XDG_STATE_HOME/leader/<session>/state.json` current. If it isn't running, the overlay refreshes by itself while it's open.
`python3 queue/tui.py --print --all` renders the queue once to stdout.

## Summaries (M4)

Every waiting row gets a one-line reason. Hooked Claude panes use the hook's text: the permission or question for BLOCKED, and the most summary-like line of the final reply for DONE. Other agents fall back to heuristics over the pane tail. `r` in the overlay re-summarizes the selected row from its tail. There are no LLM calls (the M5 option is off and not implemented).

## Development

```sh
python3 -m unittest discover -s tests
scripts/dev-server         # isolated herdr universe (own config, plugin registry, sockets)
scripts/dev-herdr <args>   # herdr CLI pinned to that dev session
```
