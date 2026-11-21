import sqlite3
import os
import math
import hashlib
from contextlib import closing
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "config", "system_analytics.db")

# Internal events that are logged for the history but are not user "tasks" (they are left out of
# every statistic: commands done, success rate, charts...).
SYSTEM_EVENT_ACTIONS = ("wake_gate",)
_SYS_PLACEHOLDERS = ",".join("?" * len(SYSTEM_EVENT_ACTIONS))

# id of the session row that is currently open (engine started, not yet stopped); None otherwise.
_current_session_id = None
_schema_checked = False

def init_db():
    """Initializes the SQLite analytical schema tables safely with session and window tracking."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_profile (
            field_key TEXT PRIMARY KEY,
            field_value TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS command_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_name TEXT,
            trigger_type TEXT,
            window_context TEXT DEFAULT 'Global',
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'success',
            failure_reason TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            logout_time DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    try:
        cursor.execute("PRAGMA table_info(command_logs)")
        columns = [row[1] for row in cursor.fetchall()]

        # Older databases are upgraded in place. Rows logged before 'status' existed read back as
        # 'success' (the default), because the outcome of those commands was never recorded.
        wanted = {
            "window_context": "TEXT DEFAULT 'Global'",
            "status": "TEXT DEFAULT 'success'",
            "failure_reason": "TEXT",
            # 'confirmed' = a check saw the expected effect, 'unverified' = ran but could not be checked,
            # 'failed' = blocked or no effect. Rows logged before this column existed read as unverified.
            "verification": "TEXT",
        }
        for col, ddl in wanted.items():
            if col not in columns:
                print(f"[DB MIGRATION] Legacy schema detected. Injecting '{col}' column...")
                cursor.execute(f"ALTER TABLE command_logs ADD COLUMN {col} {ddl}")
                conn.commit()
                print("[DB MIGRATION] Schema successfully updated.")
    except Exception as migration_error:
        print(f"[DB MIGRATION ERROR] Migration patch failed: {migration_error}")

    conn.commit()
    conn.close()
    global _schema_checked, _command_columns
    _schema_checked = True
    _command_columns = None


def _ensure_schema():
    """Makes sure the new columns exist before any analytics query runs (init_db is idempotent)."""
    if not _schema_checked:
        init_db()

_command_columns = None


def _command_log_columns(cursor):
    """Column names of command_logs, read once per run (this used to run a PRAGMA query on every logged command)."""
    global _command_columns
    if _command_columns is None:
        cursor.execute("PRAGMA table_info(command_logs)")
        _command_columns = {col[1] for col in cursor.fetchall()}
    return _command_columns


def log_command_execution(action_name, trigger_type, window_context="Global",
                          status="success", failure_reason="", verification=None):
    """Inserts an analytics record (with its outcome) and keeps the current session's activity time fresh.

    status is "success" or "failed"; failure_reason is a short human-readable explanation for failures.
    verification is "confirmed" when a check saw the command's effect, otherwise "unverified"
    (failed commands are always stored as "failed").
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cursor = conn.cursor()

        if _current_session_id is not None:
            # Engine is running: every command belongs to the session opened by start_session().
            cursor.execute(
                "UPDATE session_logs SET logout_time = CURRENT_TIMESTAMP WHERE id = ?",
                (_current_session_id,)
            )
        else:
            # Fallback (no explicit session): a new session starts after 30 minutes of inactivity.
            cursor.execute("SELECT id, logout_time FROM session_logs ORDER BY id DESC LIMIT 1")
            last_session = cursor.fetchone()

            should_create_session = True
            if last_session:
                last_session_id, last_activity = last_session
                try:
                    last_time = datetime.strptime(last_activity, "%Y-%m-%d %H:%M:%S")
                    if (datetime.utcnow() - last_time).total_seconds() < 1800:
                        cursor.execute(
                            "UPDATE session_logs SET logout_time = CURRENT_TIMESTAMP WHERE id = ?",
                            (last_session_id,)
                        )
                        should_create_session = False
                except Exception:
                    pass

            if should_create_session:
                cursor.execute("INSERT INTO session_logs (login_time, logout_time) VALUES (CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")

        columns = _command_log_columns(cursor)

        fields, values = ["action_name", "trigger_type"], [action_name, trigger_type]
        if "window_context" in columns:
            fields.append("window_context")
            values.append(window_context)
        if "status" in columns:
            fields.append("status")
            values.append("failed" if str(status).lower() == "failed" else "success")
        if "failure_reason" in columns:
            fields.append("failure_reason")
            values.append((failure_reason or "")[:300] or None)
        if "verification" in columns:
            fields.append("verification")
            if str(status).lower() == "failed":
                values.append("failed")
            else:
                values.append("confirmed" if verification == "confirmed" else "unverified")
        cursor.execute(
            f"INSERT INTO command_logs ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
            values
        )

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB SAFE SHIELD] Prevented database crash loop: {e}")
        try:
            conn.close()
        except Exception:
            pass

