import csv
import glob
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk

DB_ATTR_NAMES = ("DB_PATH", "DB_FILE", "DB_NAME", "DATABASE", "DATABASE_PATH",
                 "db_path", "db_file", "db_name")
DB_PATTERNS = ("*.db", "*.sqlite", "*.sqlite3")

EXCLUDED_TABLES = {"user_profile"}
TABLE_LABELS = {"command_logs": "Command history", "session_logs": "Session history"}
TABLE_HINTS = ("history", "log", "command", "event", "action", "activity")
TIME_HINTS = ("timestamp", "datetime", "time", "date", "created", "ts")

ALL_TRIGGERS = "All triggers"
ALL_RESULTS = "All results"
RESULT_OPTIONS = [ALL_RESULTS, "Verified", "Unverified", "Failed"]
# Result of a command: Verified = a check saw its effect, Unverified = it ran but its effect can't be observed
# (mouse clicks, scrolling...), Failed = blocked, no effect seen, or an error. See action_verifier.py.
RESULT_NAMES = {"confirmed": "Verified", "unverified": "Unverified", "failed": "Failed"}


def _result_sql(columns):
    """SQL for the result of a command_logs row: 'confirmed', 'unverified' or 'failed'.
    Rows logged before verification existed count as unverified (or failed)."""
    status = "COALESCE(status, 'success')" if "status" in columns else "'success'"
    confirmed = "verification = 'confirmed'" if "verification" in columns else "0"
    return f"(CASE WHEN {status} = 'failed' THEN 'failed' WHEN {confirmed} THEN 'confirmed' ELSE 'unverified' END)"
SYSTEM_EVENT_ACTIONS = ("wake_gate",)      # internal events: hidden by default, never counted as tasks
_SYSTEM_SQL = ",".join(f"'{a}'" for a in SYSTEM_EVENT_ACTIONS)
GROUP_GAP_SECONDS = 30

# What each action does (taken from action_dispatcher.py): action_name -> (category, description)
ACTION_INFO = {
    "wake_gate": ("System", "Locks or unlocks the gesture engine (hold your index finger up). While locked, gesture commands are ignored; voice commands and the fist play/pause still work."),
    "volume_up": ("Media", "Presses the Volume Up key - raises the system volume one step."),
    "volume_down": ("Media", "Presses the Volume Down key - lowers the system volume one step."),
    "media_play_pause": ("Media", "Presses the Play/Pause media key - pauses or resumes whatever app is playing media."),
    "previous_track": ("Media", "Sends the Windows 'previous track' key. The app currently controlling media (Spotify, a browser video...) responds, usually going back to the previous song. A short cooldown prevents accidental repeats."),
    "next_track": ("Media", "Sends the Windows 'next track' key. The app currently controlling media (Spotify, a browser video...) skips to the next song. A short cooldown prevents accidental repeats."),
    "maximize": ("Window", "Win + Up - maximizes the active window."),
    "minimize": ("Window", "Win + D - shows the desktop by minimizing every open window (press again to restore them)."),
    "minimize_all_windows": ("Window", "Win + D - shows the desktop by minimizing every open window."),
    "cycle_active_applications": ("Window", "Alt + Tab - switches to the next open application."),
    "snap_window_left_side": ("Window", "Win + Left - snaps the active window to the left half of the screen."),
    "snap_window_right_side": ("Window", "Win + Right - snaps the active window to the right half of the screen."),
    "exit": ("Window", "Alt + F4 - closes the active window."),
    "zoom_in": ("View", "Ctrl + Plus - zooms in on the active app (browsers, Office, etc.)."),
    "zoom_out": ("View", "Ctrl + Minus - zooms out on the active app (browsers, Office, etc.)."),
    "toggle_screen_magnifier": ("View", "Win + Plus - opens / zooms the Windows Magnifier."),
    "trigger_screenshot": ("View", "Win + Print Screen - saves a screenshot to Pictures > Screenshots."),
    "toggle_high_contrast_theme": ("View", "Left Alt + Left Shift + Print Screen - toggles the Windows high-contrast theme."),
    "mouse_click_left": ("Mouse", "Left-clicks at the current cursor position."),
    "mouse_click_right": ("Mouse", "Right-clicks at the current cursor position."),
    "mouse_double_click": ("Mouse", "Double-clicks at the current cursor position."),
    "scroll_view_down": ("Mouse", "Scrolls the active view down a few lines."),
    "scroll_view_up": ("Mouse", "Scrolls the active view up a few lines."),
    "next_tab": ("Browser", "Ctrl + Tab - switches to the next browser tab."),
    "prev_tab": ("Browser", "Ctrl + Shift + Tab - switches to the previous browser tab."),
    "new_tab": ("Browser", "Ctrl + T - opens a new browser tab."),
    "trigger_voice_search": ("Browser", "Opens Google in Chrome, then presses Ctrl + Shift + . to start voice search."),
    "jump_timestamp": ("Browser", "Jumps a video to the time you said by editing the page address (t=seconds). For 'N percent' it presses the matching number key instead."),
    "form_next_field": ("Forms", "Tab - moves to the next form field."),
    "form_prev_field": ("Forms", "Shift + Tab - moves to the previous form field."),
    "form_select_all": ("Forms", "Ctrl + A - selects everything in the current field."),
    "form_clear_field": ("Forms", "Ctrl + A, then Backspace - clears the current field."),
    "form_submit": ("Forms", "Enter - submits the form / confirms."),
    "enter": ("Forms", "Presses the Enter key."),
    "edit_undo": ("Editing", "Ctrl + Z - undoes the last change."),
    "edit_redo": ("Editing", "Ctrl + Y - redoes the last undone change."),
    "edit_copy": ("Editing", "Ctrl + C - copies the selection."),
    "edit_paste": ("Editing", "Ctrl + V - pastes the clipboard."),
    "edit_delete_line": ("Editing", "Home, Shift + End, Backspace - deletes the current line's text."),
    "launch_paint": ("Launch", "Opens Microsoft Paint."),
    "launch_calculator": ("Launch", "Opens Calculator."),
    "launch_file_explorer": ("Launch", "Opens File Explorer."),
    "launch_notepad": ("Launch", "Opens Notepad."),
    "launch_chrome": ("Launch", "Opens Google Chrome."),
    "start_microsoft_word": ("Launch", "Starts Microsoft Word."),
    "launch_microsoft_excel": ("Launch", "Starts Microsoft Excel."),
    "launch_microsoft_powerpoint": ("Launch", "Starts Microsoft PowerPoint."),
    "launch_microsoft_outlook": ("Launch", "Starts Microsoft Outlook."),
    "launch_calendar": ("Launch", "Opens the Windows Calendar app."),
    "launch_microsoft_teams": ("Launch", "Opens Microsoft Teams."),
    "launch_microsoft_edge": ("Launch", "Opens Microsoft Edge."),
    "launch_microsoft_onenote": ("Launch", "Opens OneNote."),
    "launch_microsoft_photos": ("Launch", "Opens the Photos app."),
    "launch_microsoft_camera": ("Launch", "Opens the Windows Camera app."),
    "launch_microsoft_paint": ("Launch", "Opens Microsoft Paint."),
    "launch_browser_google": ("Launch", "Opens google.com in your default browser."),
    "launch_youtube": ("Launch", "Opens youtube.com in your default browser."),
    "launch_gemini": ("Launch", "Opens gemini.google.com in your default browser."),
    "launch_google_slides": ("Launch", "Opens Google Slides in your default browser."),
    "launch_google_docs": ("Launch", "Opens Google Docs in your default browser."),
    "launch_google_drive": ("Launch", "Opens Google Drive in your default browser."),
    "launch_custom_executable": ("Launch", "Starts the program you named by voice."),
    "launch_custom_url": ("Launch", "Opens the web address you said in your default browser."),
}


def describe_action(action):
    """(category, description) for any logged action name, including custom ones."""
    action = action or ""
    if action in ACTION_INFO:
        return ACTION_INFO[action]
    if action.startswith("autofill_"):
        return ("Profile", f"Types your saved '{action[len('autofill_'):]}' profile value into the focused field.")
    if action.startswith("hotkey:"):
        return ("Custom", f"Custom hotkey - presses {action.partition(':')[2].strip()}.")
    if action.startswith("launch:"):
        return ("Custom", f"Custom launcher - opens {action.partition(':')[2].strip()}.")
    return ("Other", "No built-in description for this action.")


