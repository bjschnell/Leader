"""Pure queue model: herdr snapshot (+ screen verdicts) -> per-pane state -> ranked rows.

No I/O here so the whole state machine is unit-testable. Seen semantics:
herdr itself reports `done` for settled-but-unseen agents and flips them to
`idle` when their tab is focused (docs/findings.md §3). The only local seen
state is a *dismissal* from the TUI (`s`/`S`), keyed by the agent's
state_change_seq so any new report (e.g. a new turn) invalidates it.
"""

BLOCKED, DONE, WORKING, IDLE = "blocked", "done", "working", "idle"
RANK = {BLOCKED: 0, DONE: 1, WORKING: 2, IDLE: 3}

MSG_TOKEN, LAST_TOKEN, KIND_TOKEN = "hq_msg", "hq_last", "hq_kind"
DEFAULT_STALE_AFTER_S = 10.0


def _labels(snapshot):
    ws = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in snapshot.get("workspaces", [])}
    tabs = {t["tab_id"]: t.get("label") or t["tab_id"] for t in snapshot.get("tabs", [])}
    return ws, tabs


def screen_checks_needed(snapshot):
    """Pane ids whose hook-reported state should be cross-checked against the screen."""
    return [a["pane_id"] for a in snapshot.get("agents", [])
            if a.get("agent_status") in (WORKING, BLOCKED)]


def update(prev, snapshot, screen, dismissals, now, stale_after=DEFAULT_STALE_AFTER_S):
    """Compute the new per-pane state.

    prev:       {pane_id: entry} from the last update (or persisted state)
    snapshot:   herdr session.snapshot result
    screen:     {pane_id: screen-manifest state} for panes in screen_checks_needed()
    dismissals: {pane_id: state_change_seq} marked seen locally by the TUI
    """
    ws_labels, tab_labels = _labels(snapshot)
    out = {}
    for agent in snapshot.get("agents", []):
        pid = agent["pane_id"]
        old = prev.get(pid, {})
        status = agent.get("agent_status") or "unknown"
        seq = agent.get("state_change_seq")
        tokens = agent.get("tokens") or {}

        since = old.get("since", now) if old.get("status") == status else now

        # Display-only reconciliation for hook gaps (Esc interrupt / Esc deny):
        # the screen shows an idle prompt while our authority still says busy.
        stale_candidate = status == WORKING or (status == BLOCKED and tokens.get(KIND_TOKEN) == "permission")
        screen_idle = stale_candidate and screen.get(pid) == IDLE
        if screen_idle:
            screen_idle_since = old.get("screen_idle_since") if old.get("status") == status else None
            if screen_idle_since is None:
                screen_idle_since = now
        else:
            screen_idle_since = None
        interrupted = screen_idle_since is not None and now - screen_idle_since >= stale_after

        dismissed = status == DONE and dismissals.get(pid) is not None and dismissals.get(pid) == seq

        if interrupted:
            category = IDLE
        elif status == BLOCKED:
            category = BLOCKED
        elif status == DONE and not dismissed:
            category = DONE
        elif status == WORKING:
            category = WORKING
        else:
            category = IDLE  # idle, dismissed done, unknown

        if category == BLOCKED:
            summary = tokens.get(MSG_TOKEN)
        elif category == WORKING:
            summary = None
        else:
            summary = tokens.get(LAST_TOKEN)
        if interrupted:
            summary = "interrupted? (no hook fired; screen shows an idle prompt)"

        out[pid] = {
            "pane_id": pid,
            "workspace_id": agent.get("workspace_id"),
            "tab_id": agent.get("tab_id"),
            "label": f"{ws_labels.get(agent.get('workspace_id'), agent.get('workspace_id'))}/"
                     f"{tab_labels.get(agent.get('tab_id'), agent.get('tab_id'))}",
            "agent": agent.get("name") or agent.get("display_agent") or agent.get("agent") or "?",
            "status": status,
            "state_change_seq": seq,
            "since": since,
            "screen_idle_since": screen_idle_since,
            "interrupted": interrupted,
            "category": category,
            "summary": summary,
            "focused": bool(agent.get("focused")),
        }
    return out


def ranked(state):
    """Rows ordered BLOCKED, DONE, WORKING, IDLE; oldest first within a category."""
    return sorted(state.values(), key=lambda e: (RANK[e["category"]], e["since"], e["pane_id"]))


def counts(state):
    c = {BLOCKED: 0, DONE: 0, WORKING: 0, IDLE: 0}
    for e in state.values():
        c[e["category"]] += 1
    return c


def live_dismissals(dismissals, state):
    """Drop dismissals that no longer match the pane's current report."""
    return {pid: seq for pid, seq in dismissals.items()
            if pid in state and state[pid]["state_change_seq"] == seq and state[pid]["status"] == DONE}