def start_session():
    """Opens a new session (call when the engine is started). Returns its id, or None on failure."""
    global _current_session_id
    try:
        with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO session_logs (login_time, logout_time) VALUES (CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
            _current_session_id = cur.lastrowid
            conn.commit()
        return _current_session_id
    except Exception as e:
        print(f"[DB ERROR] Could not start session: {e}")
        return None


def end_session():
    """Closes the open session (call when the engine is stopped or the app closes)."""
    global _current_session_id
    sid, _current_session_id = _current_session_id, None
    if sid is None:
        return
    try:
        with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn:
            conn.execute("UPDATE session_logs SET logout_time = CURRENT_TIMESTAMP WHERE id = ?", (sid,))
            conn.commit()
    except Exception as e:
        print(f"[DB ERROR] Could not close session: {e}")


def store_profile_data(field_key, raw_value):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cursor = conn.cursor()
        if "password" in field_key.lower():
            processed_value = hashlib.sha256(raw_value.encode('utf-8')).hexdigest()
        else:
            processed_value = raw_value
            
        cursor.execute("""
            INSERT OR REPLACE INTO user_profile (field_key, field_value) 
            VALUES (?, ?)
        """, (field_key.lower().strip(), processed_value))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB ERROR] Profile storage failure: {e}")
        return False

def get_profile_data(field_key):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cursor = conn.cursor()
        cursor.execute("SELECT field_value FROM user_profile WHERE field_key = ?", (field_key.lower().strip(),))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB ERROR] Profile retrieval failure: {e}")
        return None

# --------------------------------------------------------------------------- #
# Range-aware analytics for the Profile dashboard.
# start / end are local calendar days as "YYYY-MM-DD" strings (None = unbounded). Timestamps are stored
# in UTC, so every comparison converts to local time first.
# --------------------------------------------------------------------------- #
_OK_CASE = "CASE WHEN COALESCE(status, 'success') = 'failed' THEN 0 ELSE 1 END"
_FAIL_CASE = "CASE WHEN COALESCE(status, 'success') = 'failed' THEN 1 ELSE 0 END"
_VOICE_CASE = "CASE WHEN LOWER(COALESCE(trigger_type, '')) LIKE '%voice%' THEN 1 ELSE 0 END"
# a success whose effect a check actually saw (see action_verifier.py)
_CONFIRMED_CASE = ("CASE WHEN COALESCE(status, 'success') != 'failed' AND verification = 'confirmed' "
                   "THEN 1 ELSE 0 END")


# --------------------------------------------------------------------------- #
# Reliability rates. Every command is one of:
#   C confirmed  - ran, and a check saw the expected effect
#   U unverified - ran, but its effect cannot be observed (mouse click, scroll, Enter...) or was not checked
#   F failed     - blocked by a pre-check, no effect seen by the post-check, or an error
# Verified rate         = C / (C+U+F)   lower bound: only proven successes count
# Execution rate        = (C+U) / N     upper bound: everything that did not fail counts
# Checkable success rate = C / (C+F)    reliability of the commands the system is able to check
# --------------------------------------------------------------------------- #
def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def wilson_interval(successes, n, z=1.96):
    """95% Wilson score interval for a proportion, in percent, or None when n is 0.
    Better than the plain +/- formula for small samples and rates near 0% or 100%."""
    if not n:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(100 * max(0.0, centre - half), 1), round(100 * min(1.0, centre + half), 1)