# --------------------------------------------------------------------------- #
# Trigger mappings (gestures.json / voice_commands.json)
# --------------------------------------------------------------------------- #
def _is_action(value):
    return isinstance(value, str) and (
        value in ACTION_INFO or value.startswith(("launch:", "hotkey:", "autofill_")))


def _voice_pairs(node):
    """Yield (phrase, action) pairs from a voice_commands.json of unknown shape."""
    phrase_keys = ("phrase", "phrases", "trigger", "triggers", "command", "voice", "say", "keyword", "name")
    action_keys = ("action", "action_name", "function", "executes", "target")
    if isinstance(node, dict):
        action = next((node[k] for k in action_keys if isinstance(node.get(k), str)), None)
        phrase = next((node[k] for k in phrase_keys if isinstance(node.get(k), (str, list))), None)
        if action and phrase:
            for p in (phrase if isinstance(phrase, list) else [phrase]):
                if isinstance(p, str):
                    yield p, action
            return
        for k, v in node.items():
            if _is_action(v):                       # {"phrase": "action"}
                yield k, v
            elif _is_action(k) and isinstance(v, (str, list)):   # {"action": ["phrase", ...]}
                for p in (v if isinstance(v, list) else [v]):
                    if isinstance(p, str):
                        yield p, k
            else:
                yield from _voice_pairs(v)
    elif isinstance(node, list):
        for item in node:
            yield from _voice_pairs(item)


def load_trigger_map(base_dir):
    """action -> {'gestures': [...], 'voice': [...]}  (empty if the config files can't be read)."""
    result = {}
    cfg = os.path.join(base_dir, "config")

    def entry(action):
        return result.setdefault(action, {"gestures": [], "voice": []})

    try:
        with open(os.path.join(cfg, "gestures.json"), "r", encoding="utf-8") as f:
            for g in json.load(f).get("gestures", []):
                if g.get("action") and g.get("name"):
                    name = str(g["name"])
                    if name not in entry(g["action"])["gestures"]:
                        entry(g["action"])["gestures"].append(name)
    except Exception:
        pass
    try:
        with open(os.path.join(cfg, "voice_commands.json"), "r", encoding="utf-8") as f:
            for phrase, action in _voice_pairs(json.load(f)):
                if phrase not in entry(action)["voice"]:
                    entry(action)["voice"].append(phrase)
    except Exception:
        pass
    return result


# --------------------------------------------------------------------------- #
# Database helpers (no GUI code)
# --------------------------------------------------------------------------- #
def find_db_path(base_dir):
    """Ask database_manager where its file lives; otherwise search the project folders."""
    candidates = []
    try:
        import database_manager
        for attr in DB_ATTR_NAMES:
            val = getattr(database_manager, attr, None)
            if isinstance(val, (str, os.PathLike)):
                p = os.fspath(val)
                if os.path.isabs(p):
                    candidates.append(p)
                else:
                    candidates.extend([os.path.abspath(p), os.path.join(base_dir, p)])
    except Exception:
        pass
    for p in candidates:
        if os.path.isfile(p):
            return p

    found = []
    for sub in ("config", "", "data", "database", "src"):
        for pattern in DB_PATTERNS:
            found.extend(glob.glob(os.path.join(base_dir, sub, pattern)))
    return max(found, key=os.path.getmtime) if found else None


