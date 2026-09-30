"""Dependency-free .env loader.

Reads KEY=VALUE lines from a .env file next to this module (or the path
given) and exports them into os.environ. Values already present in the
environment always win, so real env vars override .env entries.

Supported syntax (one entry per line):
    # comment
    KEY=value
    KEY="quoted value"     # quotes are stripped
    KEY='single quoted'
Blank lines and lines without '=' are ignored.
"""
import os

_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')


def _parse_value(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        return raw[1:-1]
    return raw


def load_env(path=None):
    """Load .env into os.environ. Returns the number of variables set."""
    path = path or _ENV_FILE
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            lines = fh.readlines()
    except OSError:
        return 0

    loaded = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, raw = line.partition('=')
        key = key.strip()
        if not key or not key.replace('_', '').isalnum():
            continue
        value = _parse_value(raw)
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def env_flag(name, default=False):
    """Read a boolean flag from the environment.

    Accepts 1/true/yes/on (case-insensitive) as true, 0/false/no/off as
    false; anything else falls back to the default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')