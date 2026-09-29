# M0 findings

Verified 2026-09-28 against herdr **0.8.2** (protocol 20, `herdr api schema --json` schema_version 1), Claude Code **2.1.284**, Python 3.14.7.
Sources: `herdr --skill`, `herdr api schema --json`, `herdr <group>` help, strings of the installed binary, herdr.dev/docs/{plugins,agents,socket-api}, code.claude.com/docs/en/hooks.
All live experiments ran in an isolated session (`herdr --session herdr-queue-dev server`), with `HERDR_SOCKET_PATH` pinned to its socket and `HERDR_PANE_ID`/`HERDR_TAB_ID`/`HERDR_WORKSPACE_ID` unset.

Status legend: **[live]** observed in the dev session · **[docs]** herdr/Claude docs · **[binary]** installed binary (help, schema, embedded strings).

## 1. Transport

- **[docs][live]** Newline-delimited JSON over the Unix socket at `$HERDR_SOCKET_PATH`. Request `{"id","method","params"}`; response `{"id","result":{"type":...}}` or `{"id","error":{"code","message"}}`.
- **[binary]** The CLI chooses its session from `HERDR_SOCKET_PATH`. When that is unset it falls back to `--session`/default. The daemon can therefore talk to the socket directly with stdlib `socket` and needs no subprocess per call.
- **[binary]** CLI server errors are JSON on stderr with exit 1; syntax errors exit 2.
- **[binary]** Herdr's own Claude integration script (embedded in the binary) uses exactly this pattern: python3, `AF_UNIX`, `settimeout(0.5)`, one request line, read one reply, `seq = time.time_ns()`.

## 2. `pane.report_agent` / authority (OQ1)

Params: `pane_id, source, agent, state (idle|working|blocked|unknown), message?, seq?, agent_session_id?, agent_session_path?`. There is no `done` input state; `done` is derived by herdr.

Observed on panes with no real agent process **[live]**:
- A `custom:herdr-queue` report on a plain shell pane immediately makes `agent=claude` with the reported status. `pane.agent_detected` and `pane.agent_status_changed` events fire.
- **Stale seq is silently dropped** (exit 0, no state change). This is per source, per docs.
- **`--message` is not exposed anywhere**: not in `pane get`, `agent get`, the snapshot, events, or `agent explain`. Herdr presumably uses it only for notifications. **The daemon can't recover the hook message from herdr state.** → Carry it in `pane.report_metadata` tokens instead (see §5).
- **Between two `custom:*` sources, the most recent reporter wins.** `custom:other` overrode `custom:herdr-queue` without a release. `release-agent` from a non-owner source is a no-op. `release-agent` from the owner removes the agent (`agent=null`, `unknown`) and clears `state_labels`; tokens survive.
- `pane.clear_agent_authority` is **API-only** (no CLI subcommand in 0.8.2).

From docs/binary:
- **[docs]** "For agents with complete lifecycle hooks, the integration is authoritative when it is installed and actively reporting… It does not also run screen manifest fallback for that same lifecycle authority." Claude's official integration is **session-only**. For those, "Herdr still uses screen manifest detection" because hooks "can miss permission approval results, escape interrupts, or other transitions."
- **[binary]** The binary contains `full_lifecycle_hook_authority`, `screen_detection_skip_reason`, `sync_full_lifecycle_authority_detection_pauses` and `suppress_current_full_lifecycle_hook_authority`. So screen detection is *paused* while a full-lifecycle authority is active.
- **[binary]** The official `herdr:claude` hook (v8) sends only `report_agent_session`, skips events that carry `agent_id` (subagent context), and explicitly ignores `SubagentStop`: "Claude recap/away-summary can emit it after the main turn has already stopped. Never let it revive an idle pane."

**OQ1 is still OPEN.** What remains to observe: with a real `claude` process in the pane, does a `custom:` report (a) pause screen detection, which risks getting stuck in `working` after an Esc interrupt because Esc fires no hook, or (b) get overwritten by the screen manifest on the next redraw? The shell-pane test can't tell these apart: `agent explain` says the screen evaluates to `idle` (`default_known_agent_idle_fallback`) while the reported `blocked`/`working` stuck, but nothing redrew. **Blocked:** this needs a Claude process running inside a dev-session pane (see §9).

## 3. Status, done vs idle, seen (OQ3)