def open_readonly(path):
    """Fresh read-only connection, so the viewer can never modify or lock the app's data."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=1.0)


def quote_ident(name):
    return '"' + str(name).replace('"', '""') + '"'


def list_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in EXCLUDED_TABLES]


def pick_default_table(tables):
    if "command_logs" in tables:
        return "command_logs"
    for hint in TABLE_HINTS:
        for t in tables:
            if hint in t.lower():
                return t
    return tables[0] if tables else None


def get_columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()]


def find_time_column(columns):
    for hint in TIME_HINTS:
        for c in columns:
            if hint in c.lower():
                return c
    return None


def table_signature(conn, table):
    """Cheap fingerprint - changes when something new is logged (or a session is extended)."""
    q = quote_ident(table)
    if table == "session_logs":
        return tuple(conn.execute("SELECT COUNT(*), MAX(id), MAX(logout_time) FROM session_logs").fetchone())
    try:
        return tuple(conn.execute(f"SELECT COUNT(*), MAX(rowid) FROM {q}").fetchone())
    except sqlite3.OperationalError:  # WITHOUT ROWID table
        return (conn.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0], None)


def distinct_triggers(conn):
    rows = conn.execute(
        "SELECT DISTINCT trigger_type FROM command_logs WHERE trigger_type IS NOT NULL ORDER BY 1"
    ).fetchall()
    return [r[0] for r in rows]


def _like(text):
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def range_clauses(col, date_range):
    """SQL for a (start, end) pair of local days ('YYYY-MM-DD' or None). Timestamps are stored in UTC."""
    clauses, params = [], []
    start, end = date_range if date_range else (None, None)
    if start:
        clauses.append(f"date({col}, 'localtime') >= ?")
        params.append(start)
    if end:
        clauses.append(f"date({col}, 'localtime') <= ?")
        params.append(end)
    return clauses, params


def build_where(table, columns, search, date_range, trigger, result=ALL_RESULTS):
    """WHERE clause for the numbered sub-queries below (window_context / status are always exposed there)."""
    clauses, params = [], []
    time_col = {"command_logs": "timestamp", "session_logs": "login_time"}.get(table)
    if time_col:
        c, p = range_clauses(time_col, date_range)
        clauses += c
        params += p

    if table == "command_logs":
        if trigger and trigger != ALL_TRIGGERS:
            clauses.append("trigger_type = ?")
            params.append(trigger)
        if result == "Success":
            clauses.append("status = 'success'")
        elif result == "Verified":
            clauses.append("result = 'confirmed'")
        elif result == "Unverified":
            clauses.append("result = 'unverified'")
        elif result == "Failed":
            clauses.append("status = 'failed'")
        if search:
            clauses.append(
                "(REPLACE(action_name, '_', ' ') LIKE ? ESCAPE '\\' "
                "OR trigger_type LIKE ? ESCAPE '\\' "
                "OR window_context LIKE ? ESCAPE '\\' "
                "OR status LIKE ? ESCAPE '\\' "
                "OR failure_reason LIKE ? ESCAPE '\\')")
            params += [_like(search.replace("_", " "))] * 3 + [_like(search)] * 2
    elif table == "session_logs":
        if search:
            clauses.append("(datetime(login_time, 'localtime') LIKE ? ESCAPE '\\' "
                           "OR datetime(logout_time, 'localtime') LIKE ? ESCAPE '\\')")
            params += [_like(search)] * 2
    elif search and columns:
        clauses.append("(" + " OR ".join(
            f"CAST({quote_ident(c)} AS TEXT) LIKE ? ESCAPE '\\'" for c in columns) + ")")
        params += [_like(search)] * len(columns)

    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def format_duration(secs):
    if secs is None:
        return ""
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def fetch_view(conn, table, columns, search="", date_range=None, trigger=ALL_TRIGGERS, limit=500,
               result=ALL_RESULTS, show_system=False):
    """
    Returns (headers, rows, ids, matching_count), newest first.
    Column 0 is a stable running number (1 = oldest row in the table), not the raw database id.
    ids[i] is the real database id of rows[i] (used to load the details panel).
    """
    q = quote_ident(table)

    if table == "command_logs":
        ctx = "window_context" if "window_context" in columns else "'Global'"
        status = "COALESCE(status, 'success')" if "status" in columns else "'success'"
        reason = "failure_reason" if "failure_reason" in columns else "NULL"
        base = "" if show_system else f" WHERE action_name NOT IN ({_SYSTEM_SQL})"
        inner = (f"SELECT ROW_NUMBER() OVER (ORDER BY id) AS n, id, timestamp, action_name, trigger_type, "
                 f"{ctx} AS window_context, {status} AS status, {reason} AS failure_reason, "
                 f"{_result_sql(columns)} AS result FROM {q}{base}")
        where, params = build_where(table, columns, search, date_range, trigger, result)
        matching = conn.execute(f"SELECT COUNT(*) FROM ({inner}){where}", params).fetchone()[0]
        cur = conn.execute(
            f"SELECT n, id, datetime(timestamp, 'localtime'), action_name, trigger_type, window_context, result "
            f"FROM ({inner}){where} ORDER BY id DESC LIMIT ?", params + [limit])
        raw = cur.fetchall()
        rows = [(n, t, (a or "").upper().replace("_", " "), RESULT_NAMES.get(res, "Unverified"),
                 trg or "", c or "Global")
                for n, _i, t, a, trg, c, res in raw]
        return ["#", "Time", "Action", "Result", "Trigger", "Context"], rows, [r[1] for r in raw], matching

    if table == "session_logs":
        inner = ("SELECT ROW_NUMBER() OVER (ORDER BY id) AS n, id, login_time, logout_time FROM session_logs")
        where, params = build_where(table, columns, search, date_range, trigger)
        matching = conn.execute(f"SELECT COUNT(*) FROM ({inner}){where}", params).fetchone()[0]
        span = (f"c.timestamp >= login_time AND c.timestamp <= datetime(logout_time, '+2 seconds') "
                f"AND c.action_name NOT IN ({_SYSTEM_SQL})")
        head = ("SELECT n, id, datetime(login_time, 'localtime'), datetime(logout_time, 'localtime'), "
                "MAX(0, CAST(strftime('%s', logout_time) AS INTEGER) - CAST(strftime('%s', login_time) AS INTEGER)), ")
        tail = f" FROM ({inner}){where} ORDER BY id DESC LIMIT ?"
        try:
            raw = conn.execute(
                head + f"(SELECT COUNT(*) FROM command_logs c WHERE {span}), "
                f"(SELECT COUNT(*) FROM command_logs c WHERE {span} AND COALESCE(c.status, 'success') = 'failed')"
                + tail, params + [limit]).fetchall()
        except sqlite3.OperationalError:   # database without command_logs / status column
            raw = conn.execute(head + "0, 0" + tail, params + [limit]).fetchall()
        rows = [(n, a, b, format_duration(d), cmds, bad) for n, _i, a, b, d, cmds, bad in raw]
        return ["#", "Login", "Last activity", "Duration", "Commands", "Failed"], rows, [r[1] for r in raw], matching

    where, params = build_where(table, columns, search, date_range, trigger)
    matching = conn.execute(f"SELECT COUNT(*) FROM {q}{where}", params).fetchone()[0]
    time_col = find_time_column(columns)
    order = f" ORDER BY {quote_ident(time_col)} DESC" if time_col else " ORDER BY rowid DESC"
    try:
        rows = conn.execute(f"SELECT * FROM {q}{where}{order} LIMIT ?", params + [limit]).fetchall()
    except sqlite3.OperationalError:
        if time_col:
            raise
        rows = conn.execute(f"SELECT * FROM {q}{where} LIMIT ?", params + [limit]).fetchall()
    return list(columns), rows, [], matching


def _parse_local(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def group_repeats(rows, ids, gap_seconds=GROUP_GAP_SECONDS):
    """
    Collapse consecutive identical command rows (newest-first) that are <= gap_seconds apart.
    A failed run is never merged with a successful one. Rows are (n, time, action, result, trigger, context).
    Returns (display_rows, display_ids, group_info) where group_info[i] is None or
    {'count', 'first_time', 'last_time'}.
    """
    groups = []
    for row, rid in zip(rows, ids):
        key = (row[2], row[3], row[4], row[5])
        t = _parse_local(row[1])
        g = groups[-1] if groups else None
        if g and g["key"] == key and t and g["last_t"] and (g["last_t"] - t).total_seconds() <= gap_seconds:
            g["rows"].append(row)
            g["last_t"] = t
        else:
            groups.append({"key": key, "rows": [row], "id": rid, "last_t": t})

    out_rows, out_ids, info = [], [], []
    for g in groups:
        newest, oldest = g["rows"][0], g["rows"][-1]
        count = len(g["rows"])
        if count == 1:
            out_rows.append(newest)
            info.append(None)
        else:
            out_rows.append((f"{oldest[0]}-{newest[0]}", newest[1], f"{newest[2]} \u00d7{count}",
                             newest[3], newest[4], newest[5]))
            info.append({"count": count, "first_time": oldest[1], "last_time": newest[1]})
        out_ids.append(g["id"])
    return out_rows, out_ids, info


def format_cell(value, limit=300):
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    text = str(value).replace("\r", " ").replace("\n", " \u21b5 ")
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


# --------------------------------------------------------------------------- #
# Details panel: data + text builders (no GUI code)
# --------------------------------------------------------------------------- #
def _parse_utc(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def relative_time(utc_text, now=None):
    t = _parse_utc(utc_text)
    if not t:
        return ""
    now = now or datetime.now(timezone.utc)
    secs = int((now - t).total_seconds())
    if secs < 5:
        return "just now"
    if secs < 60:
        return f"{secs} seconds ago"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h {(secs % 3600) // 60} min ago"
    days = secs // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


def pretty_local(local_text):
    t = _parse_local(local_text)
    return t.strftime("%a %d %b %Y, %H:%M:%S") if t else (local_text or "")


def fetch_command_details(conn, row_id, columns):
    ctx = "window_context" if "window_context" in columns else "'Global'"
    status = "COALESCE(status, 'success')" if "status" in columns else "'success'"
    reason = "failure_reason" if "failure_reason" in columns else "NULL"
    row = conn.execute(
        f"SELECT id, timestamp, datetime(timestamp, 'localtime'), action_name, trigger_type, {ctx}, {status}, {reason}, "
        f"{_result_sql(columns)} FROM command_logs WHERE id = ?", (row_id,)).fetchone()
    if not row:
        return None
    rid, ts, local_ts, action, trigger, context, st, why, res = row
    one = lambda sql, p=(): conn.execute(sql, p).fetchone()
    d = {"id": rid, "utc": ts, "local": local_ts, "action": action or "",
         "trigger": trigger or "", "context": context or "Global",
         "status": "failed" if st == "failed" else "success", "reason": why or "",
         "verification": res or "unverified"}
    d["total"] = one("SELECT COUNT(*) FROM command_logs WHERE action_name = ?", (action,))[0]
    d["failed_runs"] = one(f"SELECT COUNT(*) FROM command_logs WHERE action_name = ? AND {status} = 'failed'",
                           (action,))[0]
    d["verified_runs"] = one(f"SELECT COUNT(*) FROM command_logs WHERE action_name = ? "
                             f"AND {_result_sql(columns)} = 'confirmed'", (action,))[0]
    d["by_trigger"] = conn.execute(
        "SELECT trigger_type, COUNT(*) FROM command_logs WHERE action_name = ? "
        "GROUP BY trigger_type ORDER BY 2 DESC", (action,)).fetchall()
    d["today"] = one("SELECT COUNT(*) FROM command_logs WHERE action_name = ? "
                     "AND date(timestamp, 'localtime') = date('now', 'localtime')", (action,))[0]
    d["all_commands"] = one(f"SELECT COUNT(*) FROM command_logs WHERE action_name NOT IN ({_SYSTEM_SQL})")[0]
    d["first"] = one("SELECT datetime(MIN(timestamp), 'localtime') FROM command_logs WHERE action_name = ?", (action,))[0]
    d["previous_run"] = one("SELECT timestamp, datetime(timestamp, 'localtime') FROM command_logs "
                            "WHERE action_name = ? AND id < ? ORDER BY id DESC LIMIT 1", (action, rid))
    gap = "ABS(CAST(strftime('%s', timestamp) AS INTEGER) - CAST(strftime('%s', ?) AS INTEGER))"
    d["before"] = conn.execute(
        f"SELECT action_name, trigger_type, {gap} FROM command_logs WHERE id < ? ORDER BY id DESC LIMIT 3",
        (ts, rid)).fetchall()[::-1]
    d["after"] = conn.execute(
        f"SELECT action_name, trigger_type, {gap} FROM command_logs WHERE id > ? ORDER BY id ASC LIMIT 3",
        (ts, rid)).fetchall()
    try:
        d["session"] = one("SELECT id, datetime(login_time, 'localtime') FROM session_logs "
                           "WHERE login_time <= ? ORDER BY id DESC LIMIT 1", (ts,))
    except sqlite3.Error:
        d["session"] = None
    return d


def fetch_session_details(conn, row_id):
    row = conn.execute(
        "SELECT id, login_time, logout_time, datetime(login_time, 'localtime'), datetime(logout_time, 'localtime'), "
        "MAX(0, CAST(strftime('%s', logout_time) AS INTEGER) - CAST(strftime('%s', login_time) AS INTEGER)) "
        "FROM session_logs WHERE id = ?", (row_id,)).fetchone()
    if not row:
        return None
    sid, login, logout, login_l, logout_l, secs = row
    span = (f"timestamp >= ? AND timestamp <= datetime(?, '+2 seconds') AND action_name NOT IN ({_SYSTEM_SQL})")
    count = conn.execute(f"SELECT COUNT(*) FROM command_logs WHERE {span}", (login, logout)).fetchone()[0]
    try:
        failed = conn.execute(
            f"SELECT COUNT(*) FROM command_logs WHERE {span} AND COALESCE(status, 'success') = 'failed'",
            (login, logout)).fetchone()[0]
    except sqlite3.OperationalError:
        failed = 0
    top = conn.execute(
        f"SELECT action_name, COUNT(*) FROM command_logs WHERE {span} GROUP BY action_name ORDER BY 2 DESC LIMIT 5",
        (login, logout)).fetchall()
    return {"id": sid, "login": login_l, "logout": logout_l, "secs": secs, "count": count,
            "failed": failed, "top": top}


def _pretty_action(name):
    return (name or "").upper().replace("_", " ")


def build_command_panel(d, seq=None, group=None, trigger_map=None, now=None):
    """Returns a list of (text, tag) pieces for the details textbox."""
    parts = []
    add = lambda text, tag="v": parts.append((text, tag))
    head = lambda title: add(f"\n{title.upper()}\n", "h")
    kv = lambda k, v: (add(f"{k:<11}", "k"), add(f"{v}\n", "v"))

    action = d["action"]
    add(f"{_pretty_action(action)}\n", "title")
    add(f"Entry #{seq} \u00b7 {d['trigger']} \u00b7 {d['context']}\n" if seq is not None
        else f"{d['trigger']} \u00b7 {d['context']}\n", "sub")

    head("Result")
    failed = d.get("status") == "failed"
    verification = d.get("verification", "unverified")
    add(f"{'Outcome':<11}", "k")
    if failed:
        add("FAILED\n", "bad")
        kv("Reason", d.get("reason") or "No reason was recorded")
    elif verification == "confirmed":
        add("Verified\n", "good")
        add("The system checked afterwards and saw the command take effect.\n", "muted")
    else:
        add("Ran (unverified)\n", "v")
        add("The command was sent without an error, but its effect can't be observed from outside "
            "(for example a click or a scroll), so it is not counted as proven.\n", "muted")

    head("When")
    kv("Time", pretty_local(d["local"]))
    kv("Ago", relative_time(d["utc"], now))
    if group:
        kv("Repeated", f"{group['count']} times, {group['first_time'][11:]} to {group['last_time'][11:]}")
    prev = d["previous_run"]
    if prev:
        t_now, t_prev = _parse_utc(d["utc"]), _parse_utc(prev[0])
        gap = format_duration((t_now - t_prev).total_seconds()) if t_now and t_prev else ""
        kv("Previous", f"{prev[1]} ({gap} earlier)")
    else:
        kv("Previous", "first recorded use")

    head("What it does")
    category, desc = describe_action(action)
    kv("Type", category)
    add(f"{desc}\n", "v")

    head("How to trigger")
    entry = (trigger_map or {}).get(action)
    shown = False
    if entry and entry["gestures"]:
        kv("Gesture", ", ".join(entry["gestures"])); shown = True
    if entry and entry["voice"]:
        phrases = [f'"{p}"' for p in entry["voice"][:6]]
        more = f" (+{len(entry['voice']) - 6} more)" if len(entry["voice"]) > 6 else ""
        kv("Voice", ", ".join(phrases) + more); shown = True
    if not shown:
        add("No gesture or voice mapping found in your config files.\n", "muted")

    head("Usage")
    breakdown = " \u00b7 ".join(f"{t or '?'} {c}" for t, c in d["by_trigger"])
    kv("Total runs", f"{d['total']}" + (f"  ({breakdown})" if breakdown else ""))
    if d["total"]:
        bad, conf = d.get("failed_runs", 0), d.get("verified_runs", 0)
        ok = d["total"] - bad
        kv("Ran", f"{100.0 * ok / d['total']:.0f}%  ({bad} failed)")
        kv("Verified", f"{100.0 * conf / d['total']:.0f}%  ({conf} of {d['total']} confirmed by a check)")
        if conf + bad:
            kv("Checkable", f"{100.0 * conf / (conf + bad):.0f}% worked  (of the {conf + bad} runs a check could judge)")
    kv("Today", d["today"])
    if d["all_commands"]:
        kv("Share", f"{100.0 * d['total'] / d['all_commands']:.1f}% of all commands")
    kv("First used", d["first"] or "")

    head("Around this command")
    for name, trig, secs in d["before"]:
        add(f"{'-' + format_duration(secs):<8}", "k"); add(f"{_pretty_action(name)}  ({trig})\n", "v")
    add(f"{'now':<8}", "good"); add(f"{_pretty_action(action)}  <- selected\n", "good")
    for name, trig, secs in d["after"]:
        add(f"{'+' + format_duration(secs):<8}", "k"); add(f"{_pretty_action(name)}  ({trig})\n", "v")

    if d.get("session"):
        head("Session")
        kv("Session", f"#{d['session'][0]}, started {d['session'][1]}")
    return parts


def build_session_panel(d, seq=None):
    parts = []
    add = lambda text, tag="v": parts.append((text, tag))
    kv = lambda k, v: (add(f"{k:<15}", "k"), add(f"{v}\n", "v"))
    add("SESSION\n", "title")
    add(f"Entry #{seq}\n" if seq is not None else "\n", "sub")
    add("\nTIMES\n", "h")
    kv("Started", pretty_local(d["login"]))
    kv("Last activity", pretty_local(d["logout"]))
    kv("Duration", format_duration(d["secs"]))
    add("\nACTIVITY\n", "h")
    kv("Commands", d["count"])
    if d["count"]:
        ok = d["count"] - d.get("failed", 0)
        kv("Succeeded", f"{ok}  ({100.0 * ok / d['count']:.0f}%)")
        kv("Failed", d.get("failed", 0))
    if d["top"]:
        add("Most used:\n", "k")
        for name, c in d["top"]:
            add(f"  {_pretty_action(name)}  x{c}\n", "v")
    return parts


def build_generic_panel(headers, row, title="ROW"):
    parts = [(f"{title}\n", "title"), ("\n", "sub")]
    for h, v in zip(headers, row):
        parts.append((f"{h}\n", "k"))
        parts.append((f"{format_cell(v, 2000)}\n", "v"))
    return parts




def fetch_summary(conn, table, columns, search="", date_range=None, trigger=ALL_TRIGGERS,
                  result=ALL_RESULTS, show_system=False):
    """Totals for everything matching the current filters (not just the rows on screen), for the summary cards."""
    if table == "command_logs":
        status = "COALESCE(status, 'success')" if "status" in columns else "'success'"
        ctx = "window_context" if "window_context" in columns else "'Global'"
        reason = "failure_reason" if "failure_reason" in columns else "NULL"
        base = "" if show_system else f" WHERE action_name NOT IN ({_SYSTEM_SQL})"
        inner = (f"SELECT id, timestamp, action_name, trigger_type, {ctx} AS window_context, "
                 f"{status} AS status, {reason} AS failure_reason, {_result_sql(columns)} AS result "
                 f"FROM command_logs{base}")
        where, params = build_where(table, columns, search, date_range, trigger, result)
        total, failed, voice, confirmed = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN LOWER(COALESCE(trigger_type, '')) LIKE '%voice%' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN result = 'confirmed' THEN 1 ELSE 0 END) "
            f"FROM ({inner}){where}", params).fetchone()
        total, failed, voice, confirmed = total or 0, failed or 0, voice or 0, confirmed or 0
        return {"kind": "commands", "total": total, "failed": failed, "ok": total - failed,
                "confirmed": confirmed, "unverified": total - failed - confirmed,
                "voice": voice, "gesture": total - voice}
    if table == "session_logs":
        where, params = build_where(table, columns, search, date_range, trigger)
        count, secs = conn.execute(
            "SELECT COUNT(*), SUM(MAX(0, CAST(strftime('%s', logout_time) AS INTEGER) - "
            "CAST(strftime('%s', login_time) AS INTEGER))) FROM session_logs" + where, params).fetchone()
        return {"kind": "sessions", "count": count or 0, "secs": secs or 0}
    where, params = build_where(table, columns, search, date_range, trigger)
    return {"kind": "other",
            "count": conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}{where}", params).fetchone()[0]}


# --------------------------------------------------------------------------- #
# Look and feel (same palette as the dashboard, camera HUD and Profile tab)
# --------------------------------------------------------------------------- #
BG = "#0A0A0F"
CARD = "#12121A"
ROW = "#181824"
ROW_ALT = "#0E0E15"
EDGE = "#24243A"
TEXT = "#E2E1E9"
MUTED = "#6C6B80"
GREEN = "#00e676"
BLUE = "#29b6f6"
PURPLE = "#ab47bc"
AMBER = "#ffb74d"
RED = "#ff5252"
ACCENT = "#393676"
ACCENT_HOVER = "#55529e"
HEADING = "#9b97e8"

# soft tinted backgrounds for the status "pills"
TINT = {GREEN: "#0f2a1d", RED: "#2e1216", BLUE: "#0e2532", PURPLE: "#26132b", AMBER: "#2e2312", MUTED: "#1c1c28"}


def _trigger_color(trigger):
    t = str(trigger or "").lower()
    if "voice" in t:
        return PURPLE
    if t:
        return BLUE
    return MUTED


def display_cell(header, value):
    """Text shown in the table (the CSV export keeps the plain values)."""
    if header == "Result":
        return {"Verified": "✔  VERIFIED", "Failed": "✖  FAILED", "Success": "✔  SUCCESS"}.get(value, "○  UNVERIFIED")
    if header == "Trigger" and value:
        return "●  " + str(value)
    return format_cell(value)


class _Pill(ctk.CTkLabel):
    """Small rounded status chip."""

    def __init__(self, parent, **kw):
        super().__init__(parent, text="", height=24, corner_radius=12, padx=10,
                         font=ctk.CTkFont(size=11, weight="bold"), **kw)

    def show(self, text, color):
        self.configure(text=text, text_color=color, fg_color=TINT.get(color, "#1c1c28"))
        self.pack(side="left", padx=(0, 6))

    def hide(self):
        self.pack_forget()


class _StatCard(ctk.CTkFrame):
    """Compact metric card in the same style as the Profile tab's cards."""

    def __init__(self, parent, title, color):
        super().__init__(parent, fg_color=CARD, corner_radius=12, height=78,
                         border_width=1, border_color=EDGE)
        self.pack_propagate(False)
        self.accent = ctk.CTkFrame(self, fg_color=color, width=4, corner_radius=2)
        self.accent.place(x=8, rely=0.22, relheight=0.56)
        self.title = ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=10, weight="bold"), text_color=MUTED)
        self.title.pack(anchor="w", padx=(22, 10), pady=(9, 0))
        self.value = ctk.CTkLabel(self, text="-", text_color=color,
                                  font=ctk.CTkFont(family="Consolas", size=21, weight="bold"))
        self.value.pack(anchor="w", padx=(22, 10), pady=(0, 0))
        self.caption = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=10), text_color=MUTED)
        self.caption.place(relx=1.0, x=-12, y=13, anchor="ne")

    def set(self, title, value, caption="", color=None):
        self.title.configure(text=title)
        self.value.configure(text=value)
        self.caption.configure(text=caption)
        if color:
            self.value.configure(text_color=color)
            self.accent.configure(fg_color=color)


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #
class HistoryPanel(ctk.CTkFrame):
    """The History tab: summary cards, filters, a log table and a details panel.
    The date range comes from the dashboard's range bar (set_range)."""
    LIVE_INTERVAL_MS = 2000
    PANEL_WIDTH = 350
    RESULT_LABELS = {"All": ALL_RESULTS, "Verified": "Verified", "Unverified": "Unverified", "Failed": "Failed"}

    def __init__(self, parent, base_dir=None, date_range=(None, None)):
        super().__init__(parent, fg_color=BG, corner_radius=0)

        self.base_dir = base_dir or os.getcwd()
        self.db_path = find_db_path(self.base_dir)
        self.trigger_map = load_trigger_map(self.base_dir)
        self.date_range = tuple(date_range)
        self.table_name = None
        self.headers = []
        self.rows = []          # displayed rows (maybe grouped)
        self.ids = []           # database id for each displayed row
        self.groups = []        # None or group info for each displayed row
        self.export_rows = []   # ungrouped rows for CSV
        self._label_to_table = {}
        self._trigger_values = [ALL_TRIGGERS]
        self._last_signature = None
        self._search_job = None
        self._tick_job = None
        self._pulse_job = None
        self._panel_key = None

        self._cell_font = tkfont.Font(family="Consolas", size=10)
        self._head_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")

        self._build_ui()
        self._load_tables()

        self._tick_job = self.after(self.LIVE_INTERVAL_MS, self._tick)

    # ---- Range (set by the dashboard) ----------------------------------- #
    def set_range(self, start, end, refresh=True):
        """start / end are local days as 'YYYY-MM-DD' (or None)."""
        self.date_range = (start, end)
        self.range_lbl.configure(text=self._range_text())
        if refresh:
            self.refresh(force=True)

    def _range_text(self):
        start, end = self.date_range
        if start and end:
            return start if start == end else f"{start}  →  {end}"
        return "all dates"

    # ---- UI ------------------------------------------------------------- #
    def _segmented(self, parent, values, command):
        return ctk.CTkSegmentedButton(
            parent, values=values, command=command, height=32, corner_radius=8,
            fg_color=ROW, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=ROW, unselected_hover_color="#22223a", text_color=TEXT,
            font=ctk.CTkFont(size=12, weight="bold"))

    def _button(self, parent, text, command, width=92, primary=False):
        return ctk.CTkButton(parent, text=text, width=width, height=32, corner_radius=8,
                             fg_color=ACCENT if primary else ROW, hover_color=ACCENT_HOVER,
                             border_width=0 if primary else 1, border_color=EDGE, text_color=TEXT,
                             font=ctk.CTkFont(size=12, weight="bold"), command=command)

    def _switch(self, parent, text, variable, command=None):
        return ctk.CTkSwitch(parent, text=text, variable=variable, command=command, width=40,
                             progress_color=GREEN, button_color=TEXT, button_hover_color="#ffffff",
                             fg_color="#2a2a3c", text_color=TEXT, font=ctk.CTkFont(size=12))

    def _build_ui(self):
        # ---------- title row ----------
        title_row = ctk.CTkFrame(self, fg_color="transparent")
        title_row.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(title_row, text="\U0001F552  ACTIVITY HISTORY", text_color=TEXT,
                     font=ctk.CTkFont(family="Consolas", size=15, weight="bold")).pack(side="left")
        self.range_lbl = ctk.CTkLabel(title_row, text=self._range_text(), text_color=MUTED,
                                      font=ctk.CTkFont(family="Consolas", size=11))
        self.range_lbl.pack(side="left", padx=(12, 0))
        self.live_lbl = ctk.CTkLabel(title_row, text="● LIVE", text_color=GREEN, height=24, corner_radius=12,
                                     fg_color=TINT[GREEN], padx=10, font=ctk.CTkFont(size=11, weight="bold"))
        self.live_lbl.pack(side="right")

        # ---------- summary cards ----------
        cards = ctk.CTkFrame(self, fg_color="transparent")
        cards.pack(fill="x", pady=(0, 8))
        self.cards = []
        for i, (title, color) in enumerate((("IN RANGE", BLUE), ("VERIFIED", GREEN),
                                            ("FAILED", RED), ("VOICE  ·  GESTURE", PURPLE))):
            card = _StatCard(cards, title, color)
            card.pack(side="left", fill="x", expand=True, padx=(0 if i == 0 else 5, 0 if i == 3 else 5))
            self.cards.append(card)

        # ---------- filter bar ----------
        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12, border_width=1, border_color=EDGE)
        bar.pack(fill="x", pady=(0, 8))

        row1 = ctk.CTkFrame(bar, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=(10, 6))
        self.table_seg = self._segmented(row1, ["(no tables)"], self._on_table_change)
        self.table_seg.pack(side="left", padx=(0, 10))
        self.result_seg = self._segmented(row1, list(self.RESULT_LABELS), lambda _v: self.refresh(force=True))
        self.result_seg.set("All")
        self.result_seg.pack(side="left", padx=(0, 10))
        self.trigger_menu = ctk.CTkOptionMenu(
            row1, values=[ALL_TRIGGERS], width=130, height=32, corner_radius=8,
            fg_color=ROW, button_color=ACCENT, button_hover_color=ACCENT_HOVER, text_color=TEXT,
            dropdown_fg_color=CARD, dropdown_hover_color=ACCENT, font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda _v: self.refresh(force=True))
        self.trigger_menu.pack(side="left", padx=(0, 10))
        self.search_entry = ctk.CTkEntry(
            row1, height=32, corner_radius=8, fg_color=BG, border_color=EDGE, border_width=1, text_color=TEXT,
            placeholder_text="\U0001F50E  Search action, trigger, context, result...",
            placeholder_text_color=MUTED)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<KeyRelease>", self._on_search_key)

        row2 = ctk.CTkFrame(bar, fg_color="transparent")
        row2.pack(fill="x", padx=10, pady=(0, 10))
        self.live_var = ctk.BooleanVar(value=True)
        self._switch(row2, "Live", self.live_var, self._update_live_badge).pack(side="left", padx=(2, 14))
        self.group_var = ctk.BooleanVar(value=False)
        self._switch(row2, "Group repeats", self.group_var, lambda: self.refresh(force=True)).pack(side="left", padx=(0, 14))
        self.system_var = ctk.BooleanVar(value=False)
        self._switch(row2, "System events", self.system_var, lambda: self.refresh(force=True)).pack(side="left", padx=(0, 14))
        self.panel_var = ctk.BooleanVar(value=True)
        self._switch(row2, "Details", self.panel_var, self._toggle_panel).pack(side="left", padx=(0, 14))
        ctk.CTkLabel(row2, text="Rows", text_color=MUTED, font=ctk.CTkFont(size=12)).pack(side="left", padx=(4, 6))
        self.limit_menu = ctk.CTkOptionMenu(
            row2, values=["100", "500", "1000", "5000"], width=80, height=30, corner_radius=8,
            fg_color=ROW, button_color=ACCENT, button_hover_color=ACCENT_HOVER, text_color=TEXT,
            dropdown_fg_color=CARD, dropdown_hover_color=ACCENT, command=lambda _v: self.refresh(force=True))
        self.limit_menu.set("500")
        self.limit_menu.pack(side="left")
        self._button(row2, "⬇  Export CSV", self.export_csv, width=118, primary=True).pack(side="right")
        self._button(row2, "⟳  Refresh", self._manual_refresh, width=100).pack(side="right", padx=6)
        self._button(row2, "\U0001F4C2", self.choose_database, width=40).pack(side="right")

        # ---------- body: table + details ----------
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        table_card = ctk.CTkFrame(body, fg_color=CARD, corner_radius=12, border_width=1, border_color=EDGE)
        table_card.grid(row=0, column=0, sticky="nsew")
        self.table_area = tk.Frame(table_card, bg=CARD)
        self.table_area.pack(fill="both", expand=True, padx=8, pady=8)
        self.table_area.grid_rowconfigure(0, weight=1)
        self.table_area.grid_columnconfigure(0, weight=1)

        style = ttk.Style(self)
        style.theme_use("clam")  # the default Windows theme ignores Treeview colours
        style.configure("History.Treeview", background=ROW_ALT, fieldbackground=CARD, foreground=TEXT,
                        rowheight=32, borderwidth=0, relief="flat", font=("Consolas", 10))
        style.configure("History.Treeview.Heading", background=CARD, foreground=MUTED, relief="flat",
                        borderwidth=0, padding=(6, 8), font=("Segoe UI", 9, "bold"))
        style.map("History.Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        style.map("History.Treeview.Heading", background=[("active", ROW)], foreground=[("active", TEXT)])
        style.layout("History.Treeview", [("History.Treeview.treearea", {"sticky": "nswe"})])   # no border

        self.tree = ttk.Treeview(self.table_area, style="History.Treeview", show="headings", selectmode="browse")
        sb_kw = dict(fg_color=CARD, button_color="#2c2c3e", button_hover_color=ACCENT)
        vsb = ctk.CTkScrollbar(self.table_area, orientation="vertical", command=self.tree.yview, **sb_kw)
        hsb = ctk.CTkScrollbar(self.table_area, orientation="horizontal", command=self.tree.xview, **sb_kw)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("even", background=ROW_ALT)
        self.tree.tag_configure("odd", background=ROW)
        self.tree.tag_configure("failed", foreground=RED)   # only the text colour, so row shading still applies
        self.tree.tag_configure("voice", foreground="#d9b3e0")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Configure>", lambda _e: self._fit_columns())
        self._col_base = []

        self.empty_box = ctk.CTkFrame(self.table_area, fg_color=CARD)
        self.empty_icon = ctk.CTkLabel(self.empty_box, text="\U0001F4ED", font=ctk.CTkFont(size=38))
        self.empty_icon.pack(pady=(0, 6))
        self.empty_label = ctk.CTkLabel(self.empty_box, text="", text_color=MUTED, justify="center",
                                        font=ctk.CTkFont(size=13))
        self.empty_label.pack()

        # --- details panel ---
        self.detail_panel = ctk.CTkFrame(body, width=self.PANEL_WIDTH, fg_color=CARD, corner_radius=12,
                                         border_width=1, border_color=EDGE)
        self.detail_panel.grid(row=0, column=1, sticky="ns", padx=(10, 0))
        self.detail_panel.pack_propagate(False)

        head = ctk.CTkFrame(self.detail_panel, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 0))
        self.d_kicker = ctk.CTkLabel(head, text="DETAILS", text_color=HEADING,
                                     font=ctk.CTkFont(size=10, weight="bold"))
        self.d_kicker.pack(anchor="w")
        self.d_title = ctk.CTkLabel(head, text="", text_color="#ffffff", justify="left", anchor="w",
                                    wraplength=self.PANEL_WIDTH - 40,
                                    font=ctk.CTkFont(family="Segoe UI", size=19, weight="bold"))
        self.d_title.pack(anchor="w", fill="x")
        self.d_sub = ctk.CTkLabel(head, text="", text_color=MUTED, justify="left", anchor="w",
                                  wraplength=self.PANEL_WIDTH - 40, font=ctk.CTkFont(size=11))
        self.d_sub.pack(anchor="w", fill="x")
        # height=1: an empty CTkFrame otherwise keeps its default 200 px height, which pushed the idle text down
        self.pills_row = ctk.CTkFrame(head, fg_color="transparent", height=1)
        self.pills_row.pack(anchor="w", pady=(8, 0))
        self.pills = [_Pill(self.pills_row) for _ in range(3)]
        ctk.CTkFrame(self.detail_panel, fg_color=EDGE, height=1).pack(fill="x", padx=16, pady=(12, 0))

        self.detail_text = ctk.CTkTextbox(self.detail_panel, fg_color=CARD, text_color=TEXT,
                                          font=("Consolas", 12), wrap="word", border_width=0,
                                          scrollbar_button_color="#2c2c3e", scrollbar_button_hover_color=ACCENT)
        self.detail_text.pack(fill="both", expand=True, padx=8, pady=(6, 10))
        try:
            tb = self.detail_text._textbox
            tb.tag_config("title", foreground="#FFFFFF", font=("Segoe UI", 16, "bold"), spacing3=2)
            tb.tag_config("sub", foreground=MUTED)
            tb.tag_config("h", foreground=HEADING, font=("Segoe UI", 9, "bold"), spacing1=2, spacing3=2)
            tb.tag_config("k", foreground=MUTED)
            tb.tag_config("v", foreground=TEXT)
            tb.tag_config("good", foreground=GREEN)
            tb.tag_config("bad", foreground=RED)
            tb.tag_config("muted", foreground=MUTED, font=("Segoe UI", 10), spacing1=4)
        except Exception:
            pass
        self._panel_idle()

        # ---------- status bar ----------
        status_row = ctk.CTkFrame(self, fg_color="transparent")
        status_row.pack(fill="x", pady=(6, 0))
        self.status_label = ctk.CTkLabel(status_row, text="", anchor="w", text_color=MUTED,
                                         font=ctk.CTkFont(family="Consolas", size=11))
        self.status_label.pack(side="left", fill="x", expand=True)

    def _set_status(self, text):
        self.status_label.configure(text=text)

    def _show_empty(self, message, icon="\U0001F4ED"):
        self.empty_icon.configure(text=icon)
        self.empty_label.configure(text=message)
        self.empty_box.place(relx=0.5, rely=0.5, anchor="center")

    def _hide_empty(self):
        self.empty_box.place_forget()

    def _sync_filter_state(self):
        is_commands = self.table_name == "command_logs"
        state = "normal" if is_commands else "disabled"
        self.trigger_menu.configure(state=state)
        self.result_seg.configure(state=state)

    def _toggle_panel(self):
        if self.panel_var.get():
            self.detail_panel.grid()
        else:
            self.detail_panel.grid_remove()

    def _update_live_badge(self):
        if self.live_var.get():
            self.live_lbl.configure(text="● LIVE", text_color=GREEN, fg_color=TINT[GREEN])
        else:
            self.live_lbl.configure(text="○ PAUSED", text_color=MUTED, fg_color=TINT[MUTED])

    def _pulse_live(self):
        """Brief highlight on the LIVE badge when new entries arrive."""
        if not self.live_var.get():
            return
        self.live_lbl.configure(fg_color="#1f5c3c", text_color="#ffffff")
        if self._pulse_job:
            self.after_cancel(self._pulse_job)
        self._pulse_job = self.after(700, self._update_live_badge)

    # ---- Summary cards -------------------------------------------------- #
    def _update_cards(self, summary):
        c = self.cards
        if not summary:
            for card in c:
                card.set(card.title.cget("text"), "-", "")
            return
        if summary["kind"] == "commands":
            total, ok, bad = summary["total"], summary["ok"], summary["failed"]
            conf = summary.get("confirmed", 0)
            c[0].set("COMMANDS", f"{total:,}", f"{100.0 * ok / total:.0f}% ran" if total else "matching filters", BLUE)
            c[1].set("VERIFIED", f"{conf:,}", f"{100.0 * conf / total:.0f}%  ·  {summary.get('unverified', 0):,} unchecked"
                     if total else "", GREEN)
            c[2].set("FAILED", f"{bad:,}", f"{100.0 * bad / total:.0f}%" if total else "", RED if bad else MUTED)
            c[3].set("VOICE  ·  GESTURE", f"{summary['voice']:,}  ·  {summary['gesture']:,}", "", PURPLE)
        elif summary["kind"] == "sessions":
            n, secs = summary["count"], summary["secs"]
            c[0].set("SESSIONS", f"{n:,}", "START → STOP", BLUE)
            c[1].set("TOTAL TIME", format_duration(secs) or "0s", "", GREEN)
            c[2].set("AVG LENGTH", format_duration(secs / n) if n else "-", "", AMBER)
            c[3].set("PER DAY", f"{n / max(1, self._range_days()):.1f}", "sessions", PURPLE)
        else:
            c[0].set("ROWS", f"{summary['count']:,}", "", BLUE)
            for card in c[1:]:
                card.set(card.title.cget("text"), "-", "", MUTED)

    def _range_days(self):
        start, end = self.date_range
        try:
            return (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days + 1
        except (TypeError, ValueError):
            return 1

    # ---- Details panel -------------------------------------------------- #
    def _panel_header(self, kicker, title, sub="", pills=()):
        self.d_kicker.configure(text=kicker)
        self.d_title.configure(text=title)
        self.d_sub.configure(text=sub)
        for pill, spec in zip(self.pills, list(pills) + [None] * (len(self.pills) - len(pills))):
            if spec:
                pill.show(*spec)
            else:
                pill.hide()
        if pills:
            self.pills_row.pack(anchor="w", pady=(8, 0), after=self.d_sub)
        else:
            self.pills_row.pack_forget()

    def _panel_write(self, parts, key=None):
        # the builders start with a "title" and a "sub" line; those are shown in the header instead
        parts = [p for p in parts if p[1] not in ("title", "sub")]
        if parts and parts[0][0].startswith("\n"):
            parts[0] = (parts[0][0][1:], parts[0][1])      # no blank line right under the header
        tb = self.detail_text
        keep = self._panel_key == key and key is not None
        try:
            frac = tb.yview()[0] if keep else 0.0
        except Exception:
            frac = 0.0
        tb.configure(state="normal")
        tb.delete("1.0", "end")
        for text, tag in parts:
            tb.insert("end", text, tag)
        tb.configure(state="disabled")
        try:
            tb.yview("moveto", frac)
        except Exception:
            pass
        self._panel_key = key

    def _panel_idle(self):
        self._panel_header("DETAILS", "Select an entry",
                           "Click any row to see what happened and why.")
        self._panel_write([("\nYou will see whether the command succeeded or failed, what it does, "
                            "how it is triggered, how often you use it, and what happened right before "
                            "and after it.\n", "muted")])

    def _selected_index(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            idx = int(sel[0])
        except ValueError:
            return None
        return idx if 0 <= idx < len(self.rows) else None

    def _on_select(self, _event=None):
        idx = self._selected_index()
        if idx is None:
            self._panel_idle()
        else:
            self._show_details(idx)

    def _show_details(self, idx):
        row = self.rows[idx]
        key = (self.table_name, self.ids[idx] if idx < len(self.ids) else idx)
        try:
            if self.table_name == "command_logs":
                with closing(open_readonly(self.db_path)) as conn:
                    d = fetch_command_details(conn, self.ids[idx], get_columns(conn, "command_logs"))
                if d:
                    failed = d["status"] == "failed"
                    group = self.groups[idx]
                    self._panel_header(
                        f"COMMAND  #{row[0]}" + (f"   ·   ×{group['count']}" if group else ""),
                        _pretty_action(d["action"]),
                        pretty_local(d["local"]) + (f"   ·   {relative_time(d['utc'])}" if d["utc"] else ""),
                        [("✖  FAILED", RED) if failed else
                         (("✔  VERIFIED", GREEN) if d.get("verification") == "confirmed" else ("○  UNVERIFIED", BLUE)),
                         (d["trigger"].upper() or "UNKNOWN", _trigger_color(d["trigger"])),
                         (d["context"], MUTED)])
                    parts = build_command_panel(d, seq=row[0], group=group, trigger_map=self.trigger_map)
                else:
                    self._panel_header("COMMAND", "Entry not found")
                    parts = [("This entry no longer exists.\n", "muted")]
            elif self.table_name == "session_logs":
                with closing(open_readonly(self.db_path)) as conn:
                    d = fetch_session_details(conn, self.ids[idx])
                if d:
                    pills = [(format_duration(d["secs"]) or "0s", BLUE),
                             (f"{d['count']} commands", PURPLE)]
                    if d.get("failed"):
                        pills.append((f"{d['failed']} failed", RED))
                    self._panel_header(f"SESSION  #{row[0]}", pretty_local(d["login"]),
                                       f"until {pretty_local(d['logout'])}", pills)
                    parts = build_session_panel(d, seq=row[0])
                else:
                    self._panel_header("SESSION", "Entry not found")
                    parts = [("This entry no longer exists.\n", "muted")]
            else:
                label = TABLE_LABELS.get(self.table_name, self.table_name)
                self._panel_header(str(label).upper(), f"Row {idx + 1}")
                parts = build_generic_panel(self.headers, row, str(label).upper())
        except sqlite3.Error as e:
            self._panel_header("DETAILS", "Couldn't load details")
            parts = [(f"{e}\n", "muted")]
        self._panel_write(parts, key)

    # ---- Data loading --------------------------------------------------- #
    def _set_table_values(self, labels, current):
        self.table_seg.configure(values=labels)
        self.table_seg.set(current)

    def _load_tables(self):
        self._last_signature = None
        if not self.db_path or not os.path.isfile(self.db_path):
            self.table_name = None
            self._set_table_values(["(no database)"], "(no database)")
            self._sync_filter_state()
            self._render([], [], [], [])
            self._update_cards(None)
            self._show_empty("No database file found.\nUse the \U0001F4C2 button to pick your .db file.", "\U0001F5C4")
            self._set_status("No database selected")
            return
        try:
            with closing(open_readonly(self.db_path)) as conn:
                tables = list_tables(conn)
        except sqlite3.Error as e:
            self._render([], [], [], [])
            self._show_empty(f"Couldn't open the database:\n{e}", "⚠")
            self._set_status(self.db_path)
            return
        if not tables:
            self.table_name = None
            self._set_table_values(["(no tables)"], "(no tables)")
            self._sync_filter_state()
            self._render([], [], [], [])
            self._update_cards(None)
            self._show_empty("The database has no history tables yet.")
            self._set_status(self.db_path)
            return

        self._label_to_table = {TABLE_LABELS.get(t, t): t for t in tables}
        if self.table_name not in tables:
            self.table_name = pick_default_table(tables)
        self._set_table_values(list(self._label_to_table), TABLE_LABELS.get(self.table_name, self.table_name))
        self._sync_filter_state()
        self.refresh(force=True)

    def _update_trigger_menu(self, values):
        options = [ALL_TRIGGERS] + list(values)
        if options != self._trigger_values:
            self._trigger_values = options
            self.trigger_menu.configure(values=options)
            if self.trigger_menu.get() not in options:
                self.trigger_menu.set(ALL_TRIGGERS)

    def _manual_refresh(self):
        self.trigger_map = load_trigger_map(self.base_dir)
        self.refresh(force=True)

    def _result_filter(self):
        return self.RESULT_LABELS.get(self.result_seg.get(), ALL_RESULTS)

    def refresh(self, force=False):
        if not self.db_path or not self.table_name:
            return
        search = self.search_entry.get().strip()
        try:
            with closing(open_readonly(self.db_path)) as conn:
                signature = table_signature(conn, self.table_name)
                if not force and signature == self._last_signature:
                    return
                columns = get_columns(conn, self.table_name)
                if self.table_name == "command_logs":
                    self._update_trigger_menu(distinct_triggers(conn))
                trigger = self.trigger_menu.get()
                result = self._result_filter()
                headers, rows, ids, matching = fetch_view(
                    conn, self.table_name, columns, search, self.date_range, trigger,
                    int(self.limit_menu.get()), result, self.system_var.get())
                summary = fetch_summary(conn, self.table_name, columns, search, self.date_range, trigger,
                                        result, self.system_var.get())
        except sqlite3.Error as e:  # e.g. database briefly locked while the engine writes
            self._set_status(f"Database busy or unreadable: {e}")
            return

        new_data = self._last_signature is not None and signature != self._last_signature
        self._last_signature = signature
        if new_data:
            self._pulse_live()
        self._update_cards(summary)

        shown = len(rows)
        export_rows = rows
        groups = [None] * len(rows)
        if self.table_name == "command_logs" and self.group_var.get() and rows:
            rows, ids, groups = group_repeats(rows, ids)

        # keep the selected row selected across refreshes
        idx = self._selected_index()
        keep_id = self.ids[idx] if idx is not None and idx < len(self.ids) else None
        self.export_rows = export_rows
        self._render(headers, rows, ids, groups, keep_id)

        if rows:
            self._hide_empty()
        else:
            filtered = bool(search) or (self.table_name == "command_logs" and
                                        (trigger != ALL_TRIGGERS or result != ALL_RESULTS))
            if filtered:
                self._show_empty("Nothing matches the current filters\nin this date range.", "\U0001F50D")
            elif self.table_name == "command_logs":
                self._show_empty("No commands in this date range.\nPick a wider range, or start the engine\n"
                                 "and use a gesture or voice command.")
            elif self.table_name == "session_logs":
                self._show_empty("No sessions in this date range.")
            else:
                self._show_empty("This table is empty.")
        grouped_note = f" in {len(rows)} groups" if len(rows) != shown else ""
        self._set_status(f"Showing {shown} of {matching} rows{grouped_note}   ·   "
                         f"{TABLE_LABELS.get(self.table_name, self.table_name)}   ·   "
                         f"{self._range_text()}   ·   updated {time.strftime('%H:%M:%S')}")

    def _render(self, headers, rows, ids, groups, keep_id=None):
        self.headers = list(headers)
        self.rows = rows
        self.ids = list(ids)
        self.groups = list(groups) if groups else [None] * len(rows)
        self.tree.delete(*self.tree.get_children())

        col_ids = [f"c{i}" for i in range(len(headers))]
        self.tree.configure(columns=col_ids)
        sample = rows[:200]
        for i, (cid, name) in enumerate(zip(col_ids, headers)):
            widest = self._head_font.measure(name.upper()) + 30
            for r in sample:
                widest = max(widest, self._cell_font.measure(display_cell(name, r[i])[:80]) + 28)
            self.tree.heading(cid, text=name.upper(), anchor="w")
            self.tree.column(cid, width=min(380, max(52, widest)), minwidth=48, stretch=False, anchor="w")
        self._col_base = [(cid, self.tree.column(cid, "width")) for cid in col_ids]
        self._fit_columns()

        result_col = headers.index("Result") if "Result" in headers else None
        trigger_col = headers.index("Trigger") if "Trigger" in headers else None
        restore = None
        for n, row in enumerate(rows):
            tags = ["odd" if n % 2 else "even"]
            if result_col is not None and row[result_col] == "Failed":
                tags.append("failed")
            elif trigger_col is not None and "voice" in str(row[trigger_col]).lower():
                tags.append("voice")
            self.tree.insert("", "end", iid=str(n), values=[display_cell(h, v) for h, v in zip(headers, row)],
                             tags=tuple(tags))
            if keep_id is not None and restore is None and n < len(self.ids) and self.ids[n] == keep_id:
                restore = str(n)
        if restore is not None:
            self.tree.selection_set(restore)   # fires <<TreeviewSelect>> -> panel refresh
            self.tree.see(restore)
        elif keep_id is not None or self._panel_key is not None:
            self._panel_idle()

    def _fit_columns(self):
        """Spreads spare width over the columns so the table always fills its card (Tk only stretches
        columns when the window is resized, so the rows used to stop halfway across)."""
        if not self._col_base:
            return
        available = self.tree.winfo_width() - 4
        natural = sum(w for _c, w in self._col_base)
        extra = max(0, available - natural)
        # give the spare room to the wider text columns (Action, Time...) rather than to "#"
        weights = [w if i else 0 for i, (_c, w) in enumerate(self._col_base)]
        total = sum(weights) or 1
        for (cid, base), weight in zip(self._col_base, weights):
            self.tree.column(cid, width=base + int(extra * weight / total))

    # ---- Events --------------------------------------------------------- #
    def _on_table_change(self, label):
        table = self._label_to_table.get(label)
        if not table:
            return
        self.table_name = table
        if self.search_entry.get():          # deleting while the placeholder shows would erase the placeholder
            self.search_entry.delete(0, "end")
            self.focus_set()
        self.trigger_menu.set(ALL_TRIGGERS)
        self.result_seg.set("All")
        self._sync_filter_state()
        self._panel_key = None
        self._panel_idle()
        self.refresh(force=True)

    def _on_search_key(self, _event=None):
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(250, lambda: self.refresh(force=True))

    def _tick(self):
        try:
            if not self.winfo_exists():
                return
            # only poll while the History tab is actually on screen
            if self.live_var.get() and self.winfo_ismapped():
                self.refresh()
            self._tick_job = self.after(self.LIVE_INTERVAL_MS, self._tick)
        except Exception:
            pass

    def choose_database(self):
        path = filedialog.askopenfilename(
            parent=self, title="Choose database file", initialdir=self.base_dir,
            filetypes=[("SQLite database", "*.db *.sqlite *.sqlite3"), ("All files", "*.*")])
        if path:
            self.db_path = path
            self.table_name = None
            self._panel_key = None
            self._load_tables()

    def export_csv(self):
        if not self.headers:
            messagebox.showinfo("Export CSV", "There is nothing to export yet.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", initialfile=f"{self.table_name}_history.csv",
            filetypes=[("CSV file", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(self.headers)
                writer.writerows(self.export_rows)
            self._set_status(f"Exported {len(self.export_rows)} rows to {path}")
        except OSError as e:
            messagebox.showerror("Export CSV", f"Couldn't write the file:\n{e}", parent=self)

    def destroy(self):
        for job in (self._search_job, self._tick_job, self._pulse_job):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        super().destroy()