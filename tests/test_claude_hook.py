import io
import json
import os
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_hook as ch  # noqa: E402
from fake_herdr import FakeHerdr  # noqa: E402

HOOK_SH = os.path.join(ROOT, "hooks", "claude-hook.sh")


def ev(name, **kw):
    return dict({"hook_event_name": name, "session_id": "s1", "cwd": "/x"}, **kw)


class PlanTests(unittest.TestCase):
    def state(self, payload):
        action = ch.plan(payload)
        return None if action is None else action.get("state", action["kind"])

    def test_turn_lifecycle(self):
        self.assertEqual(self.state(ev("SessionStart", source="startup")), "idle")
        self.assertEqual(self.state(ev("UserPromptSubmit", prompt="hi")), "working")
        self.assertEqual(self.state(ev("PostToolUse", tool_name="Bash")), "working")
        self.assertEqual(self.state(ev("PostToolUseFailure", tool_name="Bash")), "working")
        self.assertEqual(self.state(ev("Stop", last_assistant_message="done")), "idle")
        self.assertEqual(self.state(ev("StopFailure", error_type="rate_limit")), "idle")
        self.assertEqual(self.state(ev("SessionEnd", reason="other")), "release")

    def test_compact_session_start_is_ignored(self):
        self.assertIsNone(ch.plan(ev("SessionStart", source="compact")))

    def test_blocking_events(self):
        a = ch.plan(ev("PermissionRequest", tool_name="Bash", tool_input={"command": "rm -rf build\nmore"}))
        self.assertEqual(a["state"], "blocked")
        self.assertEqual(a["message"], "Permission: Bash rm -rf build")
        self.assertEqual(a["tokens"][ch.MSG_TOKEN], a["message"])
        self.assertEqual(a["tokens"][ch.KIND_TOKEN], "permission")

        a = ch.plan(ev("Notification", notification_type="permission_prompt",
                       message="Claude needs your permission to use Bash"))
        self.assertEqual((a["state"], a["message"]), ("blocked", "Claude needs your permission to use Bash"))

        self.assertEqual(a["tokens"], {})  # keeps PermissionRequest's specific text
        a = ch.plan(ev("Notification", notification_type="elicitation_dialog", message="MCP wants input"))
        self.assertEqual(a["tokens"], {ch.MSG_TOKEN: "MCP wants input", ch.KIND_TOKEN: "input"})

        q = {"questions": [{"question": "Which DB?", "options": []}]}
        a = ch.plan(ev("PermissionRequest", tool_name="AskUserQuestion", tool_input=q))
        self.assertEqual((a["state"], a["message"]), ("blocked", "Which DB?"))
        self.assertEqual(a["tokens"][ch.KIND_TOKEN], "question")

        a = ch.plan(ev("PreToolUse", tool_name="AskUserQuestion",
                       tool_input={"questions": [{"question": "Which DB?", "options": []}]}))
        self.assertEqual((a["state"], a["message"]), ("blocked", "Which DB?"))

    def test_ignored_events(self):
        self.assertIsNone(ch.plan(ev("Notification", notification_type="idle_prompt", message="waiting")))
        self.assertIsNone(ch.plan(ev("Notification", notification_type="auth_success")))
        self.assertIsNone(ch.plan(ev("PreToolUse", tool_name="Bash")))
        self.assertIsNone(ch.plan(ev("SubagentStop")))
        self.assertIsNone(ch.plan(ev("Stop", agent_id="sub-1")))  # subagent context
        self.assertIsNone(ch.plan(ev("PostToolUse", tool_name="Bash", agent_id="sub-1")))
        self.assertIsNone(ch.plan(ev("SomeFutureEvent")))
        self.assertIsNone(ch.plan("not a dict"))

    def test_working_clears_message_and_stop_keeps_last_line(self):
        self.assertEqual(ch.plan(ev("UserPromptSubmit"))["tokens"], {ch.MSG_TOKEN: None, ch.KIND_TOKEN: None})
        a = ch.plan(ev("Stop", last_assistant_message="Line one\n\nAll tests pass.\n"))
        self.assertEqual(a["tokens"], {ch.MSG_TOKEN: None, ch.KIND_TOKEN: None, ch.LAST_TOKEN: "All tests pass."})

    def test_one_line_truncates(self):
        self.assertEqual(len(ch.one_line("x" * 500)), ch.MAX_TEXT)
        self.assertIsNone(ch.one_line("  \n "))


