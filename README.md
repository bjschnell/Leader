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

Every waiting row gets a one-line reason. Hooked Claude panes use the hook's text: the permission or question for BLOCKED, and the most summary-like line of the final reply for DONE. Other agents fall back to heuristics over the pane tail. `r` in the overlay re-summarizes the selected row from its tail. Optional AI summaries are described below.

## AI summaries (M5, off by default)

Set in `~/.config/leader/config.toml`:

```toml
[summaries]
llm = true
model = "haiku"            # anything `claude --model` accepts
# env = { AWS_PROFILE = "work", AWS_REGION = "us-west-2" }   # Bedrock, if the daemon's environment lacks it
# extra_args = ["--bare"]  # only with ANTHROPIC_API_KEY auth; --bare disables OAuth/keychain login
max_calls_per_min = 6
timeout = 20
# redaction_patterns = ["corp-[0-9]+"]   # extra regexes to scrub before sending
```

How it behaves:
- **When:** one call per transition into DONE (or into BLOCKED when no exact hook message exists). Never on a timer. The result is kept in state.json, so a restart doesn't pay again.
- **Path:** runs your own `claude -p` with no tools, no MCP servers, no slash commands and no session persistence. Pane content leaves the machine only through the Claude Code / Bedrock path your agents already use. Leader opens no other network connection, and with `llm = false` it runs no subprocess at all.
- **What is sent:** the last turn of the pane (the final user prompt onward, UI chrome stripped), after redacting keys, tokens, passwords, bearer headers, JWTs, private keys and URL credentials.
- **Failure:** timeouts, errors and rate-limit hits keep the heuristic line.



## Development

```sh
python3 -m unittest discover -s tests
scripts/dev-server         # isolated herdr universe (own config, plugin registry, sockets)
scripts/dev-herdr <args>   # herdr CLI pinned to that dev session
scripts/dev-leader daemon|tui [args]   # Leader tools pinned to that dev session
```
