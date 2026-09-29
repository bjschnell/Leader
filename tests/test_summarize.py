import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "queue"))
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

import summarize as s  # noqa: E402


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


class PickSummaryLineTests(unittest.TestCase):
    def test_prefers_conclusion(self):
        msg = "I refactored the parser into three passes.\n\nChanges:\n- lexer\n- **parser**\n\nAll 42 tests pass."
        self.assertEqual(s.pick_summary_line(msg), "All 42 tests pass.")

    def test_list_heavy_answer_uses_intro(self):
        msg = "Here are the flaky tests:\n1. test_a\n2. test_b\n3. test_c"
        self.assertEqual(s.pick_summary_line(msg), "Here are the flaky tests:")

    def test_short_answer(self):
        self.assertEqual(s.pick_summary_line("ok"), "ok")
        self.assertIsNone(s.pick_summary_line("  \n"))

    def test_joins_wrapped_paragraph_and_strips_markdown(self):
        msg = "## Result\nThe migration now runs in `3s` instead of\n  40s on the staging dataset."
        self.assertEqual(s.pick_summary_line(msg), "The migration now runs in 3s instead of 40s on the staging dataset.")

    def test_truncates(self):
        self.assertEqual(len(s.pick_summary_line("Done " + "x" * 300)), s.MAX_LEN)


class TailTests(unittest.TestCase):
    """Real Claude Code 2.1.285 pane tails captured from the dev session."""

    def test_blocked_bash_dialog(self):
        self.assertEqual(s.blocked_from_tail(fixture("tail_blocked_bash.txt")),
                         "Do you want to proceed? — touch acc.txt")

    def test_done_wrapped_reply(self):
        block = s.last_assistant_block(fixture("tail_done_wrapped.txt"))
        self.assertTrue(block.startswith("I can't say the parser refactor is complete"))
        self.assertIn("has no commits", block)          # continuation line joined in
        self.assertNotIn("Sautéed", block)               # spinner/footer stripped
        self.assertNotIn("manual mode", block)

    def test_done_list_uses_last_line_when_no_intro_visible(self):
        # The intro scrolled out of the captured tail; only list items remain.
        got = s.summarize("done", {}, lambda: fixture("tail_done_list.txt"))
        self.assertEqual(got, "250. Aphid")

    def test_chrome_only_tail(self):
        tail = "────\n❯ \n────\n  ⏸ manual mode on\n  session:0m | ctx:[----------]4%\n"
        self.assertIsNone(s.last_assistant_block(tail))


class SummarizeTests(unittest.TestCase):
    def test_hook_tokens_win_and_tail_is_not_read(self):
        def boom():
            raise AssertionError("tail read")
        self.assertEqual(s.summarize("blocked", {"leader_msg": "Permission: Bash x"}, boom), "Permission: Bash x")
        self.assertEqual(s.summarize("done", {"leader_last": "All green."}, boom), "All green.")

    def test_tail_fallback_and_failures(self):
        self.assertEqual(s.summarize("blocked", {}, lambda: fixture("tail_blocked_bash.txt")),
                         "Do you want to proceed? — touch acc.txt")
        self.assertIsNone(s.summarize("done", {}, lambda: (_ for _ in ()).throw(OSError("gone"))))
        self.assertIsNone(s.summarize("working", {"leader_last": "x"}, None))


if __name__ == "__main__":
    unittest.main()
