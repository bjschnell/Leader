import contextlib
import glob
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import install_hooks as ih  # noqa: E402

CMD = ih.quote_command(ih.HOOK_SCRIPT)

EXISTING = {
    "model": "opus",
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}],
    },
}


def ours(settings):
    return [(ev, g.get("matcher")) for ev, groups in settings.get("hooks", {}).items()
            for g in groups for h in g.get("hooks", []) if h.get("command") == CMD]


class MergeTests(unittest.TestCase):
    def test_install_adds_every_event_and_keeps_existing(self):
        out = ih.add_ours(EXISTING, CMD)
        self.assertEqual(sorted(ours(out)), sorted(ih.EVENTS.items()))
        self.assertEqual(out["model"], "opus")
        self.assertIn({"hooks": [{"type": "command", "command": "notify-send done"}]}, out["hooks"]["Stop"])
        self.assertIn({"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]},
                      out["hooks"]["PreToolUse"])

    def test_install_is_idempotent(self):
        once = ih.add_ours(EXISTING, CMD)
        self.assertEqual(ih.add_ours(once, CMD), once)

    def test_uninstall_restores_original(self):
        self.assertEqual(ih.remove_ours(ih.add_ours(EXISTING, CMD), CMD), EXISTING)
        self.assertEqual(ih.remove_ours(ih.add_ours({}, CMD), CMD), {})

    def test_uninstall_keeps_foreign_hook_sharing_a_group(self):
        mixed = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": CMD},
                                               {"type": "command", "command": "other"}]}]}}
        self.assertEqual(ih.remove_ours(mixed, CMD),
                         {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}})

    def test_refuses_malformed_hooks(self):
        with self.assertRaises(ValueError):
            ih.add_ours({"hooks": []}, CMD)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "settings.json")
        with open(self.path, "w") as fh:
            json.dump(EXISTING, fh)

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = ih.main([*args, "--settings", self.path])
        return rc, out.getvalue()

    def read(self):
        with open(self.path) as fh:
            return json.load(fh)

    def backups(self):
        return glob.glob(self.path + ".bak.herdr-queue.*")

    def test_dry_run_writes_nothing(self):
        rc, out = self.run_cli("install", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(self.read(), EXISTING)
        self.assertEqual(self.backups(), [])
        self.assertIn(CMD, json.loads(out)["hooks"]["Stop"][1]["hooks"][0]["command"])

    def test_install_backs_up_then_second_run_is_noop(self):
        self.assertEqual(self.run_cli("install")[0], 0)
        self.assertEqual(len(self.backups()), 1)
        with open(self.backups()[0]) as fh:
            self.assertEqual(json.load(fh), EXISTING)
        installed = self.read()
        rc, out = self.run_cli("install")
        self.assertIn("nothing to do", out)
        self.assertEqual(self.read(), installed)
        self.assertEqual(len(self.backups()), 1)

    def test_uninstall(self):
        self.run_cli("install")
        self.run_cli("uninstall")
        self.assertEqual(self.read(), EXISTING)

    def test_missing_file_is_created(self):
        os.unlink(self.path)
        self.assertEqual(self.run_cli("install")[0], 0)
        self.assertEqual(len(ours(self.read())), len(ih.EVENTS))

    def test_invalid_json_is_left_alone(self):
        with open(self.path, "w") as fh:
            fh.write("{broken")
        rc, _ = self.run_cli("install")
        self.assertEqual(rc, 1)
        with open(self.path) as fh:
            self.assertEqual(fh.read(), "{broken")


if __name__ == "__main__":
    unittest.main()
