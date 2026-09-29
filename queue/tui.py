"""herdr-queue overlay: a ranked list of agents that need you.

Reads state.json written by the daemon. If no daemon holds the lock for this
session, the TUI takes it and refreshes in-process while open, so there is
always exactly one writer. Rendering is a pure function (render()) so it can be
tested headlessly. Only navigation is sent to herdr (pane.focus).
"""

import argparse
import curses
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import daemon as daemon_mod  # noqa: E402
import herdr  # noqa: E402
import model  # noqa: E402
import summarize  # noqa: E402

TICK_MS = 500
TAGS = {model.BLOCKED: "BLOCKED", model.DONE: "DONE", model.WORKING: "WORKING", model.IDLE: "IDLE"}


def fmt_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d"


def visible_rows(doc, show_idle, dismissals=None, overrides=None):
    """Ranked rows; dismissals and `r` refreshes from this TUI apply immediately."""
    panes = dict((doc or {}).get("panes", {}))
    for pid, (seq, text) in (overrides or {}).items():
        e = panes.get(pid)
        if e and e["state_change_seq"] == seq and text:
            panes[pid] = dict(e, summary=text)
    for pid, seq in (dismissals or {}).items():
        e = panes.get(pid)
        if e and e["category"] == model.DONE and e["state_change_seq"] == seq:
            panes[pid] = dict(e, category=model.IDLE)
    rows = model.ranked(panes)
    return rows if show_idle else [r for r in rows if r["category"] != model.IDLE]


def footer_counts(rows_all):
    c = model.counts({r["pane_id"]: r for r in rows_all})
    parts = [f"{c['blocked']} blocked", f"{c['done']} done", f"{c['working']} working"]
    if c["idle"]:
        parts.append(f"{c['idle']} idle")
    return " · ".join(parts)


