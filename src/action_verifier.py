import asyncio
import glob
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor


class ActionFailed(Exception):
    """Raised when an action could not do its job (the message becomes the failure reason)."""


BROWSER_EXES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe",
                "iexplore.exe", "arc.exe", "librewolf.exe", "waterfox.exe", "chromium.exe"}
DESKTOP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

# Actions that send keys to whatever window is focused, so they need a real app window in front.
KEYBOARD_TARGET_ACTIONS = {
    "zoom_in", "zoom_out", "next_tab", "prev_tab", "new_tab", "form_next_field", "form_prev_field",
    "form_select_all", "form_clear_field", "form_submit", "enter", "exit", "edit_undo", "edit_redo",
    "edit_copy", "edit_paste", "edit_delete_line", "maximize", "snap_window_left_side",
    "snap_window_right_side", "jump_timestamp",
}
APP_LAUNCH_ACTIONS = {
    "launch_paint", "launch_calculator", "launch_file_explorer", "launch_chrome", "launch_notepad",
    "start_microsoft_word", "launch_microsoft_excel", "launch_microsoft_powerpoint",
    "launch_microsoft_outlook", "launch_calendar", "launch_custom_executable", "trigger_voice_search",
}
URL_LAUNCH_ACTIONS = {
    "launch_browser_google", "launch_youtube", "launch_gemini", "launch_google_slides",
    "launch_google_docs", "launch_google_drive", "launch_custom_url",
}
LAUNCH_TIMEOUT = 8.0      # Office apps can take a while on a slow laptop


# --------------------------------------------------------------------------- #
# Windows access (ctypes only, no extra packages)
# --------------------------------------------------------------------------- #
class WindowInfo:
    def __init__(self, hwnd, title, cls, pid, exe):
        self.hwnd, self.title, self.cls, self.pid, self.exe = hwnd, title, cls, pid, exe

    @property
    def name(self):
        """Readable name for messages: the program name, else the window title."""
        if self.exe:
            return os.path.splitext(self.exe)[0].capitalize()
        return self.title or "the focused window"


class _NoWindowsAPI:
    """Used when not running on Windows: every query answers 'unknown', so no check can fail."""
    available = False

    def __getattr__(self, name):
        return lambda *a, **k: None


