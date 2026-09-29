# herdr-queue (Leader)

A local-only [herdr](https://herdr.dev) plugin that answers: **which of my agents is waiting on me, and what does it need?**
See [SPEC.md](SPEC.md) for goals and constraints and [docs/findings.md](docs/findings.md) for verified herdr / Claude Code behaviour.

No network listeners. Never sends input to panes. Python 3 stdlib only.

## Status hooks (M1)

`hooks/claude-hook.sh` turns Claude Code lifecycle hooks into authoritative herdr pane state (`working` / `blocked` / `idle`). It also publishes a one-line "what it needs" as the pane token `hq_msg`, and the last reply line as `hq_last`. Outside herdr it does nothing.

```sh
hooks/install-hooks.sh --dry-run        # show the merged ~/.claude/settings.json
hooks/install-hooks.sh                  # install (backs up settings.json first; idempotent)
hooks/uninstall-hooks.sh                # remove only our entries
hooks/install-hooks.sh --settings PATH  # target another settings file (e.g. for `claude --settings PATH`)
```

Known limitation: denying a permission prompt with Esc, or interrupting a turn with Esc, fires no Claude Code hook. The pane keeps its last state (`blocked` / `working`) until the next prompt. See docs/findings.md §11.

## Development

```sh
python3 -m unittest discover -s tests
scripts/dev-herdr <args>   # herdr CLI pinned to the isolated `herdr-queue-dev` session
```