def render(doc, width, height, selected, show_idle, now, dismissals=None, status_line=None, overrides=None):
    """Return (lines, rows). lines[i] is (text, style) with style in {title,row,selected,dim,footer}."""
    rows_all = visible_rows(doc, True, dismissals, overrides)
    rows = rows_all if show_idle else [r for r in rows_all if r["category"] != model.IDLE]
    lines = [(f" herdr queue — {(doc or {}).get('session', '?')}", "title")]
    if doc is None:
        lines.append(("  waiting for herdr state…", "dim"))
    elif not rows:
        lines.append(("  nothing needs you" + ("" if show_idle else "  (a: show idle)"), "dim"))
    label_w = max([len(r["label"]) for r in rows] + [5])
    label_w = min(label_w, max(10, width // 4))
    agent_w = min(max([len(r["agent"]) for r in rows] + [5]), 14)
    body_h = max(1, height - 3)
    top = max(0, min(selected - body_h + 1, len(rows) - body_h)) if selected >= body_h else 0
    for i, r in enumerate(rows[top:top + body_h], start=top):
        tag = TAGS[r["category"]] + ("?" if r.get("interrupted") else "")
        head = (f" {tag:<8} {r['label'][:label_w]:<{label_w}}  {r['agent'][:agent_w]:<{agent_w}}"
                f"  {fmt_age(now - r['since']) + ('' if r.get('since_known', True) else '+'):>6}")
        summary = r.get("summary")
        text = f"{head}  — {summary}" if summary else head
        style = "selected" if i == selected else ("dim" if r["category"] == model.IDLE else "row")
        lines.append((text[:max(0, width - 1)], style))
    while len(lines) < height - 1:
        lines.append(("", "row"))
    foot = status_line or f" {footer_counts(rows_all)}   enter jump · s/S seen · r refresh · a idle · q close"
    lines = lines[:height - 1] + [(foot[:max(0, width - 1)], "footer")]
    return lines, rows


def mark_seen(paths, doc, pane_ids):
    """Record local dismissals for DONE panes (bound to their current state_change_seq)."""
    current = daemon_mod.read_json(paths.dismissals, {})
    panes = (doc or {}).get("panes", {})
    for pid in pane_ids:
        e = panes.get(pid)
        if e and e["category"] == model.DONE:
            current[pid] = e["state_change_seq"]
    current = model.live_dismissals(current, panes)
    daemon_mod.write_json_atomic(paths.dismissals, current)
    return current


class App:
    def __init__(self, client, paths, session, cfg):
        self.client = client
        self.paths = paths
        self.cfg = cfg
        self.selected = 0
        self.show_idle = False
        self.status_line = None
        self.status_until = 0
        self.dismissals = daemon_mod.read_json(paths.dismissals, {})
        self.overrides = {}
        self.lock = daemon_mod.acquire_lock(paths.lock)  # None if a daemon is running
        self.embedded = daemon_mod.Daemon(client, paths, session, cfg) if self.lock else None
        self.last_embedded = 0.0

    def load(self):
        if self.embedded and time.time() - self.last_embedded >= min(1.0, float(self.cfg["poll_interval"])):
            try:
                self.embedded.refresh()
            except (OSError, herdr.HerdrError, ValueError) as exc:
                self.flash(f" herdr unavailable: {exc}")
            self.last_embedded = time.time()
        return daemon_mod.read_json(self.paths.state, None)

    def flash(self, text, seconds=3):
        self.status_line = text
        self.status_until = time.time() + seconds

    def close(self):
        if self.lock:
            self.lock.close()

    def handle_key(self, key, rows, doc):
        """Returns 'quit' to exit, else None."""
        if key in (ord("q"), 27):
            return "quit"
        if key in (ord("j"), curses.KEY_DOWN):
            self.selected = min(self.selected + 1, max(0, len(rows) - 1))
        elif key in (ord("k"), curses.KEY_UP):
            self.selected = max(self.selected - 1, 0)
        elif key == ord("a"):
            self.show_idle = not self.show_idle
        elif key == ord("s") and rows:
            self.dismissals = mark_seen(self.paths, doc, [rows[self.selected]["pane_id"]])
        elif key == ord("S"):
            self.dismissals = mark_seen(self.paths, doc, [r["pane_id"] for r in rows])
        elif key == ord("r") and rows:
            row = rows[self.selected]
            category = "done" if row["category"] == model.IDLE else row["category"]
            text = summarize.summarize(category, {}, lambda: self.client.read(
                row["pane_id"], lines=int(self.cfg["tail_lines"])))
            self.overrides[row["pane_id"]] = (row["state_change_seq"], text)
            if not text:
                self.flash(" nothing summary-like in the pane tail")
        elif key in (curses.KEY_ENTER, 10, 13) and rows:
            pid = rows[self.selected]["pane_id"]
            try:
                self.client.focus_pane(pid)
                return "quit"
            except (OSError, herdr.HerdrError) as exc:
                self.flash(f" jump failed: {exc}")
        return None


STYLES = {}


def init_styles():
    curses.use_default_colors()
    pairs = {"title": (curses.COLOR_CYAN, curses.A_BOLD), "row": (-1, 0), "dim": (-1, curses.A_DIM),
             "selected": (-1, curses.A_REVERSE), "footer": (-1, curses.A_DIM)}
    for i, (name, (fg, attr)) in enumerate(pairs.items(), start=1):
        curses.init_pair(i, fg, -1)
        STYLES[name] = curses.color_pair(i) | attr


def run(stdscr, app):
    curses.curs_set(0)
    init_styles()
    stdscr.timeout(TICK_MS)
    while True:
        doc = app.load()
        h, w = stdscr.getmaxyx()
        if app.status_line and time.time() > app.status_until:
            app.status_line = None
        _, rows = render(doc, w, h, app.selected, app.show_idle, time.time(), app.dismissals,
                         overrides=app.overrides)
        app.selected = min(app.selected, max(0, len(rows) - 1))
        lines, rows = render(doc, w, h, app.selected, app.show_idle, time.time(), app.dismissals,
                             app.status_line, app.overrides)
        stdscr.erase()
        for y, (text, style) in enumerate(lines[:h]):
            try:
                stdscr.addnstr(y, 0, text, w - 1, STYLES.get(style, 0))
            except curses.error:
                pass
        stdscr.refresh()
        key = stdscr.getch()
        if key == -1:
            continue
        if app.handle_key(key, rows, doc) == "quit":
            return


def main(argv=None):
    ap = argparse.ArgumentParser(description="herdr-queue overlay")
    ap.add_argument("--session")
    ap.add_argument("--config")
    ap.add_argument("--print", action="store_true", help="render once to stdout (no curses) and exit")
    ap.add_argument("--all", action="store_true", help="include idle agents")
    args = ap.parse_args(argv)

    cfg = config.load(args.config)
    socket_path = herdr.session_socket(args.session or cfg["session"])
    session = herdr.session_name(socket_path)
    paths = daemon_mod.Paths(config.state_dir(session))
    app = App(herdr.Client(socket_path), paths, session, cfg)
    try:
        if args.print:
            app.last_embedded = 0
            doc = app.load()
            lines, _ = render(doc, 120, 40, -1, args.all, time.time(), app.dismissals)
            print("\n".join(text.rstrip() for text, _ in lines if text.strip()))
            return 0
        os.environ.setdefault("ESCDELAY", "25")
        curses.wrapper(run, app)
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
