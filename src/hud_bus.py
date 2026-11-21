import os
import re
import threading
import time
from collections import deque

_lock = threading.Lock()
_events = deque(maxlen=60)
_seq = 0
_tracking = {}


def friendly_name(raw):
    """Readable label for an action name, including custom 'launch:' / 'hotkey:' actions."""
    s = str(raw or "").strip()
    low = s.lower()
    if low.startswith("launch:"):
        target = s[len("launch:"):].strip().strip('"')
        if re.match(r"^(https?://|www\.)", target, re.IGNORECASE):
            host = re.sub(r"^(https?://)?(www\.)?", "", target, flags=re.IGNORECASE).split("/")[0]
            return f"Open {host}"
        base = os.path.splitext(os.path.basename(target.replace("\\", "/")))[0]
        return f"Launch {base or target}"
    if low.startswith("hotkey:"):
        return "Hotkey " + s[len("hotkey:"):].strip().upper()
    return s.replace("_", " ").title()


def push_event(source, action, status, detail=""):
    """Records one command. status is 'ok', 'fail' or 'blocked'."""
    global _seq
    with _lock:
        _seq += 1
        _events.append({
            "seq": _seq,
            "t": time.time(),
            "source": str(source or "").title(),
            "action": str(action or ""),
            "name": friendly_name(action),
            "status": status if status in ("ok", "fail", "blocked") else "ok",
            "detail": str(detail or ""),
        })


def get_events(limit=8):
    """Newest first."""
    with _lock:
        items = list(_events)[-limit:]
    items.reverse()
    return items


def set_tracking(**fields):
    with _lock:
        _tracking.update(fields)


def get_tracking():
    with _lock:
        return dict(_tracking)


def clear():
    global _seq
    with _lock:
        _events.clear()
        _tracking.clear()
        _seq = 0