- **[live]** Herdr already implements "seen". A pane that goes `working → idle` while its **tab is not focused** reports `done`. A pane in the focused tab (even an unfocused split) goes straight to `idle`.
- **[live]** Focusing the tab (`tab focus`, `agent focus`, or API `pane.focus`) turns `done` into `idle`. Reads (`pane get`, `pane read`, `snapshot`) don't. `blocked → idle` while unseen also yields `done`.
- `TabInfo.agent_status` and `WorkspaceInfo.agent_status` are rollups (e.g. tab `done`).
- ⇒ **OQ3 answer: yes.** `done` = settled + unseen, and it's readable from the snapshot. The daemon can use herdr's status as the source of truth for DONE-UNSEEN. It only needs its own seen flag for the TUI's `s`/`S` "mark seen without visiting" keys, and for `blocked` rows, which herdr never marks seen.

## 4. Snapshot / events

- **[binary]** `session.snapshot` → `{version, protocol, workspaces[], tabs[], panes[], layouts[], agents[], focused_workspace_id, focused_tab_id, focused_pane_id}`. `agents[]` has `pane_id, workspace_id, tab_id, agent, agent_status, focused, state_change_seq, name, display_agent, state_labels, tokens, title, agent_session, cwd`. `WorkspaceInfo.label` and `TabInfo.label` give the row's `workspace/tab`. (`herdr api snapshot` in the CLI.)
- **[binary]** Subscription types: `pane.agent_status_changed` (**requires `pane_id`**, optional `agent_status` filter), `pane.output_matched`, `pane.scroll_changed` (per pane), plus global lifecycle types: `pane.created|closed|updated|focused|moved|exited|agent_detected`, `tab.*`, `workspace.*`, `layout.updated`.
- **[live]** Wire names differ: lifecycle events arrive as `{"event":"pane_focused","data":{"type":"pane_focused",...}}` (underscore), status events as `{"event":"pane.agent_status_changed","data":{pane_id,workspace_id,agent,agent_status,...}}`.
- **[live]** The first line is the ack `{"id":..,"result":{"type":"subscription_started"}}`. One connection carries many subscriptions.
- **[live]** Right after subscribing I received some focus/detected events whose origin I couldn't attribute. Docs say there is no replay. Either way, treat events as pokes and re-snapshot (per spec).
- **[docs]** The recommended pattern is: subscribe → buffer → snapshot on a second connection → apply buffer → stream; re-snapshot on reconnect. Because status subscriptions are per pane, resubscribe (new connection) when the agent pane set changes (`pane.agent_detected`, `pane.closed`, `pane.exited`).

## 5. Metadata (G3 carrier, chrome count)

- **[live]** `pane.report_metadata --source custom:… --token k=v --state-label blocked=TEXT --ttl-ms N` works. Tokens and state_labels appear in `pane get`/snapshot. `workspace.report_metadata` tokens appear in `workspace get`.
- **[docs]** Values are normalized (trimmed, control chars removed, **capped at 80 chars**). Max 16 keys per report and 32 per resource, TTL 1 ms–24 h, not persisted across server restart. Workspace tokens can render in the sidebar via `$token_name` placeholders; changes emit `workspace.metadata_updated`. `applies_to_source` scopes presentation to a lifecycle source.
- ⇒ The hook can publish the Notification message as pane token `hq_msg` (TTL-bounded). The daemon reads it from the snapshot, so no side-channel file is needed. A queue count could be a workspace token (`hq_waiting`) that the user opts into rendering in the sidebar.

## 6. Navigation (jump)

- **[live]** API `pane.focus {pane_id}` switches workspace, tab and pane in one call and marks the pane seen. `agent focus <pane_id>` does the same for agent panes.
- **[binary]** CLI `herdr pane focus` in 0.8.2 is **directional only** (`--direction`), so use the socket method or `agent focus`.

## 7. Plugins (0.8.2) — OQ2 partially