class _WindowsAPI:
    available = True

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        self.u, self.k = u, k
        H = wintypes.HWND
        u.GetForegroundWindow.restype = H
        u.GetWindowTextLengthW.argtypes = [H]
        u.GetWindowTextW.argtypes = [H, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [H, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [H, ctypes.POINTER(wintypes.DWORD)]
        u.IsWindow.argtypes = [H]
        u.IsWindowVisible.argtypes = [H]
        u.IsZoomed.argtypes = [H]
        u.IsIconic.argtypes = [H]
        u.GetWindowRect.argtypes = [H, ctypes.POINTER(wintypes.RECT)]
        u.GetWindow.argtypes = [H, ctypes.c_uint]
        u.GetWindow.restype = H
        u.GetClipboardSequenceNumber.restype = wintypes.DWORD
        u.CountClipboardFormats.restype = ctypes.c_int
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        self._enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, H, wintypes.LPARAM)

    # ---- windows ----
    def _text(self, hwnd):
        n = self.u.GetWindowTextLengthW(hwnd)
        buf = self.ct.create_unicode_buffer(n + 1)
        self.u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _class(self, hwnd):
        buf = self.ct.create_unicode_buffer(256)
        self.u.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _exe(self, pid):
        handle = self.k.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = self.ct.create_unicode_buffer(1024)
            size = self.wt.DWORD(len(buf))
            if self.k.QueryFullProcessImageNameW(handle, 0, buf, self.ct.byref(size)):
                return os.path.basename(buf.value).lower()
            return ""
        finally:
            self.k.CloseHandle(handle)

    def window_info(self, hwnd):
        if not hwnd:
            return None
        pid = self.wt.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, self.ct.byref(pid))
        return WindowInfo(int(hwnd), self._text(hwnd), self._class(hwnd), pid.value, self._exe(pid.value))

    def foreground(self):
        """The focused top-level window, or None (e.g. the lock screen or a UAC prompt is showing)."""
        return self.window_info(self.u.GetForegroundWindow())

    def is_open(self, hwnd):
        return bool(self.u.IsWindow(hwnd)) and bool(self.u.IsWindowVisible(hwnd)) and not self.u.IsIconic(hwnd)

    def is_maximized(self, hwnd):
        return bool(self.u.IsZoomed(hwnd))

    def rect(self, hwnd):
        r = self.wt.RECT()
        if not self.u.GetWindowRect(hwnd, self.ct.byref(r)):
            return None
        return (r.left, r.top, r.right, r.bottom)

    def visible_windows(self):
        """Handles of the visible, titled top-level windows (what you see on the taskbar, roughly)."""
        found = set()

        def callback(hwnd, _lparam):
            if self.u.IsWindowVisible(hwnd) and self.u.GetWindowTextLengthW(hwnd) > 0 \
                    and not self.u.GetWindow(hwnd, 4):          # GW_OWNER: skip dialogs/tooltips owned by others
                found.add(int(hwnd))
            return True

        self.u.EnumWindows(self._enum_proc_type(callback), 0)
        return found

    # ---- clipboard ----
    def clipboard_sequence(self):
        return int(self.u.GetClipboardSequenceNumber())

    def clipboard_empty(self):
        return self.u.CountClipboardFormats() == 0

    # ---- accessibility ----
    def high_contrast_on(self):
        class HIGHCONTRASTW(self.ct.Structure):
            _fields_ = [("cbSize", self.wt.UINT), ("dwFlags", self.wt.DWORD), ("lpszDefaultScheme", self.wt.LPWSTR)]
        hc = HIGHCONTRASTW()
        hc.cbSize = self.ct.sizeof(HIGHCONTRASTW)
        if not self.u.SystemParametersInfoW(0x0042, hc.cbSize, self.ct.byref(hc), 0):   # SPI_GETHIGHCONTRAST
            return None
        return bool(hc.dwFlags & 0x1)                                                      # HCF_HIGHCONTRASTON

    # ---- files ----
    def screenshot_folders(self):
        folders = []
        try:   # the real "Screenshots" known folder (it may be on OneDrive)
            import uuid
            ct, wt = self.ct, self.wt

            class GUID(ct.Structure):
                _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ct.c_ubyte * 8)]
            g = uuid.UUID("{b7bede81-df94-4682-a7d8-57a52620b86f}")
            guid = GUID.from_buffer_copy(g.bytes_le)
            path = ct.c_wchar_p()
            if ct.windll.shell32.SHGetKnownFolderPath(ct.byref(guid), 0, None, ct.byref(path)) == 0:
                folders.append(path.value)
                ct.windll.ole32.CoTaskMemFree(path)
        except Exception:
            pass
        home = os.path.expanduser("~")
        folders += [os.path.join(home, "Pictures", "Screenshots"),
                    os.path.join(home, "OneDrive", "Pictures", "Screenshots")]
        return list(dict.fromkeys(folders))


API = _WindowsAPI() if sys.platform == "win32" else _NoWindowsAPI()


# --------------------------------------------------------------------------- #
# Optional: volume (pycaw) and media sessions (winrt). Both run in a short-lived helper thread, because
# they need their own COM setup and must not disturb the camera/voice threads.
# --------------------------------------------------------------------------- #
def _in_helper_thread(func, timeout=2.0):
    box = {}

    def run():
        try:
            box["value"] = func()
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive() or "error" in box:
        return None
    return box.get("value")