class RequestTests(unittest.TestCase):
    def test_report_requests(self):
        reqs = ch.requests_for(ch.plan(ev("PermissionRequest", tool_name="Edit",
                                          tool_input={"file_path": "a.py"})), "w1:p2", 42)
        self.assertEqual([m for m, _ in reqs], ["pane.report_agent", "pane.report_metadata"])
        report = reqs[0][1]
        self.assertEqual(report, {"pane_id": "w1:p2", "source": ch.SOURCE, "agent": "claude",
                                  "state": "blocked", "seq": 42, "message": "Permission: Edit a.py"})
        self.assertEqual(reqs[1][1]["tokens"], {ch.MSG_TOKEN: "Permission: Edit a.py", ch.KIND_TOKEN: "permission"})

    def test_release_requests(self):
        reqs = ch.requests_for({"kind": "release"}, "w1:p2", 7)
        self.assertEqual(reqs[0], ("pane.release_agent",
                                   {"pane_id": "w1:p2", "source": ch.SOURCE, "agent": "claude", "seq": 7}))


class MainTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHerdr()
        self.env = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p3", "HERDR_SOCKET_PATH": self.fake.path}

    def tearDown(self):
        self.fake.close()

    def test_sends_to_socket(self):
        ch.main(io.StringIO(json.dumps(ev("UserPromptSubmit"))), self.env)
        methods = [r["method"] for r in self.fake.requests]
        self.assertEqual(methods, ["pane.report_agent", "pane.report_metadata"])
        self.assertEqual(self.fake.requests[0]["params"]["state"], "working")

    def test_seq_is_monotonic(self):
        for name in ("UserPromptSubmit", "Stop"):
            ch.main(io.StringIO(json.dumps(ev(name))), self.env)
        seqs = [r["params"]["seq"] for r in self.fake.requests if r["method"] == "pane.report_agent"]
        self.assertLess(seqs[0], seqs[1])

    def test_noop_outside_herdr(self):
        for env in ({}, {"HERDR_ENV": "1"}, {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1"}):
            self.assertEqual(ch.main(io.StringIO(json.dumps(ev("Stop"))), env), 0)
        self.assertEqual(self.fake.requests, [])

    def test_bad_input_and_dead_socket_are_silent(self):
        self.assertEqual(ch.main(io.StringIO("{not json"), self.env), 0)
        env = dict(self.env, HERDR_SOCKET_PATH="/nonexistent/herdr.sock")
        self.assertEqual(ch.main(io.StringIO(json.dumps(ev("Stop"))), env), 0)


class ShellWrapperTests(unittest.TestCase):
    """AC3: exit 0 and no output, inside and outside herdr; low overhead."""

    def run_hook(self, env, payload):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        clean.update(env)
        t0 = time.perf_counter()
        proc = subprocess.run([HOOK_SH], input=json.dumps(payload), env=clean,
                              capture_output=True, text=True, timeout=10)
        return proc, time.perf_counter() - t0

    def test_outside_herdr(self):
        proc, _ = self.run_hook({}, ev("Stop"))
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_inside_herdr_silent_and_fast(self):
        fake = FakeHerdr()
        try:
            env = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_SOCKET_PATH": fake.path}
            timings = []
            for _ in range(5):
                proc, dt = self.run_hook(env, ev("UserPromptSubmit"))
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
                timings.append(dt)
            self.assertEqual(len(fake.requests), 10)
            median = sorted(timings)[2]
            self.assertLess(median, 0.050, f"hook median {median*1000:.1f} ms")
        finally:
            fake.close()

    def test_dead_socket_is_silent(self):
        proc, _ = self.run_hook({"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1",
                                 "HERDR_SOCKET_PATH": "/nonexistent.sock"}, ev("Stop"))
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
