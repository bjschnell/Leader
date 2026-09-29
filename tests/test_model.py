import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "queue"))

import model as m  # noqa: E402


def agent(pid, status, seq=1, tab="w1:t1", tokens=None, **kw):
    return dict({"pane_id": pid, "workspace_id": "w1", "tab_id": tab, "agent": "claude",
                 "agent_status": status, "state_change_seq": seq, "tokens": tokens or {},
                 "focused": False}, **kw)


def snap(*agents):
    return {"workspaces": [{"workspace_id": "w1", "label": "api"}],
            "tabs": [{"tab_id": "w1:t1", "label": "main"}, {"tab_id": "w1:t2", "label": "tests"}],
            "agents": list(agents)}


class UpdateTests(unittest.TestCase):
    def test_categories_labels_and_summaries(self):
        s = m.update({}, snap(
            agent("w1:p1", "blocked", tokens={"hq_msg": "Permission: Bash rm x", "hq_kind": "permission"}),
            agent("w1:p2", "done", tab="w1:t2", tokens={"hq_last": "All tests pass."}),
            agent("w1:p3", "working", tokens={"hq_last": "old"}),
            agent("w1:p4", "idle"),
            agent("w1:p5", "unknown"),
        ), {}, {}, now=100)
        self.assertEqual({p: e["category"] for p, e in s.items()},
                         {"w1:p1": "blocked", "w1:p2": "done", "w1:p3": "working", "w1:p4": "idle", "w1:p5": "idle"})
        self.assertEqual(s["w1:p1"]["summary"], "Permission: Bash rm x")
        self.assertEqual(s["w1:p2"]["summary"], "All tests pass.")
        self.assertIsNone(s["w1:p3"]["summary"])
        self.assertEqual(s["w1:p2"]["label"], "api/tests")

    def test_since_only_changes_on_status_change(self):
        s1 = m.update({}, snap(agent("w1:p1", "working", seq=1)), {}, {}, now=100)
        s2 = m.update(s1, snap(agent("w1:p1", "working", seq=2)), {}, {}, now=105)
        self.assertEqual(s2["w1:p1"]["since"], 100)
        s3 = m.update(s2, snap(agent("w1:p1", "blocked", seq=3)), {}, {}, now=107)
        self.assertEqual(s3["w1:p1"]["since"], 107)

    def test_since_known_only_after_an_observed_transition(self):
        s1 = m.update({}, snap(agent("w1:p1", "working")), {}, {}, now=100)
        self.assertFalse(s1["w1:p1"]["since_known"])      # was already working when first seen
        s2 = m.update(s1, snap(agent("w1:p1", "done")), {}, {}, now=110)
        self.assertTrue(s2["w1:p1"]["since_known"])       # we saw it finish
        s3 = m.update(s2, snap(agent("w1:p1", "done")), {}, {}, now=120)
        self.assertTrue(s3["w1:p1"]["since_known"])

    def test_malformed_snapshot_entries_are_skipped(self):
        s = m.update({}, {"workspaces": [{"label": "x"}], "tabs": None,
                          "agents": [{"agent_status": "done"}, "junk", agent("w1:p1", "done", tokens="bad")]},
                     {}, {}, now=1)
        self.assertEqual(list(s), ["w1:p1"])
        self.assertEqual(m.screen_checks_needed({"agents": [{"agent_status": "working"}]}), [])

    def test_transition_across_restart_has_unknown_start(self):
        restored = {"w1:p1": dict(m.update({}, snap(agent("w1:p1", "working")), {}, {}, now=1)["w1:p1"], restored=True)}
        s = m.update(restored, snap(agent("w1:p1", "done")), {}, {}, now=50)
        self.assertFalse(s["w1:p1"]["since_known"])
        s = m.update(s, snap(agent("w1:p1", "working")), {}, {}, now=60)
        self.assertTrue(s["w1:p1"]["since_known"])

    def test_ranking(self):
        s = m.update({}, snap(agent("w1:p4", "idle"), agent("w1:p3", "working")), {}, {}, now=1)
        s = m.update(s, snap(agent("w1:p4", "idle"), agent("w1:p3", "working"),
                             agent("w1:p2", "done"), agent("w1:p1", "blocked")), {}, {}, now=5)
        s = m.update(s, snap(agent("w1:p4", "idle"), agent("w1:p3", "working"), agent("w1:p2", "done"),
                             agent("w1:p1", "blocked"), agent("w1:p9", "blocked")), {}, {}, now=9)
        self.assertEqual([e["pane_id"] for e in m.ranked(s)], ["w1:p1", "w1:p9", "w1:p2", "w1:p3", "w1:p4"])
        self.assertEqual(m.counts(s), {"blocked": 2, "done": 1, "working": 1, "idle": 1})

    def test_dismissal_is_bound_to_seq(self):
        sn = snap(agent("w1:p2", "done", seq=7))
        self.assertEqual(m.update({}, sn, {}, {"w1:p2": 7}, now=1)["w1:p2"]["category"], "idle")
        self.assertEqual(m.update({}, sn, {}, {"w1:p2": 6}, now=1)["w1:p2"]["category"], "done")
        # a new turn finishing gives a new seq -> the old dismissal no longer applies
        again = snap(agent("w1:p2", "done", seq=9))
        self.assertEqual(m.update({}, again, {}, {"w1:p2": 7}, now=2)["w1:p2"]["category"], "done")

    def test_live_dismissals_prunes(self):
        s = m.update({}, snap(agent("w1:p2", "done", seq=7), agent("w1:p3", "working", seq=4)), {}, {}, now=1)
        self.assertEqual(m.live_dismissals({"w1:p2": 7, "w1:p3": 4, "w1:gone": 1}, s), {"w1:p2": 7})


