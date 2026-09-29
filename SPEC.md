# herdr-queue — SPEC (v0.1 draft)

A local-only herdr plugin that answers one question: **"which of my agents is waiting on me, and what does it need?"**

## 1. Problem

Running 2–6 parallel coding agents (Claude Code on Bedrock) in herdr. Pain points:

1. Missing the initial "needs you" ping leaves no reliable way to tell which pane is waiting.
2. Herdr's status for Claude Code is **screen-scraped** (manifest detection), not hook-authored. Claude's herdr integration only reports session identity. Result: a pane can look "ready" while still working, or "done" when it's blocked.
3. Reading the full pane output of each agent to work out what's needed is a large context cost for the human.

## 2. Goals

- G1. **Trustworthy status** for Claude Code panes via Claude Code lifecycle hooks -> `herdr pane report-agent`.
- G2. **A single ranked queue** (overlay pane in herdr) of agents needing attention, with jump-to-pane.
- G3. **One-line "what does it need"** summary per waiting agent (heuristic in v1, optional LLM in v2).
- G4. **Seen/unseen tracking** so finished-but-unreviewed agents stay in the queue until visited.

## 3. Non-goals / hard constraints

- **No network listener. Ever.** No HTTP server, no phone/PWA, no tailscale. Unix socket / CLI only. (Must be defensible on a managed work laptop.)
- **Read-only in v1.** Never call `pane send-input` / `send-text` / `send-keys`. Navigation (`pane focus`, `tab focus`, `workspace focus`) is allowed.
- **No new vendor.** Any LLM summarization goes through the same Claude Code / AWS Bedrock path the agents already use (`claude -p`, Haiku-class model). Off by default.
- **Zero third-party runtime deps** where feasible; small enough to audit in one sitting.
- Must work with herdr **0.8.2** (installed on Thor) and degrade gracefully on newer (0.9.x). Set `min_herdr_version` accordingly after verifying.
- Linux + macOS. (Work laptop is not Thor.)

## 4. Language / layout decision

Python 3 stdlib only (`curses`, `json`, `subprocess`, `socket`). Rationale: no build step, trivially auditable, runs on locked-down laptops. Claude Code may propose Rust/ratatui instead **only if** it justifies the added build/deploy cost; default is Python.

```
herdr-queue/
  herdr-plugin.toml        # manifest: overlay pane + action + keybinding + startup
  hooks/
    claude-hook.sh         # called by Claude Code hooks; maps event -> report-agent
    install-hooks.sh       # idempotently merges hook entries into ~/.claude/settings.json (with backup, --dry-run)
    uninstall-hooks.sh
  queue/
    daemon.py              # event subscriber, keeps state file
    tui.py                 # curses overlay pane, reads state, renders queue
    summarize.py           # heuristic (v1) / optional LLM (v2)
    herdr.py               # thin wrapper over $HERDR_BIN_PATH CLI / socket (JSON)
  tests/
  README.md
  SPEC.md
```

## 5. Component A — Status hooks (G1)

Claude Code hooks (verify exact event names/payloads against current Claude Code docs; do not trust this list):

| Claude hook event | herdr state | notes |
|---|---|---|
| `UserPromptSubmit` | `working` | |
| `PreToolUse` (optional) | `working` | keeps state fresh mid-turn |
| `Notification` | `blocked` | permission prompt or idle-waiting-for-input; payload message becomes `--message` |
| `Stop` / `SubagentStop`* | `idle` | *confirm whether subagent stop should count |
| `SessionEnd` | release agent | `pane release-agent` |

Implementation: `claude-hook.sh` reads hook JSON from stdin, and calls:

```
"$HERDR_BIN_PATH" pane report-agent "$HERDR_PANE_ID" \
  --source custom:herdr-queue --agent claude --state <state> --message "<text>" --seq <monotonic>
```

Requirements:
- No-op silently (exit 0) if `HERDR_ENV`/`HERDR_PANE_ID` are unset (Claude running outside herdr must not break).
- Never block or slow Claude: hook must return fast; hard timeout on the herdr call.
- Never emit output that Claude Code would interpret as a hook decision.
- `--seq` monotonic per pane so out-of-order hooks can't regress state.
- **Open question OQ1:** how does a `custom:` source coexist with herdr's screen-manifest detection for the same pane? Herdr docs say lifecycle-hook integrations become authoritative "when installed and actively reporting". Determine empirically whether `report-agent` with a custom source overrides the manifest for Claude, and document the result. If it does not override, find the supported mechanism (`pane clear-agent-authority`?) or report to upstream.
- `install-hooks.sh` must back up `settings.json`, be idempotent, support `--dry-run`, and never clobber existing hooks.

## 6. Component B — Queue daemon (G2, G4)

