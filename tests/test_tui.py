"""Headless TUI tests: render() to a list of strings, key handling without curses."""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "queue"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import daemon as d  # noqa: E402
import tui  # noqa: E402


def entry(pid, category, since, label="api/main", agent="claude", summary=None, seq=1, **kw):
    return dict({"pane_id": pid, "category": category, "since": since, "label": label, "agent": agent,
                 "summary": summary, "state_change_seq": seq, "status": category, "interrupted": False}, **kw)


DOC = {"session": "dev", "panes": {
    "w1:p1": entry("w1:p1", "working", 100),
    "w1:p2": entry("w1:p2", "blocked", 500, summary="Permission: Bash rm -rf build", label="api/tests"),
    "w1:p3": entry("w1:p3", "done", 300, summary="All 42 tests pass.", seq=7),
    "w1:p4": entry("w1:p4", "idle", 50),
    "w1:p5": entry("w1:p5", "blocked", 200, summary="Tabs or spaces?"),
}}


def texts(lines):
    return [t for t, _ in lines]


class RenderTests(unittest.TestCase):
    def test_ranked_rows_and_footer(self):
        lines, rows = tui.render(DOC, 100, 12, 0, False, now=600)
        self.assertEqual([r["pane_id"] for r in rows], ["w1:p5", "w1:p2", "w1:p3", "w1:p1"])
        out = texts(lines)
        self.assertIn("Leader — dev", out[0])
        self.assertTrue(out[1].startswith(" BLOCKED"))
        self.assertIn("6m", out[1])                       # blocked since 200, now 600
        self.assertIn("— Tabs or spaces?", out[1])
        self.assertIn("— Permission: Bash rm -rf build", out[2])
        self.assertTrue(out[3].startswith(" DONE"))
        self.assertTrue(out[4].startswith(" WORKING"))
        self.assertIn("2 blocked · 1 done · 1 working · 1 idle", out[-1])
        self.assertEqual(lines[1][1], "selected")

    def test_idle_toggle(self):
        _, rows = tui.render(DOC, 100, 12, 0, True, now=600)
        self.assertEqual(rows[-1]["pane_id"], "w1:p4")

    def test_width_is_respected(self):
        lines, _ = tui.render(DOC, 40, 12, 0, False, now=600)
        self.assertTrue(all(len(t) <= 39 for t in texts(lines)))

    def test_empty_and_missing_state(self):
        self.assertIn("nothing needs you", texts(tui.render({"session": "x", "panes": {}}, 80, 5, 0, False, 0)[0])[1])
        self.assertIn("waiting for herdr", texts(tui.render(None, 80, 5, 0, False, 0)[0])[1])

    def test_scrolls_to_keep_selection_visible(self):
        doc = {"session": "x", "panes": {f"w1:p{i}": entry(f"w1:p{i}", "blocked", i) for i in range(20)}}
        lines, _ = tui.render(doc, 80, 6, 15, False, now=100)
        self.assertEqual([s for _, s in lines].count("selected"), 1)

    def test_interrupted_rows_stay_visible_without_idle_toggle(self):
        doc = {"session": "x", "panes": {"a": entry("a", "idle", 1, interrupted=True), "b": entry("b", "idle", 1)}}
        _, rows = tui.render(doc, 80, 6, 0, False, 10)
        self.assertEqual([r["pane_id"] for r in rows], ["a"])

    def test_interrupted_marker(self):
        doc = {"session": "x", "panes": {"a": entry("a", "idle", 1, interrupted=True, summary="interrupted?")}}
        self.assertTrue(texts(tui.render(doc, 80, 5, 0, True, 10)[0])[1].startswith(" IDLE?"))

    def test_local_dismissal_applies_immediately(self):
        _, rows = tui.render(DOC, 100, 12, 0, False, 600, dismissals={"w1:p3": 7})
        self.assertNotIn("w1:p3", [r["pane_id"] for r in rows])
        _, rows = tui.render(DOC, 100, 12, 0, False, 600, dismissals={"w1:p3": 6})  # stale seq
        self.assertIn("w1:p3", [r["pane_id"] for r in rows])

    def test_unknown_start_is_marked(self):
        doc = {"session": "x", "panes": {"a": entry("a", "blocked", 0, since_known=False)}}
        self.assertIn("10s+", texts(tui.render(doc, 80, 5, 0, False, 10)[0])[1])

    def test_ages(self):
        self.assertEqual([tui.fmt_age(s) for s in (5, 125, 3700, 90000)], ["5s", "2m", "1h01m", "1d"])


class FakeClient:
    def __init__(self):
        self.focused = []

    def focus_pane(self, pid):
        self.focused.append(pid)

    def read(self, pid, lines=80):
        return "● Migrated 3 tables.\n\nDone: the schema is at v12.\n\n❯ \n"


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.paths = d.Paths(tempfile.mkdtemp())
        self.app = tui.App.__new__(tui.App)
        self.app.client, self.app.paths = FakeClient(), self.paths
        self.app.selected, self.app.show_idle, self.app.dismissals = 0, False, {}
        self.app.status_line, self.app.status_until = None, 0
        self.app.overrides, self.app.cfg = {}, {"tail_lines": 80}
        _, self.rows = tui.render(DOC, 100, 12, 0, False, 600)

    def test_navigation_and_jump(self):
        self.app.handle_key(ord("j"), self.rows, DOC)
        self.app.handle_key(ord("j"), self.rows, DOC)
        self.assertEqual(self.app.selected, 2)
        self.assertEqual(self.app.handle_key(10, self.rows, DOC), "quit")
        self.assertEqual(self.app.client.focused, ["w1:p3"])
        self.app.handle_key(ord("k"), self.rows, DOC)
        self.assertEqual(self.app.selected, 1)

    def test_mark_seen_writes_dismissals_for_done_only(self):
        self.app.handle_key(ord("S"), self.rows, DOC)
        with open(self.paths.dismissals) as fh:
            self.assertEqual(json.load(fh), {"w1:p3": 7})

    def test_refresh_summary_overrides_until_next_transition(self):
        self.app.selected = 2                              # the DONE row (w1:p3, seq 7)
        self.app.handle_key(ord("r"), self.rows, DOC)
        self.assertEqual(self.app.overrides["w1:p3"], (7, "Done: the schema is at v12."))
        _, rows = tui.render(DOC, 100, 12, 0, False, 600, overrides=self.app.overrides)
        self.assertEqual([r["summary"] for r in rows if r["pane_id"] == "w1:p3"], ["Done: the schema is at v12."])
        moved_on = {"session": "dev", "panes": dict(DOC["panes"], **{"w1:p3": entry("w1:p3", "done", 900, seq=8)})}
        _, rows = tui.render(moved_on, 100, 12, 0, False, 1000, overrides=self.app.overrides)
        self.assertEqual([r["summary"] for r in rows if r["pane_id"] == "w1:p3"], [None])

    def test_quit_keys(self):
        self.assertEqual(self.app.handle_key(ord("q"), self.rows, DOC), "quit")
        self.assertEqual(self.app.handle_key(27, self.rows, DOC), "quit")


if __name__ == "__main__":
    unittest.main()
