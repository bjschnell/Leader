"""leader daemon: keeps <state_dir>/state.json current for one herdr session.

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
import llm  # noqa: E402
import model  # noqa: E402
import summarize  # noqa: E402

GLOBAL_EVENTS = [
    "pane.created", "pane.closed", "pane.exited", "pane.updated", "pane.focused",
    "pane.moved", "pane.agent_detected", "tab.focused", "tab.closed", "tab.renamed",
    "tab.moved", "workspace.focused", "workspace.closed", "workspace.renamed",
]
DEBOUNCE_S = 0.05
STALE_POLL_S = 1.0
MIN_POLL_S = 0.2


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
        panes = persisted.get("panes") if persisted.get("socket") == client.socket_path else None
        self.state = {pid: dict(e, restored=True) for pid, e in (panes or {}).items() if isinstance(e, dict)}
        self.sub = None
        self.sub_panes = None
        self.last_written = None
        self.summary_cache = {}
        self.llm = llm.LLMSummarizer(cfg)
        for pid, e in self.state.items():  # don't pay twice for a summary we already have
            if isinstance(e.get("llm"), list) and len(e["llm"]) == 3:
                self.llm.seed((pid, e["llm"][0], e["llm"][1]), e["llm"][2])

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
        self.fill_summaries(snapshot)
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

    def fill_summaries(self, snapshot):
        """One line per BLOCKED/DONE row: hook text, else pane-tail heuristics (cached per
        transition, so each tail is read once). With summaries.llm on, an LLM line replaces
        it once ready, except for BLOCKED rows whose hook text is already exact."""
        tokens = {a.get("pane_id"): a.get("tokens") or {} for a in snapshot.get("agents") or []
                  if isinstance(a, dict)}
        lines = int(self.cfg["tail_lines"])
        live = set()
        for pid, entry in self.state.items():
            if entry["category"] not in (model.BLOCKED, model.DONE):
                continue
            key = (pid, entry["state_change_seq"], entry["category"])
            live.add(key)

            def read_tail(pid=pid):
                return self.client.read(pid, lines=lines)

            if not entry["summary"]:
                if key not in self.summary_cache:
                    self.summary_cache[key] = summarize.summarize(entry["category"], tokens.get(pid), read_tail)
                entry["summary"] = self.summary_cache[key]
            if entry["category"] == model.BLOCKED and (tokens.get(pid) or {}).get(model.MSG_TOKEN):
                continue
            self.llm.request(key, entry["category"], read_tail, hint=entry["summary"])
            text = self.llm.get(key)
            if text:
                entry["summary"] = text
                entry["llm"] = [entry["state_change_seq"], entry["category"], text]
        self.summary_cache = {k: v for k, v in self.summary_cache.items() if k in live}
        self.llm.prune(live)

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
        poll = max(MIN_POLL_S, float(self.cfg["poll_interval"]))
        return min(poll, STALE_POLL_S) if busy or self.llm.busy() else poll

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
            except Exception as exc:  # never die: herdr restarts, upgrades, odd data
                self.close()
                print(f"leader: {exc}; retrying in {backoff:.0f}s", file=sys.stderr, flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
        self.close()


def acquire_lock(path):
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main(argv=None):
    ap = argparse.ArgumentParser(description="leader state daemon")
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
        try:
            with open(paths.state, encoding="utf-8") as fh:
                sys.stdout.write(fh.read())
        except FileNotFoundError:
            print(f"leader: no state yet for session {session}", file=sys.stderr)
            return 1
        return 0
    if lock is None:
        print(f"leader: daemon already running for session {session}", file=sys.stderr)
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
