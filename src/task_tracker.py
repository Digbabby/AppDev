import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS_PATH = os.path.join(BASE_DIR, "config", "tasks.json")

DEFAULT_TASKS = [
    {"id": "vol_up",      "label": "Raise the volume",                  "match": ["volume_up"],                   "target": 2},
    {"id": "vol_down",    "label": "Lower the volume",                  "match": ["volume_down"],                 "target": 2},
    {"id": "play_pause",  "label": "Play / pause media",                "match": ["media_play_pause"],            "target": 1},
    {"id": "skip_track",  "label": "Skip a track (next or previous)",   "match": ["next_track", "previous_track"], "target": 2},
    {"id": "screenshot",  "label": "Take a screenshot",                 "match": ["trigger_screenshot"],          "target": 1},
    {"id": "voice_launch", "label": "Open an app or site by voice",     "match": ["launch_custom_url", "launch_custom_executable"], "via": "voice", "target": 1},
    {"id": "voice_hotkey", "label": "Fire a window shortcut by voice",  "match": ["hotkey_*"],                    "via": "voice", "target": 1},
    {"id": "gesture_custom", "label": "Run a custom gesture action",    "match": ["launch:*", "hotkey:*"],        "via": "gesture", "target": 1},
    {"id": "new_pose",    "label": "Capture and register a new pose",   "match": ["custom_pose_saved"],           "target": 1},
]

_lock = threading.Lock()
_listeners = []
_tasks = []


def _category(trigger_type):
    t = str(trigger_type or "").lower()
    return "voice" if any(k in t for k in ("voice", "speech", "audio", "mic")) else "gesture"


def _matches(patterns, name):
    for p in patterns:
        p = str(p).lower().strip()
        if p.endswith("*"):
            if name.startswith(p[:-1]):
                return True
        elif name == p:
            return True
    return False


def _fresh_defaults():
    return [dict(t, count=0) for t in DEFAULT_TASKS]


def _save_locked():
    try:
        os.makedirs(os.path.dirname(TASKS_PATH), exist_ok=True)
        tmp = TASKS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tasks": _tasks}, f, indent=4)
        os.replace(tmp, TASKS_PATH)
    except Exception as e:
        print(f"[TASKS] Could not save progress: {e}")


def _load():
    global _tasks
    try:
        with open(TASKS_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f).get("tasks", [])
        _tasks = [t for t in loaded if isinstance(t, dict) and t.get("id") and t.get("label") and t.get("match")]
        for t in _tasks:
            t["target"] = max(1, int(t.get("target", 1)))
            t["count"] = max(0, int(t.get("count", 0)))
    except Exception:
        _tasks = []
    if not _tasks:
        _tasks = _fresh_defaults()
        _save_locked()


def _notify():
    for cb in list(_listeners):
        try:
            cb()
        except Exception as e:
            print(f"[TASKS] Listener error: {e}")


def add_listener(callback):
    """callback() is called (from whichever thread logged the command) whenever progress changes."""
    if callback not in _listeners:
        _listeners.append(callback)


def record(action_name, trigger_type=""):
    """Counts one executed command toward the first unfinished task it matches. Returns True if progress changed."""
    name = str(action_name or "").strip().lower()
    if not name:
        return False
    category = _category(trigger_type)
    changed = False
    with _lock:
        for t in _tasks:
            if t["count"] >= t["target"]:
                continue
            if t.get("via") and t["via"] != category:
                continue
            if _matches(t["match"], name):
                t["count"] += 1
                changed = True
                break
        if changed:
            _save_locked()
    if changed:
        _notify()
    return changed


def get_tasks():
    with _lock:
        return [dict(t) for t in _tasks]


def summary():
    with _lock:
        done = sum(1 for t in _tasks if t["count"] >= t["target"])
        return done, len(_tasks)


def reset():
    global _tasks
    with _lock:
        for t in _tasks:
            t["count"] = 0
        _save_locked()
    _notify()


_load()