- Bootstrap with `session.snapshot`, then `events.subscribe` (`pane.agent_status_changed`, `pane.agent_detected`, lifecycle events), re-snapshot on reconnect. (Pattern per herdr socket docs.) Resubscribe when the agent-pane set changes (status event subscriptions are per-`pane_id`).
- Events are pokes; **snapshot is authoritative**. A missed event must cost at most one poll interval.
- Persist state to `$XDG_STATE_HOME/herdr-queue/state.json` (per herdr session): per pane -> `{status, since, seen, summary, label}`.
- Must respect multiple herdr sessions (`--session`); default = the session Herdr injects via `HERDR_SOCKET_PATH`.
- **Seen model:** a pane is "seen" when the user focuses it (focus event / snapshot `focused` state) *after* it entered blocked/done-idle. Any transition back to `working` resets it. Herdr's API `idle` may correspond to terminal-client `done` (Collie's `HERDR_API.md` documents this); the daemon must treat "settled + unseen" uniformly.

## 7. Component C — Queue overlay TUI (G2)

Declared as `[[panes]] placement = "overlay"` with a keybinding. Layout, one row per agent pane, ranked:

1. `BLOCKED` — needs an answer (oldest first)
2. `DONE-UNSEEN` — finished, not yet reviewed (oldest first)
3. `WORKING` — in progress, with elapsed time
4. `IDLE-SEEN` — collapsed by default

Row: `[state] workspace/tab  agent  age  — one-line summary`.

Keys: `j/k` or arrows move; `Enter` jump to pane (focus workspace, tab, pane) and close overlay; `s` mark seen; `S` mark all seen; `a` toggle showing idle-seen; `r` refresh summary; `q`/`Esc` close. Always show a footer with counts (`2 blocked · 1 done · 3 working`).

Optional: `workspace.report_metadata` / `pane.report_metadata` to surface the queue count in herdr's own chrome — investigate, don't assume.

## 8. Component D — Summaries (G3)

- **v1 (no LLM):** from `pane read --source recent-unwrapped --lines N`, plus the hook `--message`. Heuristics: for BLOCKED prefer the Notification message; else last question-like line (`?`), else last non-empty agent line, truncated to one line (~100 cols). For DONE prefer the last assistant summary-ish line.
- **v2 (opt-in, off by default):** on transition into blocked/done only (never a timer), run `claude -p --model <haiku-class>` with a fixed prompt: *"In <=15 words: what does this agent need from the human, or what did it finish? Pane tail follows."* Config flag `summaries.llm = false`. Cache per `(pane, transition_id)`. Redact obvious secrets (env-var-looking, `AKIA…`, bearer tokens) from the tail before sending. Document that pane content leaves the machine only via the already-approved Claude/Bedrock path.
- Cost/latency guard: max N calls/min, timeout, silent fallback to v1 heuristic on any failure.

## 9. Configuration

`$XDG_CONFIG_HOME/herdr-queue/config.toml`: summaries.llm, summaries.model, tail_lines, rank order, redaction patterns, poll interval, session name override.

## 10. Testing / acceptance

- Unit tests for state machine (seq ordering, seen/unseen transitions), ranking, heuristic summarizer, redactor.
- Fake-herdr fixture: a stub CLI/socket emitting recorded snapshot + event sequences; TUI rendering testable headlessly (render to a string buffer).
- **Live test on Thor:** use an isolated herdr session (`--session herdr-queue-dev`), never `default` or `alice-agents`. Start 3 Claude panes; verify a permission prompt -> BLOCKED within 1s; finish -> DONE-UNSEEN; focus -> seen; a still-working pane never shows as waiting.
- Acceptance criteria:
  - AC1: No listening sockets opened by any component (verify with `ss -ltnp`).
  - AC2: No call to any send-input/send-text/send-keys method anywhere in the code (grep-enforced test).
  - AC3: Claude hook adds <50 ms overhead, exits 0 outside herdr.
  - AC4: With LLM summaries disabled, no network calls made by the plugin.
  - AC5: Killing the daemon/TUI never affects agents or herdr.

## 11. Milestones

- M0: Read herdr docs (`herdr --skill`, `herdr api schema --json`, plugin docs at herdr.dev/docs/plugins), resolve OQ1 empirically, write findings to `docs/findings.md`.
- M1: Hooks + installer; verified accurate blocked/working/idle on real Claude panes.
- M2: Daemon + state file + seen model.
- M3: Overlay TUI with ranking + jump.
- M4: Heuristic summaries.
- M5 (optional): LLM summaries via `claude -p`.

## 12. Open questions

- OQ1: custom-source authority vs. screen manifest for Claude panes (see §5).
- OQ2: is overlay-pane focus/keyboard capture reliable in 0.8.2, or does the plugin need a newer herdr?
- OQ3: does `pane focus` mark a pane as "seen" in herdr's own client state, and can we read that from the API instead of tracking ourselves?
- OQ4: exact Claude Code hook payload fields and whether `Notification` distinguishes permission vs. idle-waiting.
- OQ5: Bedrock invocation shape for `claude -p` on the work laptop (profile/region env) — config, not code.