def reliability_rates(confirmed, unverified, failed):
    """The three rates (in percent, None when there is nothing to divide by) plus 95% intervals."""
    c, u, f = int(confirmed or 0), int(unverified or 0), int(failed or 0)
    n = c + u + f
    return {
        "total": n, "confirmed": c, "unverified": u, "failed": f,
        "verified_rate": _pct(c, n),
        "execution_rate": _pct(c + u, n),
        "checkable_rate": _pct(c, c + f),
        "verified_ci": wilson_interval(c, n),
        "checkable_ci": wilson_interval(c, c + f),
    }


def _command_where(start, end):
    """WHERE clause + params for command_logs in a day range, system events excluded."""
    clauses = [f"action_name NOT IN ({_SYS_PLACEHOLDERS})"]
    params = list(SYSTEM_EVENT_ACTIONS)
    if start:
        clauses.append("date(timestamp, 'localtime') >= ?")
        params.append(start)
    if end:
        clauses.append("date(timestamp, 'localtime') <= ?")
        params.append(end)
    return " WHERE " + " AND ".join(clauses), params


def get_overview_metrics(start=None, end=None):
    """Headline numbers for the selected range."""
    _ensure_schema()
    out = {"sessions": 0, "commands": 0, "succeeded": 0, "failed": 0, "success_rate": None,
           "avg_session_min": 0.0, "voice": 0, "gesture": 0,
           "reliability": reliability_rates(0, 0, 0),
           "by_trigger": {"voice": reliability_rates(0, 0, 0), "gesture": reliability_rates(0, 0, 0)}}
    try:
        with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn:
            where, params = _command_where(start, end)
            total, failed, voice, confirmed = conn.execute(
                f"SELECT COUNT(*), SUM({_FAIL_CASE}), SUM({_VOICE_CASE}), SUM({_CONFIRMED_CASE}) "
                f"FROM command_logs{where}", params
            ).fetchone()
            total, failed, voice, confirmed = total or 0, failed or 0, voice or 0, confirmed or 0
            out.update(commands=total, failed=failed, succeeded=total - failed,
                       voice=voice, gesture=total - voice,
                       reliability=reliability_rates(confirmed, total - failed - confirmed, failed))
            if total:
                out["success_rate"] = round(100.0 * (total - failed) / total, 1)   # = execution rate
            for is_voice, n, bad, conf in conn.execute(
                    f"SELECT {_VOICE_CASE} AS v, COUNT(*), SUM({_FAIL_CASE}), SUM({_CONFIRMED_CASE}) "
                    f"FROM command_logs{where} GROUP BY v", params).fetchall():
                n, bad, conf = n or 0, bad or 0, conf or 0
                out["by_trigger"]["voice" if is_voice else "gesture"] = reliability_rates(conf, n - bad - conf, bad)

            s_clauses, s_params = [], []
            if start:
                s_clauses.append("date(login_time, 'localtime') >= ?")
                s_params.append(start)
            if end:
                s_clauses.append("date(login_time, 'localtime') <= ?")
                s_params.append(end)
            s_where = (" WHERE " + " AND ".join(s_clauses)) if s_clauses else ""
            count, avg = conn.execute(
                "SELECT COUNT(*), AVG(MAX(1.0, (strftime('%s', logout_time) - strftime('%s', login_time)) / 60.0)) "
                f"FROM session_logs{s_where}", s_params
            ).fetchone()
            out["sessions"] = count or 0
            out["avg_session_min"] = round(avg, 1) if avg is not None else 0.0
    except Exception as e:
        print(f"[DB ERROR] Overview metrics failed: {e}")
    return out