class StaleTests(unittest.TestCase):
    """Display-only fix for Esc interrupt / Esc deny (no Claude hook fires)."""

    def run_seq(self, status, tokens, screens, stale_after=10):
        state, out = {}, []
        for t, scr in screens:
            state = m.update(state, snap(agent("w1:p1", status, tokens=tokens)), {"w1:p1": scr}, {}, now=t,
                             stale_after=stale_after)
            out.append(state["w1:p1"]["category"])
        return out, state["w1:p1"]

    def test_working_with_idle_screen_becomes_interrupted_after_threshold(self):
        cats, e = self.run_seq("working", {}, [(0, "idle"), (5, "idle"), (10, "idle")])
        self.assertEqual(cats, ["working", "working", "idle"])
        self.assertTrue(e["interrupted"])
        self.assertIn("interrupted", e["summary"])

    def test_screen_flicker_resets_timer(self):
        cats, _ = self.run_seq("working", {}, [(0, "idle"), (6, "working"), (12, "idle"), (20, "idle")])
        self.assertEqual(cats, ["working", "working", "working", "working"])

    def test_blocked_permission_goes_stale_but_question_never_does(self):
        cats, _ = self.run_seq("blocked", {"hq_kind": "permission", "hq_msg": "Permission: Bash x"},
                               [(0, "idle"), (11, "idle")])
        self.assertEqual(cats, ["blocked", "idle"])
        # herdr's screen manifest reads an open AskUserQuestion dialog as idle: must stay blocked
        cats, _ = self.run_seq("blocked", {"hq_kind": "question", "hq_msg": "Tabs or spaces?"},
                               [(0, "idle"), (60, "idle")])
        self.assertEqual(cats, ["blocked", "blocked"])
        cats, _ = self.run_seq("blocked", {}, [(0, "idle"), (60, "idle")])  # screen-detected blocked
        self.assertEqual(cats, ["blocked", "blocked"])

    def test_screen_checks_needed(self):
        self.assertEqual(m.screen_checks_needed(snap(agent("a", "working"), agent("b", "idle"),
                                                     agent("c", "blocked"), agent("d", "done"))), ["a", "c"])


if __name__ == "__main__":
    unittest.main()
