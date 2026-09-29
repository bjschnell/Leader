"""Thin herdr socket client (newline-delimited JSON over a Unix socket).

herdr answers exactly one request per connection, so every call opens its own
connection; a subscription is a long-lived connection that streams events.
Only reads and navigation live here: never add pane input methods (AC2).
"""

import json
import os
import socket
import subprocess

CALL_TIMEOUT_S = 2.0


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def session_socket(session=None, environ=None):
    """Socket path for `session`, else the one herdr injected ($HERDR_SOCKET_PATH)."""
    environ = os.environ if environ is None else environ
    if not session:
        path = environ.get("HERDR_SOCKET_PATH")
        if path:
            return path
        session = "default"
    herdr_bin = environ.get("HERDR_BIN_PATH") or "herdr"
    out = subprocess.run([herdr_bin, "session", "list", "--json"], capture_output=True,
                         text=True, timeout=5, env={k: v for k, v in environ.items()
                                                    if k != "HERDR_SOCKET_PATH"})
    for s in json.loads(out.stdout).get("sessions", []):
        if s.get("name") == session:
            return s["socket_path"]
    raise HerdrError("session_not_found", f"no herdr session named {session!r}")


def session_name(socket_path):
    """Best-effort session name from a socket path (for per-session state dirs)."""
    parent = os.path.basename(os.path.dirname(socket_path))
    grandparent = os.path.basename(os.path.dirname(os.path.dirname(socket_path)))
    return parent if grandparent == "sessions" else "default"


def _read_line(sock):
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(1 << 20)
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\n", 1)[0]


class Client:
    def __init__(self, socket_path, timeout=CALL_TIMEOUT_S):
        self.socket_path = socket_path
        self.timeout = timeout
        self._n = 0

    def call(self, method, params=None):
        self._n += 1
        req = {"id": f"herdr-queue:{os.getpid()}:{self._n}", "method": method, "params": params or {}}
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
            sock.sendall((json.dumps(req) + "\n").encode())
            line = _read_line(sock)
        finally:
            sock.close()
        if not line:
            raise HerdrError("empty_response", method)
        resp = json.loads(line)
        if "error" in resp:
            err = resp["error"] if isinstance(resp["error"], dict) else {"message": str(resp["error"])}
            raise HerdrError(err.get("code", "error"), err.get("message", ""))
        return resp.get("result", {})

    # --- reads ---
    def snapshot(self):
        return self.call("session.snapshot")["snapshot"]

    def screen_state(self, pane_id):
        """What herdr's screen manifest alone would say (ignores hook authority)."""
        return self.call("agent.explain", {"target": pane_id})["explain"].get("state")

    def read(self, pane_id, lines=80, source="recent_unwrapped"):
        return self.call("pane.read", {"pane_id": pane_id, "source": source, "lines": lines})["read"]["text"]

    # --- navigation (allowed by SPEC §3) ---
    def focus_pane(self, pane_id):
        """Switches workspace, tab and pane in one call; herdr marks it seen."""
        return self.call("pane.focus", {"pane_id": pane_id})

    def subscribe(self, subscriptions):
        return Subscription(self.socket_path, subscriptions)


class Subscription:
    """A long-lived events.subscribe connection. Use fileno() with select."""

    def __init__(self, socket_path, subscriptions):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(CALL_TIMEOUT_S)
        self.sock.connect(socket_path)
        req = {"id": "herdr-queue:sub", "method": "events.subscribe",
               "params": {"subscriptions": subscriptions}}
        self.sock.sendall((json.dumps(req) + "\n").encode())
        self._buf = b""
        ack = self._next_line()
        if ack is None or "error" in ack:
            self.close()
            raise HerdrError("subscribe_failed", json.dumps(ack))
        self.sock.setblocking(False)

    def _next_line(self):
        while b"\n" not in self._buf:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def fileno(self):
        return self.sock.fileno()

    def drain(self):
        """Read whatever is available without blocking. Returns (events, alive)."""
        events = []
        try:
            while True:
                chunk = self.sock.recv(1 << 20)
                if not chunk:
                    return self._split(events), False
                self._buf += chunk
        except (BlockingIOError, InterruptedError):
            return self._split(events), True
        except OSError:
            return self._split(events), False

    def _split(self, events):
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        return events

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
