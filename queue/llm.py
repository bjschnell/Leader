"""Optional LLM summaries (M5). Off unless config `summaries.llm = true`.

- Called only on a transition into BLOCKED/DONE (one call per pane,
  state_change_seq and category), never on a timer.
- Goes through the user's existing Claude Code install (`claude -p`, so the same
  Anthropic or Bedrock auth the agents already use). No other network path.
  With llm off, nothing here runs (AC4).
- Pane text is redacted before it leaves the machine. The model gets no tools,
  so a hostile pane tail can at worst change a one-line summary.
- Rate-capped, time-limited; on any failure the heuristic summary stays.
"""

import collections
import os
import re
import subprocess
import tempfile
import threading
import time

import summarize

PROMPT = ("In <=15 words: what does this agent need from the human, or what did it finish? "
          "Pane tail follows.")
BASE_ARGS = ["-p", "--tools", "", "--no-session-persistence", "--disable-slash-commands",
             "--strict-mcp-config"]
MAX_INPUT_CHARS = 12_000
MAX_LEN = 100

REDACTIONS = [
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]"),
    (r"\b(AKIA|ASIA)[0-9A-Z]{16}\b", "[REDACTED AWS KEY]"),
    (r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]"),
    (r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+", "[REDACTED JWT]"),
    (r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,})",
     "[REDACTED TOKEN]"),
    (r"(?i)\b([A-Z0-9_]*(SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIALS?)[A-Z0-9_]*)"
     r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)", r"\1\3[REDACTED]"),
    (r"(?i)(://[^/\s:@]+:)[^@\s]+@", r"\1[REDACTED]@"),
]


def redact(text, extra_patterns=()):
    for pattern, repl in REDACTIONS:
        text = re.sub(pattern, repl, text, flags=re.DOTALL)
    for pattern in extra_patterns or ():
        try:
            text = re.sub(pattern, "[REDACTED]", text)
        except re.error:
            pass
    return text


def clean_output(out, limit=MAX_LEN):
    for line in (out or "").splitlines():
        line = re.sub(r"[\x00-\x1f\x7f]", "", line)
        line = re.sub(r"(\*\*|__|`)", "", line).strip().strip("*_\"'").strip()
        line = re.sub(r"^(summary|answer|status|result)\s*:\s*", "", line, flags=re.IGNORECASE)
        if line:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return None


def child_env(environ, extra=None):
    # No HERDR_* so our own Claude hook stays silent for this call.
    env = {k: v for k, v in environ.items() if not k.startswith(("HERDR_", "LEADER_"))}
    env.update(extra or {})
    return env


def build_command(scfg):
    return [scfg.get("command") or "claude", *BASE_ARGS, "--model", scfg.get("model") or "haiku",
            *list(scfg.get("extra_args") or []), PROMPT]


def run_claude(cmd, text, timeout, env):
    proc = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=timeout, env=env,
                          cwd=tempfile.gettempdir())
    return proc.stdout if proc.returncode == 0 else None


class LLMSummarizer:
    def __init__(self, cfg, runner=run_claude, clock=time.monotonic, environ=None):
        scfg = cfg.get("summaries") or {}
        self.enabled = bool(scfg.get("llm"))
        self.cmd = build_command(scfg)
        self.timeout = float(scfg.get("timeout") or 20)
        self.max_per_min = int(scfg.get("max_calls_per_min") or 6)
        self.env = child_env(os.environ if environ is None else environ, scfg.get("env"))
        self.patterns = cfg.get("redaction_patterns") or []
        self.runner = runner
        self.clock = clock
        self.results = {}
        self.pending = set()
        self.calls = collections.deque()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            return self.results.get(key)

    def seed(self, key, text):
        with self.lock:
            self.results.setdefault(key, text)

    def busy(self):
        with self.lock:
            return bool(self.pending)

    def prune(self, live_keys):
        with self.lock:
            self.results = {k: v for k, v in self.results.items() if k in live_keys}

    def request(self, key, category, read_tail, hint=None, sync=False):
        """Start one call for this transition, unless off, done, in flight or over the rate cap."""
        if not self.enabled:
            return
        with self.lock:
            if key in self.results or key in self.pending:
                return
            now = self.clock()
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) >= self.max_per_min:
                self.results[key] = None  # rate-limited: the heuristic stays for this transition
                return
            self.calls.append(now)
            self.pending.add(key)
        args = (key, category, read_tail, hint)
        if sync:
            self._run(*args)
        else:
            threading.Thread(target=self._run, args=args, daemon=True).start()

    def _run(self, key, category, read_tail, hint):
        text = None
        try:
            raw = read_tail() or ""
            tail = summarize.last_turn(raw) or raw
            parts = [f"Agent state: {category}."]
            if hint:
                parts.append(f"Hook note: {hint}")
            parts.append(tail[-MAX_INPUT_CHARS:])
            out = self.runner(self.cmd, redact("\n".join(parts), self.patterns), self.timeout, self.env)
            text = clean_output(out)
        except Exception:
            text = None
        with self.lock:
            self.results[key] = text
            self.pending.discard(key)
