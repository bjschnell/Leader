"""One-line "what does it need / what did it finish" summaries (v1: heuristics, no LLM).

Sources, best first:
  BLOCKED: the hook's message (leader_msg) > last question-like line in the pane tail
           (with the pending command, if visible) > last meaningful line.
  DONE:    the hook's pick from the final assistant message (leader_last) >
           the same pick over the last assistant block in the pane tail.
Pane tails come from `pane.read recent_unwrapped`; Claude Code's UI chrome
(logo, rules, prompt box, status lines, dialog options) is stripped first.
Also imported by hooks/claude_hook.py, so keep it stdlib-only and fast to import.
"""

import re

MAX_LEN = 100

_RULE = re.compile(r"^[\s─━═╌┄╍—_\-│┃╭╮╰╯▐▛▜▝▘▗▖█▀▄▌]+$")
_CHROME = re.compile(
    r"Transcript saving|\bctx:\[|\b5h:\[|\bwk:\[|mode on\b|shift\+tab|Esc to (cancel|interrupt)|"
    r"Enter to (select|confirm)|Tab to amend|^\s*\[OMC|Claude Code v\d|^\s*Tip:|^\s*⚠|"
    r"^\s*(Opus|Sonnet|Haiku|Fable) \d|\? for shortcuts|^\s*session:\d"
)
_SPINNER = re.compile(r"^\s*[✻✶✳✢✽·*]\s+\S+(\s+\S+)?\s+(for\s+\d|…|\.\.\.)")
_PROMPT = re.compile(r"^\s*[❯>]\s")
_OPTION = re.compile(r"^\s*(❯\s*)?\d+\.\s")
_LIST = re.compile(r"^\s*(\d+[.)]|[-*•+]|\[[ xX]\])\s+")
_CONCLUSION = re.compile(
    r"^(done|all\b|fixed|added|implemented|updated|created|completed|finished|shipped|"
    r"summary|result|tests?\b|build\b|i('ve| have)|the (fix|change|migration|refactor)|"
    r"(it|this) (now|is)|no (changes|issues|errors))", re.IGNORECASE)
_MD = re.compile(r"(\*\*|__|`)")


def clean(line):
    line = _MD.sub("", line).strip()
    line = re.sub(r"^#+\s*", "", line)
    return " ".join(line.split())


def truncate(text, limit=MAX_LEN):
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _paragraph_lines(text):
    """Join hard-wrapped continuation lines (Claude indents them by two spaces)."""
    out = []
    for raw in text.splitlines():
        if not raw.strip():
            out.append("")
            continue
        is_cont = raw.startswith("  ") and not _LIST.match(raw) and out and out[-1] and not _LIST.match(out[-1])
        if is_cont:
            out[-1] = out[-1].rstrip() + " " + raw.strip()
        else:
            out.append(raw.rstrip())
    return out


def pick_summary_line(text, limit=MAX_LEN):
    """Choose the most summary-like line of an assistant message."""
    if not isinstance(text, str):
        return None
    lines = [clean(l) for l in _paragraph_lines(text)]
    lines = [l for l in lines if l]
    if not lines:
        return None
    prose = [l for l in lines if not _LIST.match(l)]
    for line in reversed(prose):
        if _CONCLUSION.match(line):
            return truncate(line, limit)
    if prose and len(prose) < len(lines):
        return truncate(prose[0], limit)  # list-heavy answer: its intro says what it is
    return truncate(lines[-1], limit)


def strip_chrome(tail):
    kept = []
    for raw in tail.splitlines():
        s = raw.strip()
        if not s or s in ("❯", ">") or _RULE.match(s) or _CHROME.search(raw) or _SPINNER.match(raw):
            kept.append("")
            continue
        kept.append(raw.rstrip())
    return kept


def last_assistant_block(tail):
    """Text of the last `●` block, minus tool-call lines (⎿) and chrome."""
    lines = strip_chrome(tail)
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("●"):
            start = i
    if start is None:
        # The block's first line scrolled out of the tail: take what follows the
        # last visible user prompt, or everything.
        start = 0
        for i, line in enumerate(lines):
            if _PROMPT.match(line) and line.strip() not in ("❯", ">"):
                start = i + 1
    block = []
    for line in lines[start:]:
        if _PROMPT.match(line) and any(b.strip() for b in block):
            break
        if line.lstrip().startswith("⎿"):
            continue
        block.append(line.replace("●", " ", 1) if not block else line)
    text = "\n".join(block).strip()
    return text or None


def blocked_from_tail(tail, limit=MAX_LEN):
    lines = strip_chrome(tail)
    meaningful = [l for l in lines if l.strip() and not _OPTION.match(l)]
    question_idx = None
    for i in range(len(meaningful) - 1, -1, -1):
        if clean(meaningful[i]).endswith("?"):
            question_idx = i
            break
    if question_idx is None:
        return truncate(clean(meaningful[-1]), limit) if meaningful else None
    question = clean(meaningful[question_idx])
    command = None
    for line in reversed(meaningful[:question_idx]):
        c = clean(line)
        m = re.match(r"^⎿\s*\$\s*(.+)", c)
        if m:
            command = m.group(1)
            break
    return truncate(f"{question} — {command}" if command else question, limit)


def summarize(category, tokens, read_tail=None, limit=MAX_LEN):
    """category: model category; tokens: pane tokens; read_tail: () -> str, called only if needed."""
    tokens = tokens or {}
    if category == "blocked":
        if tokens.get("leader_msg"):
            return truncate(tokens["leader_msg"], limit)
        tail = _safe(read_tail)
        return blocked_from_tail(tail, limit) if tail else None
    if category in ("done", "idle"):
        if tokens.get("leader_last"):
            return truncate(tokens["leader_last"], limit)
        tail = _safe(read_tail)
        block = last_assistant_block(tail) if tail else None
        return pick_summary_line(block, limit) if block else None
    return None


def _safe(fn):
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None