- **[docs]** `herdr-plugin.toml` needs `id, name, version, min_herdr_version`; it may also declare `description, platforms, [[build]], [[startup]], [[actions]], [[events]], [[panes]], [[link_handlers]]`.
- **[docs]** `[[panes]] placement = "overlay"` (the default) is a "temporary zoomed overlay; restores previous focus/zoom on close". `popup` "receives all terminal input including Escape" and has no `HERDR_PANE_ID`.
- **[binary]** 0.8.2 has `herdr plugin link|list|pane open|action invoke|log list`. The binary validates `min_herdr_version` and has `plugin_requires_newer_herdr`.
- **[docs]** Plugin processes get `HERDR_SOCKET_PATH, HERDR_BIN_PATH, HERDR_ENV, HERDR_PLUGIN_ID, HERDR_PLUGIN_ROOT, HERDR_PLUGIN_CONFIG_DIR, HERDR_PLUGIN_STATE_DIR, HERDR_PLUGIN_CONTEXT_JSON`, and panes also get `HERDR_PLUGIN_ENTRYPOINT_ID`. **Use `HERDR_PLUGIN_STATE_DIR` for state when running as a plugin**, falling back to `$XDG_STATE_HOME/herdr-queue/<session>` otherwise.
- **[docs]** Keybinding: user config `[[keys.command]] key=… type="plugin_action" command="<plugin>.<action>"`. The 0.8.2 default config only documents `shell|pane|popup`, so `plugin_action` needs a live check. Fallback: `type="popup"` / `"pane"` running `herdr plugin pane open …` or the TUI directly.
- Overlay keyboard capture (OQ2) needs a live check with an attached client.

## 8. Claude Code hooks (OQ4)

- **[docs]** Common input: `session_id, transcript_path, cwd, hook_event_name, permission_mode?, agent_id?/agent_type?` (the last two only in subagent context).
- **[docs]** `Notification` input has `notification_type` + `message`. Types: `permission_prompt, idle_prompt, auth_success, elicitation_dialog, elicitation_url_dialog, elicitation_complete, elicitation_response, agent_needs_input, agent_completed, quota_auto_resume_*`. **So yes, it distinguishes permission from idle-waiting.** `permission_prompt`, `elicitation_dialog`, `elicitation_url_dialog` and `agent_needs_input` → blocked; `idle_prompt` → idle (already settled; must not revive).
- **[docs]** `PermissionRequest` fires when a tool call needs a permission decision (matcher = tool name). It's an earlier and more precise blocked signal than Notification. **Nothing fires when the user approves**, so `PostToolUse`/`PostToolUseFailure` → working must clear it. A denial or Esc produces no hook → rely on the next `Stop`/`UserPromptSubmit` (risk noted in OQ1).
- **[docs]** `PreToolUse` with `tool_name == AskUserQuestion` → blocked (herdr's own kimi/qoder integrations use `^AskUserQuestion$` → blocked).
- **[docs]** `Stop` → idle. It includes `last_assistant_message`, which is a good DONE summary source for M4. `StopFailure` → idle (the turn ended with an error). `SubagentStop` → **ignore** (matches herdr's own decision). `SessionEnd` → release (1.5 s shared budget).
- **[docs]** Output safety: exit 0 with **empty stdout** is safe for every event. Plain stdout becomes *context* on `UserPromptSubmit`/`SessionStart`, so the hook must print nothing. Exit 2 blocks on `PreToolUse`/`UserPromptSubmit`/`Stop`, so the hook must always exit 0. Default command timeout is 600 s (30 s on UserPromptSubmit), so set an explicit small `timeout`. Hooks can also be `"async": true`, which fits the non-blocking requirement.
- **[live]** `~/.claude/settings.json` currently has no `hooks` key. The herdr `claude` integration is **not installed** on Thor (`herdr integration status`).

## 9. Blockers / pending live checks

1. **OQ1 (needs a real Claude in a dev pane):** Claude Code's auto-mode classifier denied `herdr pane run` (typing into a pane) even in the isolated session. Running `claude` in a herdr pane needs pane input (`pane run`/`agent start`) or a plugin pane entrypoint. That needs the user's go-ahead.
2. **OQ2:** overlay keyboard capture and `type="plugin_action"` keybinding need an attached client.
3. AC checks against real Claude panes (BLOCKED within 1 s etc.) are part of M1 and depend on (1).

## 10. Decisions taken from these findings

- **Python stdlib** (per spec). Nothing here needs Rust.
- `min_herdr_version = "0.8.2"`. Every method used exists in protocol 20. Tolerate unknown fields and event types so newer versions degrade gracefully.
- Hook talks to the socket directly (python3, 0.5 s timeout, like herdr's own integration). This avoids spawning the herdr binary, which is better for AC3 (<50 ms). Seq = `time.time_ns()`.
- Treat herdr's `done` as DONE-UNSEEN. The daemon adds only a local "dismissed" overlay for `s`/`S`.
