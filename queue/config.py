"""Config and state paths.

Config: $HERDR_PLUGIN_CONFIG_DIR/config.toml when running as a herdr plugin,
else $XDG_CONFIG_HOME/leader/config.toml. State lives per herdr session
under $XDG_STATE_HOME/leader/<session>.
"""

import copy
import os
import tomllib

DEFAULTS = {
    "session": None,           # herdr session name; None = the one herdr injected
    "poll_interval": 2.0,      # seconds; a missed event costs at most this much
    "stale_after": 10.0,       # seconds of idle screen before a busy pane is shown as interrupted
    "tail_lines": 80,
    "summaries": {"llm": False, "model": "haiku", "max_calls_per_min": 6, "timeout": 20},
    "redaction_patterns": [],
}


def _xdg(var, fallback):
    return os.environ.get(var) or os.path.expanduser(fallback)


def config_path(environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("HERDR_PLUGIN_CONFIG_DIR"):
        return os.path.join(environ["HERDR_PLUGIN_CONFIG_DIR"], "config.toml")
    return os.path.join(environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "leader", "config.toml")


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path=None):
    path = path or config_path()
    try:
        with open(path, "rb") as fh:
            return _merge(DEFAULTS, tomllib.load(fh))
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)


def state_dir(session, environ=None):
    # Deliberately not $HERDR_PLUGIN_STATE_DIR: a daemon started from a shell and
    # an overlay started by herdr must find the same state.json and lock.
    environ = os.environ if environ is None else environ
    base = os.path.join(environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "leader")
    path = os.path.join(base, session)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path
