"""Claude Code hook -> herdr lifecycle report.

Invoked by claude-hook.sh with the hook JSON on stdin. Maps the event to a
herdr agent state and sends it straight to $HERDR_SOCKET_PATH (one short-lived
Unix socket connection, no subprocess). Must never print, never block Claude,
and always exit 0.

See docs/findings.md sections 2 and 8 for why each event maps the way it does.
"""

import json
import os
import socket
import sys
import time

SOURCE = "custom:leader"
META_SOURCE = "custom:leader-meta"
AGENT = "claude"
SOCKET_TIMEOUT_S = 0.3     # connect + send
REPLY_WAIT_S = 0.025       # herdr applies requests even if we hang up; it sometimes
                            # stalls ~100 ms before replying, which Claude should not pay for
MSG_TOKEN = "leader_msg"      # what a blocked agent needs (or null)
LAST_TOKEN = "leader_last"    # last assistant line when a turn ends
KIND_TOKEN = "leader_kind"    # why blocked: permission | question | input (or null)
TOKEN_TTL_MS = 86_400_000  # herdr maximum; tokens are cleared on transitions anyway
MAX_TEXT = 200            # herdr caps presentation text at 80; keep a bit more for the daemon

BLOCKING_NOTIFICATIONS = {
    "permission_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
}
SESSION_START_IDLE_SOURCES = {"startup", "resume", "clear"}
# A subagent's permission dialog / question blocks the same pane, and its tool
# results mean the pane is working again.
SUBAGENT_EVENTS = {"PreToolUse", "PermissionRequest", "PostToolUse", "PostToolUseFailure", "Notification"}


