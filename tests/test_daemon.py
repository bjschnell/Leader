import json
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "queue"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import daemon as d  # noqa: E402
import herdr  # noqa: E402
from fake_herdr import FakeHerdr  # noqa: E402


def agent(pid, status, seq=1, **kw):
    return dict({"pane_id": pid, "workspace_id": "w1", "tab_id": "w1:t1", "agent": "claude",
                 "agent_status": status, "state_change_seq": seq, "tokens": {}, "focused": False}, **kw)


class World:
    """Mutable herdr world the fake serves."""

    def __init__(self):
        self.agents = []
        self.screen = {}
        self.tails = {}
        self.reads = []

    def reply(self, req):
        m = req["method"]
        if m == "session.snapshot":
            return {"type": "session_snapshot", "snapshot": {
                "version": "0.8.2", "protocol": 20, "workspaces": [{"workspace_id": "w1", "label": "api"}],
                "tabs": [{"tab_id": "w1:t1", "label": "main"}], "panes": [], "layouts": [],
                "agents": list(self.agents), "focused_pane_id": None}}
        if m == "pane.read":
            self.reads.append(req["params"]["pane_id"])
            return {"type": "pane_read", "read": {"text": self.tails.get(req["params"]["pane_id"], "")}}
        if m == "agent.explain":
            return {"type": "agent_explain", "explain": {"state": self.screen.get(req["params"]["target"], "idle")}}
        if m == "events.subscribe":
            return {"type": "subscription_started"}
        return {"type": "ok"}


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.fake = FakeHerdr(reply=self.world.reply)
        self.dir = tempfile.mkdtemp()
        self.paths = d.Paths(self.dir)
        self.cfg = dict(config.DEFAULTS, poll_interval=5.0)
        self.now = [1000.0]
        self.daemon = d.Daemon(herdr.Client(self.fake.path), self.paths, "test", self.cfg,
                               clock=lambda: self.now[0])

    def tearDown(self):
        self.daemon.close()
        self.fake.close()

    def state(self):
        with open(self.paths.state) as fh:
            return json.load(fh)

    def test_refresh_writes_ranked_state(self):
        self.world.agents = [agent("w1:p1", "done", tokens={"hq_last": "shipped"}),
                             agent("w1:p2", "blocked", tokens={"hq_msg": "Permission: Bash x", "hq_kind": "permission"})]
        self.world.screen = {"w1:p2": "blocked"}
        self.daemon.refresh()
        s = self.state()
        self.assertEqual(s["counts"], {"blocked": 1, "done": 1, "working": 0, "idle": 0})
        self.assertEqual(s["panes"]["w1:p1"]["summary"], "shipped")
        self.assertEqual(s["panes"]["w1:p2"]["label"], "api/main")
        explained = [r["params"]["target"] for r in self.fake.requests if r["method"] == "agent.explain"]
        self.assertEqual(explained, ["w1:p2"])  # only busy panes are cross-checked

    def test_tail_summary_for_undescribed_panes_is_read_once(self):
        tail = ("● Here are the flaky tests:\n  1. test_a\n  2. test_b\n\n✻ Baked for 3s\n"
                "────\n❯ \n────\n  ⏸ manual mode on\n")
        self.world.agents = [agent("w1:p1", "done", seq=3, agent="codex")]
        self.world.tails = {"w1:p1": tail}
        for _ in range(3):
            self.daemon.refresh()
        self.assertEqual(self.state()["panes"]["w1:p1"]["summary"], "Here are the flaky tests:")
        self.assertEqual(self.world.reads, ["w1:p1"])
        self.world.agents = [agent("w1:p1", "done", seq=5, agent="codex")]   # a new turn finished
        self.daemon.refresh()
        self.assertEqual(self.world.reads, ["w1:p1", "w1:p1"])

    def test_hook_tokens_avoid_tail_reads(self):
        self.world.agents = [agent("w1:p1", "blocked", tokens={"hq_msg": "Tabs or spaces?", "hq_kind": "question"})]
        self.daemon.refresh()
        self.assertEqual(self.world.reads, [])

    def test_dismissals_file_is_honoured(self):
        self.world.agents = [agent("w1:p1", "done", seq=4)]
        with open(self.paths.dismissals, "w") as fh:
            json.dump({"w1:p1": 4}, fh)
        self.daemon.refresh()
        self.assertEqual(self.state()["panes"]["w1:p1"]["category"], "idle")

    def test_resubscribes_when_agent_panes_change(self):
        self.world.agents = [agent("w1:p1", "working")]
        self.daemon.ensure_subscription(self.daemon.refresh())
        self.world.agents.append(agent("w1:p2", "idle"))
        self.assertTrue(self.daemon.ensure_subscription(self.daemon.refresh()))
        self.assertFalse(self.daemon.ensure_subscription(self.daemon.refresh()))
        per_pane = [sorted(s["pane_id"] for s in subs if s["type"] == "pane.agent_status_changed")
                    for subs in self.fake.subscriptions()]
        self.assertEqual(per_pane, [["w1:p1"], ["w1:p1", "w1:p2"]])

    def test_event_poke_wakes_step_before_poll_interval(self):
        self.world.agents = [agent("w1:p1", "working")]
        self.world.screen = {"w1:p1": "working"}
        self.daemon.step()  # establishes the subscription, then waits out the poll interval... unless poked
        self.world.agents = [agent("w1:p1", "blocked", seq=2)]
        threading.Timer(0.2, lambda: self.fake.push(
            {"event": "pane.agent_status_changed", "data": {"pane_id": "w1:p1", "agent_status": "blocked"}})).start()
        t0 = time.monotonic()
        self.daemon.step()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.daemon.refresh()
        self.assertEqual(self.state()["panes"]["w1:p1"]["category"], "blocked")

    def test_since_survives_restart(self):
        self.world.agents = [agent("w1:p1", "done")]
        self.daemon.refresh()
        self.now[0] = 2000.0
        again = d.Daemon(herdr.Client(self.fake.path), self.paths, "test", self.cfg, clock=lambda: self.now[0])
        again.refresh()
        self.assertEqual(self.state()["panes"]["w1:p1"]["since"], 1000.0)

    def test_state_from_another_socket_is_discarded(self):
        self.world.agents = [agent("w1:p1", "done")]
        self.daemon.refresh()
        other = d.Daemon(herdr.Client("/elsewhere.sock"), self.paths, "test", self.cfg)
        self.assertEqual(other.state, {})

    def test_unexpected_errors_do_not_kill_the_loop(self):
        calls = []
        stop = {"flag": False}

        def flaky():
            calls.append(1)
            if len(calls) == 2:
                stop["flag"] = True
            raise KeyError("snapshot")
        self.daemon.step = flaky
        d.time.sleep, real = (lambda s: None), d.time.sleep
        try:
            self.daemon.run_forever(stop)
        finally:
            d.time.sleep = real
        self.assertEqual(len(calls), 2)

    def test_dead_herdr_raises_for_retry(self):
        dead = d.Daemon(herdr.Client("/nonexistent/herdr.sock"), self.paths, "test", self.cfg)
        with self.assertRaises(OSError):
            dead.step()

    def test_lock_is_exclusive(self):
        first = d.acquire_lock(self.paths.lock)
        self.assertIsNotNone(first)
        self.assertIsNone(d.acquire_lock(self.paths.lock))
        with open(self.paths.lock) as fh:
            self.assertEqual(fh.read(), str(os.getpid()))   # the failed attempt didn't wipe it
        first.close()
        again = d.acquire_lock(self.paths.lock)
        self.assertIsNotNone(again)
        again.close()


class ConfigTests(unittest.TestCase):
    def test_defaults_and_merge(self):
        path = os.path.join(tempfile.mkdtemp(), "config.toml")
        self.assertEqual(config.load(path)["poll_interval"], 2.0)
        with open(path, "w") as fh:
            fh.write('poll_interval = 1.5\n[summaries]\nllm = true\n')
        cfg = config.load(path)
        self.assertEqual((cfg["poll_interval"], cfg["summaries"]["llm"], cfg["summaries"]["model"]),
                         (1.5, True, "haiku"))

    def test_session_name(self):
        self.assertEqual(herdr.session_name("/h/.config/herdr/herdr.sock"), "default")
        self.assertEqual(herdr.session_name("/h/.config/herdr/sessions/dev/herdr.sock"), "dev")


if __name__ == "__main__":
    unittest.main()
