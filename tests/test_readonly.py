"""AC2: no component may send input to a pane (grep-enforced)."""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCANNED_DIRS = ["hooks", "queue"]
SCANNED_FILES = ["herdr-plugin.toml"]
FORBIDDEN = re.compile(
    r"send[_-]?input|send[_-]?text|send[_-]?keys|pane\.run\b|pane\s+run\b|agent[._\s]+prompt|agent[._\s]+start",
    re.IGNORECASE,
)


def source_files():
    for d in SCANNED_DIRS:
        for dirpath, _, names in os.walk(os.path.join(ROOT, d)):
            for name in names:
                if name.endswith((".py", ".sh", ".toml")):
                    yield os.path.join(dirpath, name)
    for f in SCANNED_FILES:
        path = os.path.join(ROOT, f)
        if os.path.exists(path):
            yield path


class ReadOnlyTests(unittest.TestCase):
    def test_no_input_methods(self):
        hits = []
        for path in source_files():
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if FORBIDDEN.search(line):
                        hits.append(f"{os.path.relpath(path, ROOT)}:{n}: {line.strip()}")
        self.assertEqual(hits, [], "input-sending calls found:\n" + "\n".join(hits))

    def test_no_listening_sockets_in_code(self):
        """AC1 (static half): nothing binds or listens."""
        pattern = re.compile(r"\.(bind|listen)\(|socketserver|http\.server|asyncio\.start_server")
        hits = []
        for path in source_files():
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if pattern.search(line):
                        hits.append(f"{os.path.relpath(path, ROOT)}:{n}: {line.strip()}")
        self.assertEqual(hits, [], "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