def get_daily_activity(start=None, end=None):
    """[{'day', 'label', 'ok', 'verified', 'failed'}] per local day ('verified' is the confirmed part of 'ok');
    empty days are filled in for ranges up to ~2 months."""
    _ensure_schema()
    found = {}
    try:
        with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn:
            where, params = _command_where(start, end)
            rows = conn.execute(
                f"SELECT date(timestamp, 'localtime') AS d, SUM({_OK_CASE}), SUM({_FAIL_CASE}), "
                f"SUM({_CONFIRMED_CASE}) FROM command_logs{where} GROUP BY d ORDER BY d", params
            ).fetchall()
        found = {d: (ok or 0, bad or 0, conf or 0) for d, ok, bad, conf in rows if d}
    except Exception as e:
        print(f"[DB ERROR] Daily activity failed: {e}")

    days = sorted(found)
    try:
        if start and end:
            d0 = datetime.strptime(start, "%Y-%m-%d")
            d1 = datetime.strptime(end, "%Y-%m-%d")
            if 0 <= (d1 - d0).days <= 62:
                days = [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((d1 - d0).days + 1)]
    except ValueError:
        pass

    result = []
    for d in days:
        ok, bad, conf = found.get(d, (0, 0, 0))
        try:
            label = datetime.strptime(d, "%Y-%m-%d").strftime("%b %d")
        except ValueError:
            label = d
        result.append({"day": d, "label": label, "ok": ok, "verified": conf, "failed": bad})
    return result


def get_command_stats(start=None, end=None):
    """Per-command totals for the range, most used first:
    [{'action', 'total', 'ok', 'failed', 'rate', 'voice', 'gesture', 'last_run', 'verified', 'unverified',
      'verified_rate', 'checkable_rate'}]. 'rate' is the execution rate (everything that did not fail)."""
    _ensure_schema()
    stats = []
    try:
        with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn:
            where, params = _command_where(start, end)
            rows = conn.execute(
                f"SELECT action_name, COUNT(*), SUM({_FAIL_CASE}), SUM({_VOICE_CASE}), "
                f"MAX(datetime(timestamp, 'localtime')), SUM({_CONFIRMED_CASE}) FROM command_logs{where} "
                "GROUP BY action_name ORDER BY COUNT(*) DESC, action_name", params
            ).fetchall()
        for action, total, failed, voice, last_run, confirmed in rows:
            total, failed, voice, confirmed = total or 0, failed or 0, voice or 0, confirmed or 0
            rel = reliability_rates(confirmed, total - failed - confirmed, failed)
            stats.append({
                "action": action or "", "total": total, "failed": failed, "ok": total - failed,
                "rate": round(100.0 * (total - failed) / total, 1) if total else 0.0,
                "voice": voice, "gesture": total - voice, "last_run": last_run or "",
                "verified": confirmed, "unverified": rel["unverified"],
                "verified_rate": rel["verified_rate"], "checkable_rate": rel["checkable_rate"],
            })
    except Exception as e:
        print(f"[DB ERROR] Command stats failed: {e}")
    return stats


def get_tried_actions():
    """Set of action names that have succeeded at least once (all time, system events excluded)."""
    _ensure_schema()
    try:
        with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn:
            rows = conn.execute(
                f"SELECT DISTINCT action_name FROM command_logs WHERE COALESCE(status, 'success') != 'failed' "
                f"AND action_name NOT IN ({_SYS_PLACEHOLDERS})", list(SYSTEM_EVENT_ACTIONS)
            ).fetchall()
        return {r[0] for r in rows if r[0]}
    except Exception as e:
        print(f"[DB ERROR] Tried-actions lookup failed: {e}")
        return set()


def clear_all_analytics_logs():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM command_logs")
        cursor.execute("DELETE FROM session_logs")
        # AUTOINCREMENT never reuses ids; reset the counters so numbering restarts at 1
        cursor.execute("DELETE FROM sqlite_sequence WHERE name IN ('command_logs', 'session_logs')")
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB ERROR] Hard reset execution failed: {e}")
        return False