def _volume_state_now():
    import comtypes
    comtypes.CoInitialize()
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    speakers = AudioUtilities.GetSpeakers()
    endpoint = getattr(speakers, "EndpointVolume", None)
    if endpoint is None:
        endpoint = cast(speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
    return round(float(endpoint.GetMasterVolumeLevelScalar()), 3), bool(endpoint.GetMute())


def volume_state():
    """(level 0..1, muted) or None when it cannot be read (pycaw not installed...)."""
    if sys.platform != "win32":
        return None
    return _in_helper_thread(_volume_state_now)


def _media_manager():
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
    except ImportError:
        from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
    return M


PLAYING, PAUSED = 4, 5     # GlobalSystemMediaTransportControlsSessionPlaybackStatus


def _media_state_now():
    manager_cls = _media_manager()

    async def query():
        manager = await manager_cls.request_async()
        session = manager.get_current_session()
        if session is None:
            return {"session": False}
        info = session.get_playback_info()
        title = None
        try:
            props = await session.try_get_media_properties_async()
            title = (props.title, props.artist)
        except Exception:
            pass
        return {"session": True, "status": int(info.playback_status) if info else None, "title": title,
                "app": session.source_app_user_model_id}

    return asyncio.run(query())


def media_state():
    """{'session': bool, 'status', 'title', 'app'} or None when it cannot be read (winrt not installed...)."""
    if sys.platform != "win32":
        return None
    return _in_helper_thread(_media_state_now, timeout=3.0)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def _wait_until(condition, timeout, interval=0.1):
    """True as soon as condition() is true, False if it never is within timeout seconds."""
    end = time.monotonic() + timeout
    while True:
        if condition():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(interval)


def _require_target_window(fg):
    """Pre-check for anything that types into the focused window."""
    if fg is None:
        raise ActionFailed("No window is focused, so the keys had nowhere to go")
    if fg.cls in DESKTOP_CLASSES:
        raise ActionFailed("The desktop or taskbar is focused, not an app window")
    if fg.pid == os.getpid():
        raise ActionFailed("The HCI dashboard itself was focused, so the keys went to it instead of your app. "
                           "Click the app you want to control first")


def _hotkey_keys(action_name):
    return [k.strip().lower() for k in action_name.partition(":")[2].split("+") if k.strip()]


def _validate_keys(keys):
    try:
        import pyautogui
    except Exception:
        return
    bad = [k for k in keys if not pyautogui.isValidKey(k)]
    if bad:
        raise ActionFailed(f"Unknown key name: {', '.join(bad)} (check the spelling in Settings)")


class Plan:
    """What to check after an action ran. post() raises ActionFailed if the expected effect never happened."""

    def __init__(self, post=None):
        self.post = post


def prepare(action_name, raw_phrase=""):
    """Runs the pre-check (raises ActionFailed if the action cannot work) and returns a Plan for the post-check.
    Any unexpected error inside a check means 'cannot tell' and never blocks the action."""
    try:
        return _prepare(action_name, raw_phrase)
    except ActionFailed:
        raise
    except Exception as e:
        print(f"[VERIFY] Pre-check skipped for {action_name}: {type(e).__name__}: {e}")
        return Plan()


def confirm(plan):
    """Runs the post-check. Returns (status, reason, verification):
    ('success', '', 'confirmed')   the check saw the expected effect
    ('success', '', 'unverified')  there is no check for this command, or the check could not run
    ('failed', reason, 'failed')   the check saw that nothing happened"""
    if plan is None or plan.post is None:
        return "success", "", "unverified"
    try:
        plan.post()
        return "success", "", "confirmed"
    except ActionFailed as e:
        return "failed", str(e), "failed"
    except Exception as e:
        print(f"[VERIFY] Post-check skipped: {type(e).__name__}: {e}")
        return "success", "", "unverified"


def _fail_unless(ok, reason):
    if ok is False:
        raise ActionFailed(reason)


def _prepare(a, raw_phrase):
    api = API
    post = None

    # ---- keys typed into the focused window ----
    if a.startswith("hotkey:"):
        keys = _hotkey_keys(a)
        _validate_keys(keys)
        if "win" in keys or not api.available:     # Windows shortcuts don't need an app window
            return Plan()
        fg = api.foreground()
        _require_target_window(fg)
        return Plan()

    needs_window = a in KEYBOARD_TARGET_ACTIONS or a.startswith("autofill_")
    fg = api.foreground() if api.available else None
    if needs_window and api.available:
        _require_target_window(fg)

    if a == "edit_copy":
        before = api.clipboard_sequence()
        if before is not None:
            post = lambda: _fail_unless(_wait_until(lambda: api.clipboard_sequence() != before, 1.0),
                                        "Nothing was copied - no text or item was selected")

    elif a == "edit_paste":
        if api.clipboard_empty():
            raise ActionFailed("The clipboard is empty, so there is nothing to paste")

    elif a in ("next_tab", "prev_tab", "new_tab") and fg is not None:
        title = fg.title
        reason = ("No new tab opened in " if a == "new_tab" else "The tab did not change in ") + fg.name + \
                 ("" if a == "new_tab" else " (only one tab open, or the app has no tabs)")
        post = lambda: _fail_unless(_wait_until(lambda: (api.foreground() or fg).title != title, 1.5), reason)

    elif a == "maximize" and fg is not None:
        if api.is_maximized(fg.hwnd):
            raise ActionFailed(f"{fg.name} is already maximized")
        post = lambda: _fail_unless(_wait_until(lambda: api.is_maximized(fg.hwnd), 1.0),
                                    f"{fg.name} could not be maximized")

    elif a in ("snap_window_left_side", "snap_window_right_side") and fg is not None:
        rect = api.rect(fg.hwnd)
        if rect is not None:
            post = lambda: _fail_unless(_wait_until(lambda: api.rect(fg.hwnd) != rect, 1.0),
                                        f"{fg.name} did not move (it may not support snapping)")

    elif a == "exit" and fg is not None:
        hwnd, name = fg.hwnd, fg.name
        post = lambda: _fail_unless(_wait_until(lambda: not api.is_open(hwnd), 2.0),
                                    f"{name} is still open (it may be asking to save changes)")

    elif a in ("minimize", "minimize_all_windows") and api.available:
        hwnd_before = fg.hwnd if fg else None

        def desktop_shown():
            now = api.foreground()
            return now is None or now.hwnd != hwnd_before or now.cls in DESKTOP_CLASSES
        post = lambda: _fail_unless(_wait_until(desktop_shown, 1.2), "The windows did not minimize")

    elif a == "cycle_active_applications" and fg is not None:
        hwnd_before = fg.hwnd
        post = lambda: _fail_unless(
            _wait_until(lambda: (api.foreground() or fg).hwnd != hwnd_before, 1.0),
            "There was no other window to switch to")

    elif a == "jump_timestamp" and fg is not None and fg.exe and fg.exe not in BROWSER_EXES:
        raise ActionFailed(f"Jumping to a time only works in a browser video tab ({fg.name} is focused)")

    elif a == "toggle_high_contrast_theme":
        before = api.high_contrast_on()
        if before is not None:
            post = lambda: _fail_unless(
                _wait_until(lambda: api.high_contrast_on() not in (None, before), 3.0),
                "High contrast did not change (Windows may be showing a confirmation prompt, or the "
                "shortcut is turned off in Settings > Accessibility > Contrast themes)")

    elif a == "trigger_screenshot" and api.available:
        folders = api.screenshot_folders() or []
        before = _files_in(folders)
        post = lambda: _fail_unless(_wait_until(lambda: bool(_files_in(folders) - before), 3.0, 0.2),
                                    "No screenshot was saved to Pictures > Screenshots")

    elif a == "toggle_screen_magnifier" and api.available:
        post = _expect_window_change(api, fg, 3.0, "The Magnifier did not open")

    elif a in APP_LAUNCH_ACTIONS or a in URL_LAUNCH_ACTIONS or a.startswith(("launch:", "launch_")):
        if api.available:
            post = _expect_window_change(api, fg, LAUNCH_TIMEOUT,
                                         f"Nothing opened: no new window appeared within {LAUNCH_TIMEOUT:.0f} s")

    elif a in ("volume_up", "volume_down"):
        before = volume_state()
        if before is not None:
            level, muted = before
            if a == "volume_up" and level >= 0.999 and not muted:
                raise ActionFailed("The volume is already at 100%")
            if a == "volume_down" and level <= 0.001:
                raise ActionFailed("The volume is already at 0%")
            post = lambda: _fail_unless(_wait_until(lambda: volume_state() not in (None, before), 0.8, 0.15),
                                        "The volume did not change")

    elif a in ("media_play_pause", "next_track", "previous_track"):
        before = media_state()
        if before is not None:
            if not before["session"]:
                raise ActionFailed("No app is playing or paused media right now")
            if a == "media_play_pause" and before.get("status") in (PLAYING, PAUSED):
                status = before["status"]
                post = lambda: _fail_unless(
                    _wait_until(lambda: (media_state() or {}).get("status", status) != status, 1.5, 0.25),
                    "The media app did not respond to play/pause")
            elif a == "next_track" and before.get("title"):
                title = before["title"]
                post = lambda: _fail_unless(
                    _wait_until(lambda: (media_state() or {}).get("title", title) != title, 2.5, 0.25),
                    "The track did not change (the app may not support skipping, or this was the last track)")
            # previous_track often just restarts the current song, so only the pre-check applies

    return Plan(post)


def _files_in(folders):
    files = set()
    for folder in folders:
        if os.path.isdir(folder):
            files.update(glob.glob(os.path.join(folder, "*")))
    return files


def _expect_window_change(api, fg_before, timeout, reason):
    """Post-check for launches: a new window appeared, or another window/tab came to the front."""
    windows_before = api.visible_windows() or set()
    hwnd_before = fg_before.hwnd if fg_before else None
    title_before = fg_before.title if fg_before else None

    def changed():
        if (api.visible_windows() or set()) - windows_before:
            return True
        now = api.foreground()
        return now is not None and (now.hwnd != hwnd_before or now.title != title_before)

    return lambda: _fail_unless(_wait_until(changed, timeout, 0.25), reason)


# --------------------------------------------------------------------------- #
# Background post-checks
# --------------------------------------------------------------------------- #
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="verify")


def confirm_later(plan, on_done):
    """Runs the post-check off the calling thread, then calls on_done(status, reason, verification)."""
    def job():
        on_done(*confirm(plan))
    _executor.submit(job)