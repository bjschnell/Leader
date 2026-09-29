"""herdr-queue daemon: keeps <state_dir>/state.json current for one herdr session.

Pattern (docs/findings.md §4): events are pokes, the snapshot is authoritative.
Subscribe (global lifecycle + per-pane status), snapshot, recompute, write;
re-snapshot on every poke and at least every poll_interval; resubscribe when
the set of agent panes changes; reconnect with backoff if herdr goes away.
Reads and writes only its own files; never writes herdr state or pane input.
"""

import argparse
import fcntl
import json
import os
import select
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import herdr  # noqa: E402
import model  # noqa: E402

GLOBAL_EVENTS = [
    "pane.created", "pane.closed", "pane.exited", "pane.updated", "pane.focused",
    "pane.moved", "pane.agent_detected", "tab.focused", "tab.closed", "tab.renamed",
    "tab.moved", "workspace.focused", "workspace.closed", "workspace.renamed",
]
DEBOUNCE_S = 0.05
STALE_POLL_S = 1.0


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


class Paths:
    def __init__(self, directory):
        self.dir = directory
        self.state = os.path.join(directory, "state.json")
        self.dismissals = os.path.join(directory, "dismissals.json")
        self.lock = os.path.join(directory, "daemon.lock")


def subscriptions_for(pane_ids):
    subs = [{"type": t} for t in GLOBAL_EVENTS]
    subs += [{"type": "pane.agent_status_changed", "pane_id": p} for p in sorted(pane_ids)]
    return subs


class Daemon:
    def __init__(self, client, paths, session, cfg, clock=time.time):
        self.client = client
        self.paths = paths
        self.session = session
        self.cfg = cfg
        self.clock = clock
        persisted = read_json(paths.state, {})
        self.state = persisted.get("panes", {}) if persisted.get("socket") == client.socket_path else {}
        self.sub = None
        self.sub_panes = None
        self.last_written = None

    def refresh(self):
        """One snapshot -> state.json pass. Returns the snapshot's agent pane ids."""
        snapshot = self.client.snapshot()
        screen = {}
        for pid in model.screen_checks_needed(snapshot):
            try:
                screen[pid] = self.client.screen_state(pid)
            except (herdr.HerdrError, OSError, KeyError):
                pass
        dismissals = read_json(self.paths.dismissals, {})
        now = self.clock()
        self.state = model.update(self.state, snapshot, screen, dismissals, now,
                                  stale_after=float(self.cfg["stale_after"]))
        doc = {
            "version": 1,
            "session": self.session,
            "socket": self.client.socket_path,
            "herdr_version": snapshot.get("version"),
            "focused_pane_id": snapshot.get("focused_pane_id"),
            "daemon_pid": os.getpid(),
            "panes": self.state,
            "counts": model.counts(self.state),
        }
        comparable = json.dumps(doc, sort_keys=True)
        if comparable != self.last_written:
            doc["updated"] = now
            write_json_atomic(self.paths.state, doc)
            self.last_written = comparable
        else:
            self.touch(now)
        return {a["pane_id"] for a in snapshot.get("agents", [])}

    def touch(self, now):
        """Heartbeat so readers can tell a live daemon from a stale file."""
        try:
            os.utime(self.paths.state, (now, now))
        except OSError:
            pass

    def ensure_subscription(self, panes):
        if self.sub is not None and panes == self.sub_panes:
            return False
        if self.sub is not None:
            self.sub.close()
        self.sub = self.client.subscribe(subscriptions_for(panes))
        self.sub_panes = panes
        return True

    def close(self):
        if self.sub is not None:
            self.sub.close()
            self.sub = None
            self.sub_panes = None

    def wait_timeout(self):
        busy = any(e["screen_idle_since"] is not None and not e["interrupted"] for e in self.state.values())
        return min(float(self.cfg["poll_interval"]), STALE_POLL_S) if busy else float(self.cfg["poll_interval"])

    def step(self):
        """Refresh, (re)subscribe, then block until a poke or the poll interval."""
        panes = self.refresh()
        if self.ensure_subscription(panes):
            panes = self.refresh()  # close the gap between the snapshot and the new subscription
            self.ensure_subscription(panes)
        ready, _, _ = select.select([self.sub], [], [], self.wait_timeout())
        if ready:
            time.sleep(DEBOUNCE_S)
            _, alive = self.sub.drain()
            if not alive:
                self.close()

    def run_forever(self, stop):
        backoff = 1.0
        while not stop["flag"]:
            try:
                self.step()
                backoff = 1.0
            except (OSError, herdr.HerdrError, ValueError) as exc:
                self.close()
                print(f"herdr-queue: {exc}; retrying in {backoff:.0f}s", file=sys.stderr, flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
        self.close()


def acquire_lock(path):
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main(argv=None):
    ap = argparse.ArgumentParser(description="herdr-queue state daemon")
    ap.add_argument("--session", help="herdr session name (default: the injected one)")
    ap.add_argument("--config", help="config.toml path")
    ap.add_argument("--once", action="store_true", help="write state once, print it, exit")
    args = ap.parse_args(argv)

    cfg = config.load(args.config)
    socket_path = herdr.session_socket(args.session or cfg["session"])
    session = herdr.session_name(socket_path)
    paths = Paths(config.state_dir(session))
    daemon = Daemon(herdr.Client(socket_path), paths, session, cfg)

    lock = acquire_lock(paths.lock)
    if args.once:
        if lock is not None:  # no daemon running: we are the only writer
            daemon.refresh()
            lock.close()
        with open(paths.state, encoding="utf-8") as fh:
            sys.stdout.write(fh.read())
        return 0
    if lock is None:
        print(f"herdr-queue: daemon already running for session {session}", file=sys.stderr)
        return 0
    stop = {"flag": False}

    def on_signal(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        daemon.run_forever(stop)
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