def one_line(text, limit=MAX_TEXT):
    if not isinstance(text, str):
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    line = " ".join(lines[0].split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def last_line(text, limit=MAX_TEXT):
    if not isinstance(text, str):
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return one_line(lines[-1], limit) if lines else None


def summary_line(text, limit=MAX_TEXT):
    """Most summary-like line of the final reply (queue/summarize.py), else its last line."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "queue"))
        import summarize
        return summarize.pick_summary_line(text, limit)
    except Exception:
        return last_line(text, limit)


def describe_tool(payload):
    tool = payload.get("tool_name") or "tool"
    tool_input = payload.get("tool_input")
    detail = None
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "url", "pattern", "description", "prompt"):
            if isinstance(tool_input.get(key), str) and tool_input[key].strip():
                detail = tool_input[key]
                break
    return one_line(f"Permission: {tool} {detail}" if detail else f"Permission: {tool}")


def ask_user_question_text(payload):
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        questions = tool_input.get("questions")
        if isinstance(questions, list) and questions and isinstance(questions[0], dict):
            return one_line(questions[0].get("question"))
        return one_line(tool_input.get("question"))
    return None


def plan(payload):
    """Map a hook payload to an action.

    Returns None (do nothing) or a dict:
      {"kind": "report", "state": ..., "message": str|None, "tokens": {...}}
      {"kind": "release"}
    """
    if not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name")
    if payload.get("agent_id") and event not in SUBAGENT_EVENTS:
        return None  # a subagent's turn/session events don't settle the main agent

    if event == "SessionStart":
        if payload.get("source") in SESSION_START_IDLE_SOURCES:
            return {"kind": "report", "state": "idle", "message": None,
                    "tokens": {MSG_TOKEN: None, KIND_TOKEN: None, LAST_TOKEN: None}}
        return None  # "compact" can fire mid-turn
    if event == "UserPromptSubmit":
        return {"kind": "report", "state": "working", "message": None,
                "tokens": {MSG_TOKEN: None, KIND_TOKEN: None}}
    if event == "PreToolUse":
        if payload.get("tool_name") == "AskUserQuestion":
            msg = ask_user_question_text(payload) or "Claude is asking a question"
            return {"kind": "report", "state": "blocked", "message": msg,
                    "tokens": {MSG_TOKEN: msg, KIND_TOKEN: "question"}}
        return None
    if event == "PermissionRequest":
        if payload.get("tool_name") == "AskUserQuestion":
            msg = ask_user_question_text(payload) or "Claude is asking a question"
            kind = "question"
        else:
            msg = describe_tool(payload)
            kind = "permission"
        return {"kind": "report", "state": "blocked", "message": msg,
                "tokens": {MSG_TOKEN: msg, KIND_TOKEN: kind}}
    if event in ("PostToolUse", "PostToolUseFailure"):
        return {"kind": "report", "state": "working", "message": None,
                "tokens": {MSG_TOKEN: None, KIND_TOKEN: None}}
    if event == "Notification":
        ntype = payload.get("notification_type")
        if ntype in BLOCKING_NOTIFICATIONS:
            msg = one_line(payload.get("message")) or "Claude needs your input"
            # permission_prompt trails PermissionRequest by seconds with a vaguer
            # message; keep the specific token PermissionRequest already set.
            tokens = {} if ntype == "permission_prompt" else {MSG_TOKEN: msg, KIND_TOKEN: "input"}
            return {"kind": "report", "state": "blocked", "message": msg, "tokens": tokens}
        return None  # idle_prompt etc.: already settled, must not revive
    if event in ("Stop", "StopFailure"):
        last = summary_line(payload.get("last_assistant_message"))
        if event == "StopFailure":
            err = payload.get("error_type") or payload.get("error") or "error"
            last = one_line(f"Turn failed: {err}")
        return {"kind": "report", "state": "idle", "message": last,
                "tokens": {MSG_TOKEN: None, KIND_TOKEN: None, LAST_TOKEN: last}}
    if event == "SessionEnd":
        return {"kind": "release"}
    return None


def requests_for(action, pane_id, seq):
    reqs = []
    if action["kind"] == "release":
        reqs.append(("pane.release_agent", {
            "pane_id": pane_id, "source": SOURCE, "agent": AGENT, "seq": seq}))
        reqs.append(("pane.report_metadata", {
            "pane_id": pane_id, "source": META_SOURCE, "seq": seq,
            "tokens": {MSG_TOKEN: None, KIND_TOKEN: None, LAST_TOKEN: None}}))
        return reqs
    params = {"pane_id": pane_id, "source": SOURCE, "agent": AGENT,
              "state": action["state"], "seq": seq}
    if action.get("message"):
        params["message"] = action["message"]
    reqs.append(("pane.report_agent", params))
    tokens = {k: v for k, v in action.get("tokens", {}).items()}
    if tokens:
        reqs.append(("pane.report_metadata", {
            "pane_id": pane_id, "source": META_SOURCE, "seq": seq,
            "tokens": tokens, "ttl_ms": TOKEN_TTL_MS}))
    return reqs


def send(socket_path, reqs, timeout=SOCKET_TIMEOUT_S):
    """Send each request on its own connection (herdr answers one request per connection)."""
    for i, (method, params) in enumerate(reqs):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        try:
            client.connect(socket_path)
            req = {"id": f"leader:{os.getpid()}:{i}", "method": method, "params": params}
            client.sendall((json.dumps(req) + "\n").encode())
            client.settimeout(REPLY_WAIT_S)
            try:
                client.recv(4096)
            except OSError:
                pass
        finally:
            client.close()


def debug_log(line):
    path = os.environ.get("LEADER_HOOK_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def main(stdin=None, environ=None):
    environ = os.environ if environ is None else environ
    pane_id = environ.get("HERDR_PANE_ID")
    socket_path = environ.get("HERDR_SOCKET_PATH")
    if environ.get("HERDR_ENV") != "1" or not pane_id or not socket_path:
        return 0
    try:
        raw = (stdin or sys.stdin).read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    action = plan(payload)
    debug_log(json.dumps({"t": time.time(), "event": payload.get("hook_event_name"),
                          "ntype": payload.get("notification_type"),
                          "tool": payload.get("tool_name"), "agent_id": payload.get("agent_id"),
                          "action": action}))
    if action is None:
        return 0
    try:
        send(socket_path, requests_for(action, pane_id, time.time_ns()))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
