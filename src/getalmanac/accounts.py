"""Carry on with a second Claude login when the first is out of usage.

Only a login that is already valid is used: nothing here refreshes a token or writes a
credentials file. Tokens are read for one request and never stored or logged; the cooldown
table is keyed by a hash of the token and lives in memory.
"""
import datetime
import hashlib
import json
import math
import os
import re
import sys
import threading
import time

_lock = threading.Lock()
_until = {}  # sha256(token) -> when it may be tried again
_told = [False]


def _key(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def token(folder):
    """The access token in `<folder>/.credentials.json` if it stays valid for another minute, else None."""
    try:
        path = os.path.join(os.path.expanduser(str(folder)), ".credentials.json")
        with open(path, encoding="utf-8") as handle:
            oauth = json.load(handle)["claudeAiOauth"]
        tok = oauth["accessToken"]
        if isinstance(tok, str) and tok and float(oauth["expiresAt"]) / 1000 > time.time() + 60:
            return tok
    except Exception:
        pass
    return None


def is_limit(status, body: bytes, header=None) -> bool:
    """A 429 that says the plan's usage is spent, not a plain rate limit.

    `header(name)` reads a reply header: a unified status of `rejected` counts on its own."""
    if status != 429:
        return False
    try:
        if str(header("anthropic-ratelimit-unified-status") or "").strip().lower() == "rejected":
            return True
    except Exception:
        pass
    text = bytes(body or b"")[:2000].decode("utf-8", "replace").lower()
    return "usage_limit" in text or "usage limit" in text or ("hit your" in text and "limit" in text)


RESETS = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b(?:[^()]{0,20}\(([^)]{1,40})\))?", re.I)


def _clock(body: bytes, now: float):
    """The next time the clock reading in 'resets 2:30am (Zone)' occurs, or None."""
    found = RESETS.search(bytes(body or b"")[:2000].decode("utf-8", "replace"))
    if not found:
        return None
    hour, minute, ampm, zone = int(found[1]), int(found[2] or 0), found[3].lower(), found[4]
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if ampm == "p" else 0)
    tz = None
    if zone:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(zone.strip())
        except Exception:  # no zone data or an odd name: read the clock as local time
            pass
    here = datetime.datetime.fromtimestamp(now, tz)
    due = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= here:
        due += datetime.timedelta(days=1)
    return due.timestamp()


def reset_at(cfg, header, body: bytes, now=None) -> float:
    """When this login's usage comes back. `header(name)` reads a reply header.

    Order: anthropic-ratelimit-unified-reset (unix seconds), retry-after (seconds), the
    'resets 2:30am' wording of the body, then accounts.cooldown_minutes. Kept between 1 minute and 7 days."""
    now = time.time() if now is None else now
    at = None
    for name_, convert in (("anthropic-ratelimit-unified-reset", float), ("retry-after", lambda v: now + float(v))):
        try:
            at = convert(str(header(name_)).strip())
            if not math.isfinite(at):
                at = None
                continue
            break
        except Exception:
            pass
    if at is None:
        try:
            at = _clock(body, now)
        except Exception:
            at = None
    if at is None:
        at = now + float(cfg["accounts"]["cooldown_minutes"]) * 60
    return min(max(at, now + 60), now + 7 * 86400)


def mark(cfg, tok: str, until=None):
    """Remember that this login is out of usage until `until` (default: `accounts.cooldown_minutes` from now)."""
    with _lock:
        _until[_key(tok)] = until or time.time() + float(cfg["accounts"]["cooldown_minutes"]) * 60


def cooling_until(tok: str):
    with _lock:
        until = _until.get(_key(tok), 0)
    return until if until > time.time() else None


def alternative(cfg, current):
    """(folder, token) of the first fallback login that is valid, differs from `current` and is not cooling."""
    for folder in cfg["accounts"]["fallback"]:
        tok = token(folder)
        if tok and tok != current and not cooling_until(tok):
            return folder, tok
    return None


def name(folder) -> str:
    return os.path.basename(os.path.expanduser(str(folder)).rstrip("/\\"))


def folder_of(cfg, tok):
    for folder in cfg["accounts"]["fallback"]:
        if tok and token(folder) == tok:
            return folder
    return None


def announce(cfg, used, folder):
    """Once per process, tell the owner on stderr that requests now go out on another login."""
    with _lock:
        if _told[0]:
            return
        _told[0] = True
    gone = folder_of(cfg, used)
    until = time.strftime("%H:%M", time.localtime(cooling_until(used) or time.time()))
    print(f"almanac: the {name(gone) if gone else 'original'} login is out of usage; "
          f"requests now go out on {name(folder)} until {until}", file=sys.stderr, flush=True)


def reset():
    """Forget every cooldown and the one-time notice (tests)."""
    with _lock:
        _until.clear()
        _told[0] = False


def status_line(cfg, records=()) -> str:
    """For `almanac status`: each folder's token state, and the last switch the log recorded."""
    parts = []
    for folder in cfg["accounts"]["fallback"]:
        parts.append(f"{name(folder)} ({'valid' if token(folder) else 'no valid token'})")
    line = ", ".join(parts)
    for rec in reversed(list(records)):
        acc = rec.get("account")
        if isinstance(acc, dict) and acc.get("switched"):
            back = acc.get("until") or rec.get("ts", 0) + float(cfg["accounts"]["cooldown_minutes"]) * 60
            if back > time.time():
                at = time.strftime("%H:%M", time.localtime(rec.get("ts", 0)))
                line += (f"; in use: {acc['switched']} since {at}, the other login is cooling down "
                         f"until {time.strftime('%H:%M', time.localtime(back))}")
            break
    return line
