import os
import sys
import json
import inspect
import threading
import time
import queue
import tkinter as tk
import customtkinter as ctk
from report_dashboard import ReportDashboard
from camera_preview import CustomCameraPreview

# Set before any window exists, so the dashboard is created dark instead of being restyled (and flickering)
# after it appears.
ctk.set_appearance_mode("dark")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(SCRIPT_DIR, 'config')):
    BASE_DIR = SCRIPT_DIR
else:
    BASE_DIR = os.path.dirname(SCRIPT_DIR)

sys.path.append(os.path.join(BASE_DIR, "src"))

from vision_module import run_hand_tracker
import vision_module
import voice_module
from voice_module import run_voice_loop
from hud_overlay import start_hud_process  

class AppShortcutEditorWindow(ctk.CTkToplevel):
    def __init__(self, parent_settings, app_path, config_data):
        super().__init__(parent_settings)
        self.parent_settings = parent_settings
        self.app_path = app_path
        self.config_data = config_data
        self.shortcut_rows = []

        app_filename = os.path.basename(app_path) if app_path else "New Application"
        self.title(f"Edit Rules: {app_filename}")
        self.geometry("540x480")
        self.transient(parent_settings)
        self.grab_set()

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(bar, text="Inner Workspace Macros:", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkButton(bar, text="+ Add Macro", width=90, fg_color="#393676", command=self.append_blank_macro_row).pack(side="right")

        self.scroll = ctk.CTkScrollableFrame(self, width=500, height=280)
        self.scroll.pack(padx=20, pady=5, fill="both", expand=True)

        for hotkey, phrases in config_data.get("shortcuts", {}).items():
            self.render_macro_row(hotkey, ", ".join(phrases))

        ctrl = ctk.CTkFrame(self, fg_color="transparent")
        ctrl.pack(fill="x", padx=20, pady=15)
        ctk.CTkButton(ctrl, text="Confirm Sub-Config", fg_color="#2baf54", command=self.apply_sub_config).pack(side="right")
        ctk.CTkButton(ctrl, text="Discard", fg_color="#7f8c8d", command=self.destroy).pack(side="left")

    def render_macro_row(self, hotkey, phrases):
        row = ctk.CTkFrame(self.scroll, fg_color="#1F1F2E")
        row.pack(fill="x", padx=5, pady=4)

        e_key = ctk.CTkEntry(row, width=120, placeholder_text="Hotkey (ctrl+t)")
        if hotkey:
            e_key.insert(0, hotkey)
        e_key.pack(side="left", padx=5, pady=5)

        e_phrase = ctk.CTkEntry(row, width=240, placeholder_text="Phrases...")
        if phrases:
            e_phrase.insert(0, phrases)
        e_phrase.pack(side="left", fill="x", expand=True, padx=5)

        container = {"frame": row, "key_widget": e_key, "phrase_widget": e_phrase}
        ctk.CTkButton(row, text="🗑", width=30, fg_color="#ff1744", command=lambda: self.drop_macro(row, container)).pack(side="right", padx=5)
        self.shortcut_rows.append(container)

    def append_blank_macro_row(self): self.render_macro_row("", "")
    def drop_macro(self, f, c): f.destroy(); self.shortcut_rows.remove(c)

    def apply_sub_config(self):
        """Compiles changes locally back to parent rows variables."""
        compiled_shortcuts = {}
        for row in self.shortcut_rows:
            k = row["key_widget"].get().strip().lower()
            txt = row["phrase_widget"].get().strip()
            if k and txt:
                compiled_shortcuts[k] = [p.strip().lower() for p in txt.split(",") if p.strip()]
        
        self.config_data["shortcuts"] = compiled_shortcuts
        self.destroy()

# ======================================================================
# GESTURE HAND MODEL (static preview used by the capture popup, the 👁 list button and the duplicate dialog)
# ======================================================================
ALL_FIVE = ("thumb", "index", "middle", "ring", "pinky")
FOUR_FINGERS = ("index", "middle", "ring", "pinky")

# Built-in gestures have no stored landmarks, so they are drawn from a template hand.
#   fingers = highlighted tips, pose = fingers drawn extended (defaults to fingers),
#   touch = tips drawn meeting, flip = hand upside down, trail = direction of motion (ghost copies behind the hand)
BUILTIN_PREVIEWS = {
    "fist": dict(fingers=()),
    "index_point": dict(fingers=("index",)),
    "victory_sign": dict(fingers=("index", "middle")),
    "two_finger_scroll": dict(fingers=("index", "middle")),
    "pinch_zoom_in": dict(fingers=("thumb", "index"), touch=("thumb", "index")),
    "l_zoom_out": dict(fingers=("thumb", "index")),
    "three_finger_pinch": dict(fingers=("thumb", "index", "middle"), touch=("thumb", "index", "middle")),
    "thumb_up": dict(fingers=("thumb",)),
    "thumb_down": dict(fingers=("thumb",), flip=True),
    "open_hand": dict(fingers=ALL_FIVE),
    "swipe_up": dict(fingers=FOUR_FINGERS, pose=ALL_FIVE, trail=(0, -1)),
    "swipe_down": dict(fingers=FOUR_FINGERS, pose=ALL_FIVE, trail=(0, 1)),
    "swipe_left": dict(fingers=FOUR_FINGERS, pose=ALL_FIVE, trail=(-1, 0)),
    "swipe_right": dict(fingers=FOUR_FINGERS, pose=ALL_FIVE, trail=(1, 0)),
}
BUILTIN_PREVIEW_ALIASES = {"thumbs_up": "thumb_up", "thumbs_down": "thumb_down"}


def get_builtin_preview(gesture_name):
    key = str(gesture_name).strip().lower().replace(" ", "_")
    return BUILTIN_PREVIEWS.get(BUILTIN_PREVIEW_ALIASES.get(key, key))


def build_template_hand(extended, touch=None, flip=False):
    """Returns 21 (x, y) landmarks of an upright template hand (palm length = 1, y grows downward)."""
    bases = {"index": (-0.30, -0.95), "middle": (0.0, -1.0), "ring": (0.28, -0.95), "pinky": (0.52, -0.82)}
    lens = {"index": (0.30, 0.22, 0.20), "middle": (0.33, 0.24, 0.20), "ring": (0.30, 0.22, 0.19), "pinky": (0.22, 0.16, 0.15)}
    fan = {"index": -0.18, "middle": 0.02, "ring": 0.08, "pinky": 0.16}
    first_joint = {"index": 5, "middle": 9, "ring": 13, "pinky": 17}

    pts = [(0.0, 0.0)] * 21
    if "thumb" in extended:
        pts[1:5] = [(-0.35, -0.25), (-0.58, -0.42), (-0.78, -0.60), (-0.96, -0.78)]
    else:
        pts[1:5] = [(-0.35, -0.25), (-0.42, -0.45), (-0.25, -0.70), (-0.10, -0.90)]

    for finger, i in first_joint.items():
        bx, by = bases[finger]
        pts[i] = (bx, by)
        if finger in extended:
            x, y = bx, by
            for k, seg in enumerate(lens[finger]):
                x += fan[finger] * seg
                y -= seg
                pts[i + 1 + k] = (x, y)
        else:
            pip = (bx + fan[finger] * 0.1, by - 0.20)
            dip = (pip[0], pip[1] + 0.12)
            pts[i + 1:i + 4] = [pip, dip, (dip[0], dip[1] + 0.10)]

    if touch:
        tip_idx = {"thumb": 4, "index": 8, "middle": 12, "ring": 16, "pinky": 20}
        others = [f for f in touch if f != "thumb"]
        if others:
            tx = sum(pts[tip_idx[f]][0] for f in others) / len(others)
            ty = sum(pts[tip_idx[f]][1] for f in others) / len(others)
            for n, f in enumerate(touch):
                tip = tip_idx[f]
                new_tip = (tx + 0.02 * n, ty)
                prev = pts[tip - 2]
                pts[tip] = new_tip
                pts[tip - 1] = ((prev[0] + new_tip[0]) / 2, (prev[1] + new_tip[1]) / 2)

    if flip:
        pts = [(x, -y) for x, y in pts]
    return pts


class GesturePreviewPanel(ctk.CTkFrame):
    """Static hand model: dark silhouette, ripple-ringed glowing fingertips, optional fading ghost trail for motion."""
    BG = "#12121A"
    SKIN = "#2A2A44"
    EDGE = "#3C3C5E"
    FINGER_ORDER = ("thumb", "index", "middle", "ring", "pinky")
    TIP_IDS = {"thumb": 4, "index": 8, "middle": 12, "ring": 16, "pinky": 20}
    DEFAULT_COLORS = {"thumb": "#00FF00", "index": "#0000FF", "middle": "#FF00FF", "ring": "#00FFFF", "pinky": "#FFFFFF"}
    CHAINS = ((1, 2, 3, 4), (5, 6, 7, 8), (9, 10, 11, 12), (13, 14, 15, 16), (17, 18, 19, 20))
    GHOST_OPACITY = (0.50, 0.38, 0.26, 0.16)  # nearest -> farthest copy, blended toward the card color

    def __init__(self, parent, colors, width=340, height=260):
        super().__init__(parent, fg_color=self.BG, corner_radius=16)
        self.colors = colors
        self.w, self.h = width, height
        self.canvas = ctk.CTkCanvas(self, width=width, height=height, bg=self.BG, highlightthickness=0)
        self.canvas.pack(padx=10, pady=(10, 2))
        self.readout = ctk.CTkLabel(self, text="", font=ctk.CTkFont(family="Consolas", size=10),
                                    text_color="#A2A1B6", justify="center", wraplength=width)
        self.readout.pack(pady=(0, 10))

    @staticmethod
    def _blend(fg, bg, t):
        f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02X%02X%02X" % tuple(int(round(b[i] + (f[i] - b[i]) * t)) for i in range(3))

    def _tip_color(self, finger):
        return SettingsWindow._norm_hex(self.colors.get(finger, "")) or self.DEFAULT_COLORS[finger]

    def show_message(self, text):
        self.canvas.delete("all")
        self.canvas.create_text(self.w / 2, self.h / 2, text=text, fill="#6C6B80", font=("Consolas", 10), width=self.w - 40, justify="center")
        self.readout.configure(text="")

    def show(self, pts, extended, caption="", trail=None):
        """pts = 21 (x, y) landmarks in any units, extended = set of finger names to highlight, trail = (dx, dy) or None."""
        import math
        c = self.canvas
        c.delete("all")

        ux = uy = 0.0
        if trail:
            norm = math.hypot(*trail) or 1.0
            ux, uy = trail[0] / norm, trail[1] / norm

        # fit the hand into the canvas; with a trail, reserve room behind it for the ghost copies
        pad = 52
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        bw, bh = (max(xs) - min(xs)) or 1e-3, (max(ys) - min(ys)) or 1e-3
        avail_w, avail_h = self.w - 2 * pad, self.h - 2 * pad
        cx, cy = self.w / 2, self.h / 2
        step = 0.0
        if trail:
            horizontal = abs(ux) >= abs(uy)
            step = 0.09 * (self.w if horizontal else self.h)
            reserve = step * len(self.GHOST_OPACITY)
            if horizontal:
                avail_w -= reserve
                cx += ux * reserve / 2
            else:
                avail_h -= reserve
                cy += uy * reserve / 2
        s = min(avail_w / bw, avail_h / bh)
        mx, my = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        px = [(cx + (x - mx) * s, cy + (y - my) * s) for x, y in pts]

        fw = max(8.0, 0.21 * math.hypot(px[9][0] - px[0][0], px[9][1] - px[0][1]))

        if trail:
            for k in range(len(self.GHOST_OPACITY), 0, -1):
                a = self.GHOST_OPACITY[k - 1]
                moved = [(x - ux * step * k, y - uy * step * k) for x, y in px]
                self._silhouette(moved, fw, self._blend(self.SKIN, self.BG, a), self._blend(self.EDGE, self.BG, a))
        self._silhouette(px, fw, self.SKIN, self.EDGE)

        for finger in self.FINGER_ORDER:
            if finger in extended:
                self._tip(px[self.TIP_IDS[finger]], self._tip_color(finger), fw)

        self.readout.configure(text=caption)

    def _silhouette(self, px, fw, fill, edge):
        c = self.canvas

        def flat(idx):
            return [v for i in idx for v in px[i]]

        for width, color in ((fw + 4, edge), (fw, fill)):
            for chain in self.CHAINS:
                c.create_line(*flat(chain), fill=color, width=width, capstyle="round", joinstyle="round")
            for i in (1, 5, 9, 13, 17):
                c.create_line(*flat((0, i)), fill=color, width=width * 1.5, capstyle="round")
            c.create_polygon(*flat((0, 1, 5, 9, 13, 17)), fill=color, outline=color)

    def _tip(self, pos, color, fw):
        c = self.canvas
        x, y = pos
        r = max(7, int(fw * 0.45))
        for grow, strength in ((24, 0.22), (16, 0.40), (9, 0.65)):  # ripple rings
            c.create_oval(x - r - grow, y - r - grow, x + r + grow, y + r + grow,
                          outline=self._blend(color, self.BG, strength), width=2)
        c.create_oval(x - r - 4, y - r - 4, x + r + 4, y + r + 4, fill=self._blend(color, self.BG, 0.35), outline="")
        c.create_oval(x - r, y - r, x + r, y + r, fill=color, outline="#FFFFFF", width=2)


class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, parent_dashboard):
        super().__init__()
        self.parent_dashboard = parent_dashboard
        self.title("Customize Command Settings")

        win_w = min(920, self.winfo_screenwidth() - 40)
        win_h = min(740, self.winfo_screenheight() - 90)
        self.geometry(f"{win_w}x{win_h}")
        self.minsize(860, 640)
        self.transient(parent_dashboard)  
        self.grab_set()   

        self.dynamic_apps_ui_rows = []
        self.dynamic_shortcuts_ui_rows = []
        self.gesture_dropdown_map = {}
        self.voice_text_map = {}

        # Calibration camera state (worker thread -> UI thread hand-off)
        self._calib_lock = threading.Lock()
        self._calib_generation = 0
        self._calib_thread = None
        self._calib_poll_job = None
        self._calib_frame = None
        self._calib_telemetry = None
        self._calib_error = None
        self._calib_ctk_image = None
        self.last_detected_landmarks_cache = None
        self.is_calibration_stream_active = False
        self.webcam_capture_device = None
        self._calib_notice_until = 0.0

        self.available_gesture_actions = [
            "media_play_pause", "volume_up", "volume_down", 
            "previous_track", "next_track", "trigger_screenshot", 
            "wake_gate", "two_finger_scroll", "system_idle"
        ]
        
        self.selected_finger = "thumb" 
        self.custom_colors = {
            "thumb": "#00FF00", "index": "#0000FF", 
            "middle": "#FF00FF", "ring": "#00FFFF", "pinky": "#FFFFFF"
        }

        self.protocol("WM_DELETE_WINDOW", self.on_window_close_cleanup)

        self.setup_settings_ui()

    def setup_settings_ui(self):
        gesture_path = os.path.join(BASE_DIR, 'config', 'gestures.json')
        voice_path = os.path.join(BASE_DIR, 'config', 'voice_commands.json')

        try:
            with open(gesture_path, 'r') as f:
                data = json.load(f)
            if "colors" in data:
                self.custom_colors.update(data["colors"])
        except Exception:
            pass

        title_label = ctk.CTkLabel(self, text="Advanced Multimodal Command Configurations", font=ctk.CTkFont(size=15, weight="bold"))
        title_label.pack(pady=(12, 2))

        self.tab_view = ctk.CTkTabview(self, width=860, height=600)
        self.tab_view.pack(padx=15, pady=5, fill="both", expand=True)

        self.tab_view.add("Dynamic Launchers")
        self.tab_view.add("Hand Gestures")
        self.tab_view.add("Voice Commands")
        self.tab_view.add("Finger Colors")
        self.tab_view.add("Profile Autofill")

        # ----------------------------------------------------
        # TAB 1 PANEL: DYNAMIC EXECUTABLE LAUNCHERS
        # ----------------------------------------------------
        launch_tab = self.tab_view.tab("Dynamic Launchers")
        l_bar = ctk.CTkFrame(launch_tab, fg_color="transparent")
        l_bar.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(l_bar, text="Map target executable binaries to custom voice tags:", font=ctk.CTkFont(size=11, slant="italic")).pack(side="left")
        ctk.CTkButton(l_bar, text="+ Add App Link", width=120, fg_color="#393676", hover_color="#55529e", command=self.add_blank_launcher_row).pack(side="right")
        
        self.launcher_scroll = ctk.CTkScrollableFrame(launch_tab)
        self.launcher_scroll.pack(fill="both", expand=True, padx=5, pady=5)

        # ----------------------------------------------------
        # TAB 2 PANEL: HAND GESTURES (live camera + pose capture + gesture/command list)
        # ----------------------------------------------------
        gesture_tab = self.tab_view.tab("Hand Gestures")

        cam_section = ctk.CTkFrame(gesture_tab, fg_color="#12121A", corner_radius=15)
        cam_section.pack(fill="x", padx=8, pady=(8, 4))

        self.camera_feed_lbl = ctk.CTkLabel(
            cam_section, width=320, height=236,
            text="[ WEBCAM CORE STANDBY ]\n\nStart the video feed to track and capture poses.",
            font=ctk.CTkFont(family="Consolas", size=11), text_color="#6C6B80"
        )
        self.camera_feed_lbl.pack(side="left", padx=10, pady=10)

        cam_controls = ctk.CTkFrame(cam_section, fg_color="transparent")
        cam_controls.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        self.toggle_feed_btn = ctk.CTkButton(cam_controls, text="📹 Start Video Feed", fg_color="#393676", hover_color="#55529e", command=self.toggle_calibration_camera_thread)
        self.toggle_feed_btn.pack(fill="x", pady=5)

        self.capture_pose_btn = ctk.CTkButton(cam_controls, text="📸 Capture Pose as New Gesture", fg_color="#2baf54", hover_color="#228b42", state="disabled", command=self.prompt_save_captured_custom_pose)
        self.capture_pose_btn.pack(fill="x", pady=5)

        self.vision_telemetry_lbl = ctk.CTkLabel(cam_controls, text="Tendon Frame State: IDLE\nLandmark Array Hash: Null", font=ctk.CTkFont(family="Consolas", size=10), justify="left", anchor="w", wraplength=300, text_color="#A2A1B6")
        self.vision_telemetry_lbl.pack(pady=10, fill="x", anchor="w")

        g_bar = ctk.CTkFrame(gesture_tab, fg_color="transparent")
        g_bar.pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(g_bar, text="Saved gestures -> command (add new ones with the camera above, 👁 shows a gesture):", font=ctk.CTkFont(size=11, slant="italic")).pack(side="left")

        self.gesture_scroll = ctk.CTkScrollableFrame(gesture_tab, height=170)
        self.gesture_scroll.pack(fill="both", expand=True, padx=5, pady=5)

        self.gesture_ui_rows = []
        try:
            with open(gesture_path, 'r') as f:
                gesture_data = json.load(f)
            for g in gesture_data.get('gestures', []):
                self.render_gesture_row(g)
        except Exception as e:
            ctk.CTkLabel(self.gesture_scroll, text=f"Error parsing gestures data: {e}").pack()

        # ----------------------------------------------------
        # TAB 3 PANEL: HARDCODED VOICE COMMANDS
        # ----------------------------------------------------
        voice_tab = self.tab_view.tab("Voice Commands")
        v_bar = ctk.CTkFrame(voice_tab, fg_color="transparent")
        v_bar.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(v_bar, text="Map an action to voice phrases (separate phrases with commas):", font=ctk.CTkFont(size=11, slant="italic")).pack(side="left")
        ctk.CTkButton(v_bar, text="+ Add Command", width=120, fg_color="#393676", hover_color="#55529e", command=self.add_blank_voice_row).pack(side="right")

        self.voice_scroll = ctk.CTkScrollableFrame(voice_tab)
        self.voice_scroll.pack(fill="both", expand=True, padx=5, pady=5)

        self.voice_ui_rows = []
        self.known_voice_actions = []
        try:
            with open(voice_path, 'r') as f:
                voice_data = json.load(f)
            commands_dict = voice_data.get('voice_commands', {})
            self.known_voice_actions = list(commands_dict.keys())
            for action_key, phrases_list in commands_dict.items():
                self.render_voice_row(action_key, ", ".join(phrases_list))
        except Exception as e:
            ctk.CTkLabel(self.voice_scroll, text=f"Error parsing voice mappings: {e}").pack()

        # ----------------------------------------------------
        # TAB 4 PANEL: FINGER COLORS
        # ----------------------------------------------------
        self._build_finger_color_tab(self.tab_view.tab("Finger Colors"))




        # ----------------------------------------------------
        # TAB 5 PANEL: PROFILE VAULT SECURE STORAGE
        # ----------------------------------------------------
        profile_tab = self.tab_view.tab("Profile Autofill")
        self.profile_scroll = ctk.CTkScrollableFrame(profile_tab)
        self.profile_scroll.pack(fill="both", expand=True, padx=5, pady=5)
        
        vault_notice = ctk.CTkLabel(self.profile_scroll, text="🔒 Safe Profile Vault: Say 'Fill [Field Name]' aloud to autofill lines.", font=ctk.CTkFont(size=11, slant="italic"), text_color="#00e676")
        vault_notice.pack(pady=(0, 15))

        import database_manager
        self.profile_field_entries = {}
        target_profile_keys = ["first_name", "middle_name", "last_name", "email", "phone_number", "address"]

        for field_key in target_profile_keys:
            p_lbl = ctk.CTkLabel(self.profile_scroll, text=f"{field_key.upper().replace('_', ' ')}:", font=ctk.CTkFont(weight="bold"))
            p_lbl.pack(anchor="w", padx=15, pady=(8, 2))

            saved_data = database_manager.get_profile_data(field_key)
            display_value = saved_data[0] if saved_data and isinstance(saved_data, tuple) else (saved_data if saved_data else "")
            
            p_entry = ctk.CTkEntry(self.profile_scroll, width=440, placeholder_text=f"Enter {field_key.replace('_', ' ')}")
            p_entry.insert(0, display_value)
            p_entry.pack(padx=15, pady=(0, 10), fill="x")
            self.profile_field_entries[field_key] = p_entry

        self.load_settings_payload_from_disk(voice_path)

        self.changes_tracker_lbl = ctk.CTkLabel(self, text="ℹ️ Changes apply after Save All Changes (restart the engine to reload gestures).", font=ctk.CTkFont(size=11, slant="italic"), text_color="#A2A1B6")
        self.changes_tracker_lbl.pack(fill="x", side="bottom", pady=(0, 5))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", pady=10, padx=20, side="bottom")
        save_btn = ctk.CTkButton(btn_frame, text="Save All Changes", fg_color="#2baf54", hover_color="#228b42", command=self.save_all_settings)
        save_btn.pack(side="right", padx=5)
        cancel_btn = ctk.CTkButton(btn_frame, text="Cancel", fg_color="#7f8c8d", hover_color="#95a5a6", command=self.destroy)
        cancel_btn.pack(side="left", padx=5)

    def toggle_calibration_camera_thread(self):
        """Starts/stops the calibration camera. The worker thread only grabs + analyses frames; the UI thread renders them."""
        if not self.is_calibration_stream_active:
            self._start_calibration_stream()
        else:
            self._safely_kill_video_hardware()

    def _start_calibration_stream(self):
        old_thread = self._calib_thread
        if old_thread is not None and old_thread.is_alive():
            # Give the previous worker a moment to release the camera before reopening it.
            old_thread.join(timeout=1.5)
            if old_thread.is_alive():
                self.vision_telemetry_lbl.configure(text="⏳ Camera is still releasing, try again in a moment.", text_color="#ff1744")
                return

        self.is_calibration_stream_active = True
        self._calib_generation += 1
        generation = self._calib_generation
        with self._calib_lock:
            self._calib_frame = None
            self._calib_telemetry = None
            self._calib_error = None

        self.toggle_feed_btn.configure(text="■ Stop Video Feed", fg_color="#ff1744", hover_color="#ff4f70")
        self.capture_pose_btn.configure(state="normal")

        self._calib_thread = threading.Thread(target=self._run_live_webcam_capture_loop, args=(generation,), daemon=True)
        self._calib_thread.start()
        self._poll_calibration_frame()

    def _poll_calibration_frame(self):
        """Runs on the UI thread: pulls the newest frame/telemetry produced by the worker and renders it."""
        self._calib_poll_job = None
        if not self.is_calibration_stream_active:
            return

        with self._calib_lock:
            frame = self._calib_frame
            telemetry = self._calib_telemetry
            error = self._calib_error
            self._calib_frame = None
            self._calib_telemetry = None

        if error:
            self._safely_kill_video_hardware()
            self.vision_telemetry_lbl.configure(text=f"❌ {error}", text_color="#ff1744")
            return

        try:
            if frame is not None:
                if self._calib_ctk_image is None:
                    self._calib_ctk_image = ctk.CTkImage(light_image=frame, dark_image=frame, size=(320, 236))
                    self.camera_feed_lbl.configure(image=self._calib_ctk_image, text="")
                else:
                    # Reuse one CTkImage instead of creating a new one every frame.
                    self._calib_ctk_image.configure(light_image=frame, dark_image=frame)
            if telemetry is not None and time.monotonic() >= self._calib_notice_until:
                self.vision_telemetry_lbl.configure(text=telemetry[0], text_color=telemetry[1])
        except Exception as e:
            print(f"[CALIBRATION] Render error, stopping feed: {e}")
            self._safely_kill_video_hardware()
            return

        self._calib_poll_job = self.after(33, self._poll_calibration_frame)

    def _run_live_webcam_capture_loop(self, generation):
        """Worker thread: captures frames and runs MediaPipe tracking. Never touches Tk widgets directly."""
        def is_current():
            return self.is_calibration_stream_active and generation == self._calib_generation

        def post_error(message):
            with self._calib_lock:
                if generation == self._calib_generation:
                    self._calib_error = message

        def local_hex_to_bgr(hex_str):
            hex_str = hex_str.lstrip('#')
            return tuple(int(hex_str[i:i+2], 16) for i in (4, 2, 0))

        cap = None
        detector = None
        try:
            import cv2
            from PIL import Image
            import mediapipe as mp
            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision

            task_path = os.path.join(BASE_DIR, 'hand_landmarker.task')
            with open(task_path, 'rb') as f:
                model_bytes = f.read()

            options = vision.HandLandmarkerOptions(
                base_options=python.BaseOptions(model_asset_buffer=model_bytes),
                running_mode=vision.RunningMode.VIDEO,
                num_hands=1,
                min_hand_detection_confidence=0.75,
                min_tracking_confidence=0.75
            )
            detector = vision.HandLandmarker.create_from_options(options)

            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if not cap.isOpened():
                post_error("Could not open the webcam. Is the main engine or another app using it?")
                return
            self.webcam_capture_device = cap

            start_time = time.monotonic()
            last_ts = -1
            failed_reads = 0

            while is_current():
                success, frame = cap.read()
                if not success:
                    failed_reads += 1
                    if failed_reads > 100:
                        post_error("Webcam stopped returning frames.")
                        return
                    time.sleep(0.03)
                    continue
                failed_reads = 0

                frame = cv2.flip(frame, 1)
                frame = cv2.resize(frame, (460, 340))
                h, w, _ = frame.shape

                colors = dict(self.custom_colors)

                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)

                # MediaPipe VIDEO mode requires strictly increasing timestamps.
                ts = int((time.monotonic() - start_time) * 1000)
                if ts <= last_ts:
                    ts = last_ts + 1
                last_ts = ts

                detection_result = detector.detect_for_video(mp_image, ts)

                if detection_result and getattr(detection_result, 'hand_landmarks', None):
                    hand_points = detection_result.hand_landmarks[0]
                    self.last_detected_landmarks_cache = hand_points

                    pts = [(lm.x, lm.y) for lm in hand_points]
                    info = self._analyze_hand(pts)
                    extended = info["extended"]

                    # Only fingertips that are actually extended are drawn, each in its configured color.
                    for name in self.FINGER_ORDER:
                        if not extended[name]:
                            continue
                        tip_lm = hand_points[self.FINGER_TIPS[name]]
                        tx, ty = int(tip_lm.x * w), int(tip_lm.y * h)
                        bgr_shade = local_hex_to_bgr(colors.get(name, self.DEFAULT_FINGER_COLORS[name]))
                        cv2.circle(frame, (tx, ty), 10, bgr_shade, -1)
                        cv2.circle(frame, (tx, ty), 12, (255, 255, 255), 1)

                    active_fingers = [n.upper() for n in self.FINGER_ORDER if extended[n]]
                    detected_str = ", ".join(active_fingers) if active_fingers else "NONE (FIST)"
                    cv2.putText(frame, info["label"], (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 1)

                    telemetry = (
                        f"📡 HAND ENGINE STATE: TRACKING ACTIVE\n"
                        f" • Active Tips: {detected_str}\n"
                        f" • Detected Gesture: {info['label']}\n"
                        f" • Calibration Scale Factor: {info['scale']:.3f}",
                        "#00e676"
                    )
                else:
                    self.last_detected_landmarks_cache = None
                    telemetry = ("📡 HAND ENGINE STATE: SEARCHING...\nLandmark Array Hash: Null", "#ff1744")

                cv2.putText(frame, "LIVE CALIBRATION ENGINE ROUTINE ACTIVE", (20, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)

                img_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

                # Hand the newest frame to the UI thread (older unrendered frames are simply dropped).
                with self._calib_lock:
                    if generation == self._calib_generation:
                        self._calib_frame = img_pil
                        self._calib_telemetry = telemetry

                time.sleep(0.03)
        except Exception as e:
            print(f"[CALIBRATION] Worker error: {e}")
            post_error(f"Calibration camera error: {e}")
        finally:
            try:
                if detector is not None:
                    detector.close()
            except Exception:
                pass
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass
            if self.webcam_capture_device is cap:
                self.webcam_capture_device = None

    # ------------------------------------------------------------------
    # POSE ANALYSIS HELPERS (shared by the live feed and the capture check)
    # ------------------------------------------------------------------
    FINGER_TIPS = {"thumb": 4, "index": 8, "middle": 12, "ring": 16, "pinky": 20}

    POSE_LABELS = {
        "pinch_zoom_in": "PINCH (ZOOM IN)",
        "l_zoom_out": "L-SHAPE (ZOOM OUT)",
        "three_finger_pinch": "3-FINGER PINCH (WRITING TOGGLE)",
        "fist": "FIST",
        "index_point": "INDEX POINT (WAKE)",
        "victory_sign": "VICTORY SIGN",
        "two_finger_scroll": "TWO-FINGER SCROLL",
        "thumb_up": "THUMB UP",
        "thumb_down": "THUMB DOWN",
        "open_hand": "OPEN HAND",
        "unmapped": "NEW POSE (not a built-in)",
    }
    # Built-in poses vision_module hard-codes behaviour for; a custom pose matching these would shadow them.
    RESERVED_BUILTIN_POSES = {
        "index_point": "the wake gesture",
        "three_finger_pinch": "the writing-mode toggle",
    }

    @staticmethod
    def _analyze_hand(pts):
        """pts = 21 (x, y) tuples. Mirrors the static-pose rules in vision_module.run_hand_tracker
        (swipes are motion-based, so they are not handled here)."""
        import math

        def d(a, b):
            return math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1])

        scale = d(0, 9) or 1.0
        extended = {}
        for name, tip, mcp in (("index", 8, 5), ("middle", 12, 9), ("ring", 16, 13), ("pinky", 20, 17)):
            extended[name] = d(tip, 0) > d(mcp, 0) + 0.15 * scale
        thumb_open = d(4, 17) / scale > 0.65
        extended["thumb"] = thumb_open

        idx, mid, ring, pinky = (extended[n] for n in ("index", "middle", "ring", "pinky"))
        all_closed = not (idx or mid or ring or pinky)
        three_closed = not (mid or ring or pinky)
        two_closed = not (ring or pinky)
        gap_index = d(4, 8) / scale
        gap_middle = d(4, 12) / scale
        thumb_gap = d(4, 5) / scale

        if gap_index < 0.25 and idx and three_closed:
            key = "pinch_zoom_in"
        elif thumb_open and idx and three_closed and gap_index >= 0.25:
            key = "l_zoom_out"
        elif gap_index < 0.28 and gap_middle < 0.28 and idx and mid and two_closed:
            key = "three_finger_pinch"
        elif all_closed and thumb_gap <= 0.28:
            key = "fist"
        elif idx and three_closed:
            key = "index_point"
        elif idx and mid and two_closed:
            key = "victory_sign" if d(8, 12) / scale > 0.40 else "two_finger_scroll"
        elif thumb_open and all_closed:
            key = "thumb_up" if pts[4][1] <= pts[0][1] else "thumb_down"
        elif idx and mid and ring and pinky:
            key = "open_hand"
        else:
            key = "unmapped"

        return {"scale": scale, "extended": extended, "key": key, "label": SettingsWindow.POSE_LABELS[key]}

    def _pose_match_threshold(self):
        """Same threshold the engine uses for custom poses."""
        try:
            with open(os.path.join(BASE_DIR, 'config', 'gestures.json'), 'r') as f:
                return float(json.load(f).get('pose_match_threshold', 0.25))
        except Exception:
            return 0.25

    def find_existing_gesture(self, snapshot):
        """Returns (row, reserved_key). row = gesture list entry that already owns this pose (or None);
        reserved_key is set when the pose is a hard-coded built-in that cannot be reassigned."""
        import numpy as np

        # 1) Custom poses: identical test to vision_module.match_custom_pose (same normalization, metric, threshold).
        current = vision_module.normalize_pose(snapshot)
        threshold = self._pose_match_threshold()
        best_row, best_dist = None, threshold
        if current is not None:
            for row in self.gesture_ui_rows:
                saved = (row.get("extra") or {}).get("landmarks")
                if not saved or len(saved) != 21:
                    continue
                ref = vision_module.normalize_pose(saved)
                if ref is None:
                    continue
                dist = float(np.mean(np.linalg.norm(current - ref, axis=1)))
                if dist < best_dist:
                    best_row, best_dist = row, dist
        if best_row is not None:
            return best_row, None

        # 2) Built-in poses: the engine looks the detected pose up by gesture name in gestures.json.
        key = self._analyze_hand([(p.x, p.y) for p in snapshot])["key"]
        if key in self.RESERVED_BUILTIN_POSES:
            return None, key
        for row in self.gesture_ui_rows:
            if row["name_widget"].get().strip().lower().replace(" ", "_") == key:
                return row, None
        return None, None

    # ------------------------------------------------------------------
    # TAB 4 HELPERS: FINGER COLORS
    # ------------------------------------------------------------------
    FINGER_ORDER = ["thumb", "index", "middle", "ring", "pinky"]
    DEFAULT_FINGER_COLORS = {
        "thumb": "#00FF00", "index": "#0000FF",
        "middle": "#FF00FF", "ring": "#00FFFF", "pinky": "#FFFFFF"
    }
    PRESET_SWATCHES = [
        "#FF1744", "#FF9100", "#FFEA00", "#00E676", "#00E5FF",
        "#2979FF", "#7C4DFF", "#D500F9", "#FF4081", "#FFFFFF"
    ]
    WHEEL_PX = 220
    WHEEL_R = 96
    HAND_BG = "#12121A"

    @staticmethod
    def _norm_hex(value):
        """Returns '#RRGGBB' (uppercase) for a valid 6-digit hex string, else None."""
        import re
        v = str(value).strip()
        if re.fullmatch(r"#?[0-9a-fA-F]{6}", v):
            return "#" + v.lstrip("#").upper()
        return None

    @staticmethod
    def _blend_hex(fg, bg, t):
        """Mixes fg over bg by t (0..1); used for soft glows against the dark card."""
        f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
        r, g, bl = (int(round(b[i] + (f[i] - b[i]) * t)) for i in range(3))
        return f"#{r:02X}{g:02X}{bl:02X}"

    def _build_finger_color_tab(self, color_tab):
        self._pulse_phase = 0.0
        self._pulse_job = None
        self.wheel_val = 1.0
        self._pick_h, self._pick_s = 0.0, 0.0
        self._wheel_photo = None

        head = ctk.CTkFrame(color_tab, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(8, 2))
        ctk.CTkLabel(head, text="Fingertip Colors", font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(head, text="Select a fingertip, then pick its color. These colors tint the live tracking overlay.",
                     font=ctk.CTkFont(size=11), text_color="#A2A1B6").pack(anchor="w")

        self.finger_selector = ctk.CTkSegmentedButton(
            color_tab, values=[f.capitalize() for f in self.FINGER_ORDER],
            command=self.on_finger_changed, selected_color="#393676", selected_hover_color="#55529e"
        )
        self.finger_selector.pack(padx=18, fill="x", pady=(6, 6))

        body = ctk.CTkFrame(color_tab, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12)

        # ---- left card: interactive hand ----
        hand_card = ctk.CTkFrame(body, fg_color=self.HAND_BG, corner_radius=16)
        hand_card.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.hand_preview_canvas = ctk.CTkCanvas(hand_card, width=380, height=310, bg=self.HAND_BG, highlightthickness=0)
        self.hand_preview_canvas.pack(padx=10, pady=10, expand=True)
        self.hand_preview_canvas.bind("<Button-1>", self.on_hand_preview_click)
        self.hand_preview_canvas.bind("<Motion>", self._on_hand_hover)

        # ---- right card: wheel + brightness + hex + presets ----
        picker = ctk.CTkFrame(body, fg_color=self.HAND_BG, corner_radius=16)
        picker.pack(side="left", fill="y", padx=(6, 0))

        self.wheel_canvas = ctk.CTkCanvas(picker, width=self.WHEEL_PX, height=self.WHEEL_PX, bg=self.HAND_BG,
                                          highlightthickness=0, cursor="crosshair")
        self.wheel_canvas.pack(padx=22, pady=(12, 4))
        self.wheel_canvas.bind("<Button-1>", self.sample_wheel_color)
        self.wheel_canvas.bind("<B1-Motion>", self.sample_wheel_color)

        bright_row = ctk.CTkFrame(picker, fg_color="transparent")
        bright_row.pack(fill="x", padx=22, pady=(0, 4))
        ctk.CTkLabel(bright_row, text="Brightness", font=ctk.CTkFont(size=10, weight="bold"), text_color="#6C6B80").pack(side="left")
        self.brightness_slider = ctk.CTkSlider(bright_row, from_=0.15, to=1.0, number_of_steps=85, width=140,
                                               command=self.on_brightness_change, button_color="#E2E1E9",
                                               button_hover_color="#FFFFFF", progress_color="#393676")
        self.brightness_slider.set(1.0)
        self.brightness_slider.pack(side="right")

        hex_row = ctk.CTkFrame(picker, fg_color="transparent")
        hex_row.pack(fill="x", padx=22, pady=4)
        self.color_preview_swatch = ctk.CTkFrame(hex_row, width=34, height=34, corner_radius=8,
                                                 fg_color="#FFFFFF", border_width=2, border_color="#2B2B42")
        self.color_preview_swatch.pack(side="left")
        self.hex_entry = ctk.CTkEntry(hex_row, width=96, font=ctk.CTkFont(family="Consolas", size=12), placeholder_text="#RRGGBB")
        self.hex_entry.pack(side="left", padx=8)
        self.hex_entry.bind("<KeyRelease>", self.on_hex_entry)
        ctk.CTkButton(hex_row, text="Reset", width=52, height=28, fg_color="#2c2c3e", hover_color="#3e3e56",
                      command=self.reset_selected_finger_color).pack(side="left")
        ctk.CTkButton(hex_row, text="All", width=36, height=28, fg_color="#2c2c3e", hover_color="#3e3e56",
                      command=self.reset_all_finger_colors).pack(side="left", padx=(4, 0))

        sw_row = ctk.CTkFrame(picker, fg_color="transparent")
        sw_row.pack(pady=(6, 12))
        for c in self.PRESET_SWATCHES:
            ctk.CTkButton(sw_row, text="", width=22, height=22, corner_radius=11, fg_color=c, hover_color=c,
                          border_width=1, border_color="#2B2B42",
                          command=lambda col=c: self.apply_finger_color(col)).pack(side="left", padx=2)

        # ---- bottom: one chip per finger showing its current color ----
        chips = ctk.CTkFrame(color_tab, fg_color="transparent")
        chips.pack(fill="x", padx=14, pady=(8, 2))
        self.finger_chips = {}
        for f in self.FINGER_ORDER:
            chip = ctk.CTkFrame(chips, fg_color="#1A1A26", corner_radius=10, border_width=2, border_color="#1A1A26")
            chip.pack(side="left", expand=True, fill="x", padx=4)
            dot = ctk.CTkFrame(chip, width=16, height=16, corner_radius=8, fg_color=self.custom_colors.get(f, "#FFFFFF"))
            dot.pack(side="left", padx=(12, 6), pady=8)
            lbl = ctk.CTkLabel(chip, text=f.upper(), font=ctk.CTkFont(size=11, weight="bold"), text_color="#A2A1B6")
            lbl.pack(side="left", padx=(0, 12))
            for w in (chip, dot, lbl):
                w.bind("<Button-1>", lambda e, name=f: self.select_finger(name))
            self.finger_chips[f] = (chip, dot, lbl)

        ctk.CTkLabel(color_tab, text="Colors apply after Save All Changes.", font=ctk.CTkFont(size=11, slant="italic"),
                     text_color="#A2A1B6").pack(pady=(2, 0))

        self.draw_hsv_color_wheel()
        self.select_finger(self.selected_finger)
        self._pulse_job = self.after(100, self._pulse_tick)

    # ---------------- selection + color state ----------------
    def select_finger(self, name):
        if name not in self.FINGER_ORDER:
            return
        self.selected_finger = name
        self.finger_selector.set(name.capitalize())
        self.apply_finger_color(self.custom_colors.get(name, "#FFFFFF"), sync_picker=True)

    def on_finger_changed(self, value):
        self.select_finger(str(value).lower())

    def apply_finger_color(self, color, sync_picker=True):
        """Single entry point that updates the stored color and every widget that shows it."""
        import colorsys
        color = self._norm_hex(color)
        if not color:
            return
        self.custom_colors[self.selected_finger] = color

        if sync_picker:
            r, g, b = (int(color[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
            h, s, v = colorsys.rgb_to_hsv(r, g, b)
            self._pick_h, self._pick_s = h, s
            self.wheel_val = max(v, 0.15)
            self.brightness_slider.set(self.wheel_val)
            self.draw_hsv_color_wheel()

        self._refresh_color_widgets(color)

    def _refresh_color_widgets(self, color):
        if self.hex_entry.get().strip().upper() != color:
            self.hex_entry.delete(0, "end")
            self.hex_entry.insert(0, color)
        self.color_preview_swatch.configure(fg_color=color)

        for f, (chip, dot, lbl) in self.finger_chips.items():
            dot.configure(fg_color=self.custom_colors.get(f, "#FFFFFF"))
            selected = (f == self.selected_finger)
            chip.configure(border_color="#E2E1E9" if selected else "#1A1A26")
            lbl.configure(text_color="#E2E1E9" if selected else "#A2A1B6")

        self._draw_wheel_marker(color)
        self.draw_settings_skeletal_hand()

        tracker = getattr(self, 'changes_tracker_lbl', None)
        if tracker is not None and tracker.winfo_exists():
            tracker.configure(text=f"🎨 {self.selected_finger.upper()} color: {color}", text_color=color)

    def on_hex_entry(self, event=None):
        color = self._norm_hex(self.hex_entry.get())
        if color:
            self.apply_finger_color(color, sync_picker=True)

    def on_brightness_change(self, value):
        import colorsys
        self.wheel_val = float(value)
        self.draw_hsv_color_wheel()
        r, g, b = colorsys.hsv_to_rgb(self._pick_h, self._pick_s, self.wheel_val)
        self.apply_finger_color(f"#{int(round(r * 255)):02X}{int(round(g * 255)):02X}{int(round(b * 255)):02X}",
                                sync_picker=False)

    def reset_selected_finger_color(self):
        self.apply_finger_color(self.DEFAULT_FINGER_COLORS[self.selected_finger], sync_picker=True)

    def reset_all_finger_colors(self):
        self.custom_colors.update(self.DEFAULT_FINGER_COLORS)
        self.select_finger(self.selected_finger)

    # ---------------- color wheel ----------------
    def draw_hsv_color_wheel(self):
        """Renders an anti-aliased HSV wheel (supersampled) at the current brightness."""
        import numpy as np
        from PIL import Image, ImageTk

        size = self.WHEEL_PX
        ss = 2
        n = size * ss
        c = n / 2.0
        r = self.WHEEL_R * ss

        ys, xs = np.mgrid[0:n, 0:n]
        dx = xs + 0.5 - c
        dy = ys + 0.5 - c
        dist = np.sqrt(dx * dx + dy * dy)
        hue = (np.arctan2(dy, dx) + np.pi) / (2 * np.pi)
        sat = np.clip(dist / r, 0.0, 1.0)
        val = float(self.wheel_val)

        h6 = hue * 6.0
        i = np.floor(h6).astype(int) % 6
        f = h6 - np.floor(h6)
        p = val * (1 - sat)
        q = val * (1 - f * sat)
        t = val * (1 - (1 - f) * sat)
        v = np.full_like(p, val)
        red = np.choose(i, [v, q, p, p, t, v])
        grn = np.choose(i, [t, v, v, q, p, p])
        blu = np.choose(i, [p, p, t, v, v, q])
        alpha = np.clip(r - dist + 0.5, 0.0, 1.0)

        rgba = np.dstack([red, grn, blu, alpha])
        img = Image.fromarray((rgba * 255).astype(np.uint8), "RGBA").resize((size, size), Image.LANCZOS)
        self._wheel_photo = ImageTk.PhotoImage(img)

        canvas = self.wheel_canvas
        canvas.delete("wheel")
        mid = size / 2
        canvas.create_oval(mid - self.WHEEL_R - 3, mid - self.WHEEL_R - 3,
                           mid + self.WHEEL_R + 3, mid + self.WHEEL_R + 3,
                           outline="#2B2B42", width=2, tags="wheel")
        canvas.create_image(mid, mid, image=self._wheel_photo, tags="wheel")
        canvas.tag_lower("wheel")

    def _draw_wheel_marker(self, color):
        import math
        canvas = self.wheel_canvas
        canvas.delete("marker")
        mid = self.WHEEL_PX / 2
        angle = self._pick_h * 2 * math.pi - math.pi
        mx = mid + self._pick_s * self.WHEEL_R * math.cos(angle)
        my = mid + self._pick_s * self.WHEEL_R * math.sin(angle)
        canvas.create_oval(mx - 11, my - 11, mx + 11, my + 11, outline="#000000", width=1, tags="marker")
        canvas.create_oval(mx - 9, my - 9, mx + 9, my + 9, outline="#FFFFFF", width=2, fill=color, tags="marker")

    def sample_wheel_color(self, event):
        """Click/drag on the wheel: hue from angle, saturation from distance (clamped to the rim)."""
        import math
        import colorsys
        mid = self.WHEEL_PX / 2
        dx, dy = event.x - mid, event.y - mid
        dist = math.hypot(dx, dy)
        if dist > self.WHEEL_R + 16:
            return
        self._pick_s = min(dist / self.WHEEL_R, 1.0)
        self._pick_h = ((math.atan2(dy, dx) + math.pi) / (2 * math.pi)) % 1.0
        r, g, b = colorsys.hsv_to_rgb(self._pick_h, self._pick_s, self.wheel_val)
        self.apply_finger_color(f"#{int(round(r * 255)):02X}{int(round(g * 255)):02X}{int(round(b * 255)):02X}",
                                sync_picker=False)

    # ---------------- hand preview ----------------
    def _hand_geometry(self):
        """(base knuckle, mid joint, tip) per finger on the 380x310 preview canvas."""
        return {
            "thumb":  ((126, 225), (88, 193), (60, 155)),
            "index":  ((140, 172), (128, 122), (120, 74)),
            "middle": ((180, 163), (180, 104), (180, 52)),
            "ring":   ((220, 168), (232, 114), (240, 66)),
            "pinky":  ((258, 182), (276, 142), (290, 104)),
        }

    def _hand_tip_positions(self):
        return {name: pts[2] for name, pts in self._hand_geometry().items()}

    def draw_settings_skeletal_hand(self):
        """Draws a filled hand silhouette with a glowing, colored fingertip per finger; the selected one is emphasised."""
        canvas = getattr(self, 'hand_preview_canvas', None)
        if canvas is None:
            return
        canvas.delete("all")
        bg = self.HAND_BG
        skin, edge = "#1B1B2B", "#2B2B42"
        geo = self._hand_geometry()

        palm = [(132, 290), (250, 290), (272, 224), (276, 188), (258, 178), (220, 168),
                (180, 160), (140, 168), (124, 188), (116, 228)]
        flat = [coord for pt in palm for coord in pt]

        # silhouette: outline pass, then fill pass, so fingers and palm merge into one shape
        for base, mid, tip in geo.values():
            canvas.create_line(*base, *mid, *tip, fill=edge, width=26, capstyle="round", joinstyle="round")
        canvas.create_polygon(*flat, smooth=True, fill=edge, outline=edge, width=4)
        for base, mid, tip in geo.values():
            canvas.create_line(*base, *mid, *tip, fill=skin, width=22, capstyle="round", joinstyle="round")
        canvas.create_polygon(*flat, smooth=True, fill=skin, outline=skin)

        # joint hints
        for base, mid, tip in geo.values():
            canvas.create_oval(mid[0] - 3, mid[1] - 3, mid[0] + 3, mid[1] + 3, fill=edge, outline="")

        # fingertips: soft glow rings + colored dot
        for finger in self.FINGER_ORDER:
            x, y = geo[finger][2]
            col = self.custom_colors.get(finger, "#FFFFFF")
            selected = (finger == self.selected_finger)
            radius = 13 if selected else 10
            for grow, strength in ((9, 0.12), (5, 0.25)):
                glow = self._blend_hex(col, bg, strength + (0.1 if selected else 0))
                canvas.create_oval(x - radius - grow, y - radius - grow, x + radius + grow, y + radius + grow,
                                   fill=glow, outline="")
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=col,
                               outline="#FFFFFF" if selected else "#0D0D14", width=2)
            canvas.create_text(x, y - 30, text=finger.upper(), fill="#E2E1E9" if selected else "#6C6B80",
                               font=("Consolas", 9, "bold"))

        canvas.create_text(12, 298, anchor="w", text="Click a fingertip to select it", fill="#44445A",
                           font=("Consolas", 8))

    def _nearest_finger(self, x, y, reach=26):
        best, best_dist = None, reach
        for finger, (tx, ty) in self._hand_tip_positions().items():
            dist = ((x - tx) ** 2 + (y - ty) ** 2) ** 0.5
            if dist <= best_dist:
                best, best_dist = finger, dist
        return best

    def _on_hand_hover(self, event):
        try:
            self.hand_preview_canvas.configure(cursor="hand2" if self._nearest_finger(event.x, event.y) else "")
        except Exception:
            pass

    def on_hand_preview_click(self, event):
        """Selects the fingertip nearest to the click."""
        best = self._nearest_finger(event.x, event.y)
        if best:
            self.select_finger(best)

    def _pulse_tick(self):
        """Gentle pulsing ring around the selected fingertip so the hand never looks frozen."""
        import math
        self._pulse_job = None
        try:
            if not self.winfo_exists():
                return
            if self.tab_view.get() == "Finger Colors":
                canvas = self.hand_preview_canvas
                canvas.delete("pulse")
                x, y = self._hand_tip_positions()[self.selected_finger]
                self._pulse_phase = (self._pulse_phase + 0.14) % (2 * math.pi)
                k = (math.sin(self._pulse_phase) + 1) / 2
                radius = 20 + 9 * k
                ring = self._blend_hex(self.custom_colors.get(self.selected_finger, "#FFFFFF"), self.HAND_BG,
                                       0.85 - 0.6 * k)
                canvas.create_oval(x - radius, y - radius, x + radius, y + radius, outline=ring, width=2, tags="pulse")
            self._pulse_job = self.after(50, self._pulse_tick)
        except Exception:
            pass

    def load_settings_payload_from_disk(self, config_path):
        """Loads voice commands and the unified dynamic application shortcuts tracking registry."""
        if not os.path.exists(config_path): 
            return
        try:
            with open(config_path, 'r') as f:
                data = json.load(f)

            registry = data.get("dynamic_apps_registry", {})
            for app_path, config_payload in registry.items():
                phrases_str = ", ".join(config_payload.get("launch_phrases", []))
                self.render_launcher_entry(app_path, phrases_str, config_payload)
                
        except Exception as e:
            print(f"Error loading system profile values: {e}")

    def render_launcher_entry(self, path, phrases, config_object=None):
        """Renders an application configuration row equipped with inner workspace context editing."""
        row = ctk.CTkFrame(self.launcher_scroll, fg_color="#181824")
        row.pack(fill="x", padx=5, pady=4)

        config_data = config_object if config_object else {"window_identifiers": [], "shortcuts": {}}

        logo_label = ctk.CTkLabel(row, text="🌐" if not path.lower().endswith(".exe") else "🚀", font=ctk.CTkFont(size=14))
        logo_label.pack(side="left", padx=8)

        if path and os.path.exists(path) and path.lower().endswith(".exe"):
            try:
                from PIL import Image
                import icoextract
                extractor = icoextract.IconExtractor(path)
                icon_tmp_path = os.path.join(BASE_DIR, "config", "tmp_icon.png")
                extractor.export_icon(icon_tmp_path)
                if os.path.exists(icon_tmp_path):
                    my_img = ctk.CTkImage(light_image=Image.open(icon_tmp_path), dark_image=Image.open(icon_tmp_path), size=(20, 20))
                    logo_label.configure(image=my_img, text="")
            except Exception:
                pass 

        e_path = ctk.CTkEntry(row, width=200, placeholder_text="Binary path (.exe)...")
        e_path.insert(0, path)
        e_path.pack(side="left", padx=5, pady=5)

        ctk.CTkButton(row, text="Browse", width=60, command=lambda: self.pick_file(e_path)).pack(side="left", padx=2)

        e_phrase = ctk.CTkEntry(row, width=180, placeholder_text="Phrases (comma separated)...")
        e_phrase.insert(0, phrases)
        e_phrase.pack(side="left", fill="x", expand=True, padx=5)

        edit_btn = ctk.CTkButton(
            row, text="⚙ Edit Configuration", width=110, fg_color="#2c2c3e", hover_color="#3e3e56",
            command=lambda: AppShortcutEditorWindow(self, e_path.get(), config_data)
        )
        edit_btn.pack(side="left", padx=5)

        container = {"frame": row, "path_widget": e_path, "phrase_widget": e_phrase, "inner_config": config_data}
        
        ctk.CTkButton(row, text="🗑", width=35, fg_color="#ff1744", command=lambda: self.drop_launcher(row, container)).pack(side="right", padx=5)
        self.dynamic_apps_ui_rows.append(container)

    def render_shortcut_entry(self, app_tag, identifier, keystroke, phrases):
        row = ctk.CTkFrame(self.shortcut_scroll, fg_color="#1F1F2E")
        row.pack(fill="x", padx=5, pady=4)
        e_tag = ctk.CTkEntry(row, width=90, placeholder_text="App Tag...")
        e_tag.insert(0, app_tag)
        e_tag.pack(side="left", padx=4, pady=5)
        e_ident = ctk.CTkEntry(row, width=130, placeholder_text="Window Identifier...")
        e_ident.insert(0, identifier)
        e_ident.pack(side="left", padx=4)
        e_key = ctk.CTkEntry(row, width=100, placeholder_text="Hotkey (ctrl+s)...")
        e_key.insert(0, keystroke)
        e_key.pack(side="left", padx=4)
        e_phrase = ctk.CTkEntry(row, width=160, placeholder_text="Phrases...")
        e_phrase.insert(0, phrases)
        e_phrase.pack(side="left", fill="x", expand=True, padx=4)
        container = {"frame": row, "tag_widget": e_tag, "ident_widget": e_ident, "key_widget": e_key, "phrase_widget": e_phrase}
        ctk.CTkButton(row, text="🗑", width=35, fg_color="#ff1744", command=lambda: self.drop_shortcut(row, container)).pack(side="right", padx=5)
        self.dynamic_shortcuts_ui_rows.append(container)

    def pick_file(self, widget):
        """Spawns a single unified dialog window to select either a local file or type a URL."""
        source_modal = ctk.CTkToplevel(self)
        source_modal.title("Select Target Destination Source")
        source_modal.geometry("500x230")
        source_modal.resizable(False, False)
        source_modal.transient(self)
        source_modal.grab_set()

        lbl_info = ctk.CTkLabel(
            source_modal, 
            text="Link a Local Application File or enter a Web URL Link", 
            font=ctk.CTkFont(weight="bold", size=13)
        )
        lbl_info.pack(pady=(15, 10))

        file_frame = ctk.CTkFrame(source_modal, fg_color="transparent")
        file_frame.pack(fill="x", padx=30, pady=5)
        
        lbl_file = ctk.CTkLabel(file_frame, text="Local File Explorer:", font=ctk.CTkFont(size=11, weight="bold"))
        lbl_file.pack(anchor="w")

        def launch_explorer():
            from tkinter import filedialog
            path = filedialog.askopenfilename(filetypes=[("Executables", "*.exe"), ("All Files", "*.*")])
            if path:
                widget.delete(0, "end")
                widget.insert(0, path.lower().strip())
                source_modal.destroy()

        browse_system_btn = ctk.CTkButton(
            file_frame, 
            text="📁 Choose Executable File...", 
            fg_color="#393676", 
            hover_color="#55529e",
            command=launch_explorer
        )
        browse_system_btn.pack(fill="x", pady=(2, 5))

        ctk.CTkLabel(source_modal, text="- OR -", font=ctk.CTkFont(size=10, weight="bold"), text_color="#6C6B80").pack(pady=2)

        url_frame = ctk.CTkFrame(source_modal, fg_color="transparent")
        url_frame.pack(fill="x", padx=30, pady=5)

        lbl_url = ctk.CTkLabel(url_frame, text="Web Address Link URL:", font=ctk.CTkFont(size=11, weight="bold"))
        lbl_url.pack(anchor="w")

        url_row = ctk.CTkFrame(url_frame, fg_color="transparent")
        url_row.pack(fill="x", pady=2)

        e_url_input = ctk.CTkEntry(url_row, placeholder_text="e.g. ://github.com or https://youtube.com")
        e_url_input.pack(side="left", fill="x", expand=True, padx=(0, 8))

        def submit_custom_url():
            entered_url = e_url_input.get().strip()
            if entered_url:
                clean_url = entered_url
                if not clean_url.lower().startswith(("http://", "https://", "www.")):
                    clean_url = "www." + clean_url
                
                widget.delete(0, "end")
                widget.insert(0, clean_url)
                source_modal.destroy()

        url_submit_btn = ctk.CTkButton(
            url_row, 
            text="Apply Link", 
            width=100, 
            fg_color="#2baf54", 
            hover_color="#228b42",
            command=submit_custom_url
        )
        url_submit_btn.pack(side="right")

    def render_voice_row(self, action_key, phrases):
        """Renders one editable voice command row: action name + comma separated phrases + delete."""
        row = ctk.CTkFrame(self.voice_scroll, fg_color="#181824")
        row.pack(fill="x", padx=5, pady=4)

        action_values = list(dict.fromkeys(self.known_voice_actions))
        e_action = ctk.CTkComboBox(row, values=action_values, width=190)
        e_action.set(action_key)
        e_action.pack(side="left", padx=5, pady=5)

        e_phrase = ctk.CTkEntry(row, placeholder_text="Phrases (comma separated)...")
        if phrases:
            e_phrase.insert(0, phrases)
        e_phrase.pack(side="left", fill="x", expand=True, padx=5)

        container = {"frame": row, "action_widget": e_action, "phrase_widget": e_phrase}
        ctk.CTkButton(row, text="🗑", width=35, fg_color="#ff1744", command=lambda: self.drop_voice_row(row, container)).pack(side="right", padx=5)
        self.voice_ui_rows.append(container)

    def get_action_choices(self):
        """Preset actions plus any custom actions already used by a gesture row."""
        choices = list(self.available_gesture_actions)
        for row in getattr(self, 'gesture_ui_rows', []):
            try:
                action = row["action_widget"].get().strip()
            except Exception:
                continue
            if action and action not in choices:
                choices.append(action)
        return choices

    def render_gesture_row(self, gesture):
        """Renders one editable gesture row: name + action (preset or custom) + custom editor + delete."""
        gesture = dict(gesture) if gesture else {}
        row = ctk.CTkFrame(self.gesture_scroll, fg_color="#181824")
        row.pack(fill="x", padx=5, pady=4)

        e_name = ctk.CTkEntry(row, width=150, placeholder_text="Gesture name...")
        if gesture.get("name"):
            e_name.insert(0, gesture["name"])
        e_name.pack(side="left", padx=5, pady=5)

        initial_action = gesture.get("action", "")
        values = self.get_action_choices()
        if initial_action and initial_action not in values:
            values.append(initial_action)
        e_action = ctk.CTkComboBox(row, values=values, width=230)
        e_action.set(initial_action)
        e_action.pack(side="left", padx=5)

        ctk.CTkButton(row, text="⚙ Custom", width=80, fg_color="#2c2c3e", hover_color="#3e3e56",
                      command=lambda: self.open_custom_action_dialog(e_action)).pack(side="left", padx=5)

        container = {"frame": row, "name_widget": e_name, "action_widget": e_action, "extra": gesture}
        ctk.CTkButton(row, text="🗑", width=35, fg_color="#ff1744", command=lambda: self.drop_gesture_row(row, container)).pack(side="right", padx=5)
        ctk.CTkButton(row, text="👁", width=35, fg_color="#2c2c3e", hover_color="#3e3e56",
                      command=lambda: self.show_gesture_preview(container)).pack(side="right", padx=2)
        self.gesture_ui_rows.append(container)
        return container

    def drop_gesture_row(self, f, c):
        f.destroy()
        if c in self.gesture_ui_rows:
            self.gesture_ui_rows.remove(c)

    def open_custom_action_dialog(self, action_widget, parent=None):
        """Lets the user define a custom gesture action: launch an app/URL or press a hotkey."""
        modal = ctk.CTkToplevel(parent or self)
        modal.title("Custom Gesture Action")
        modal.geometry("480x240")
        modal.resizable(False, False)
        modal.transient(parent or self)
        modal.grab_set()

        ctk.CTkLabel(modal, text="What should this gesture do?", font=ctk.CTkFont(weight="bold", size=13)).pack(pady=(15, 8))
        type_menu = ctk.CTkSegmentedButton(modal, values=["Launch app / URL", "Press hotkey"])
        type_menu.set("Launch app / URL")
        type_menu.pack(padx=30, fill="x")

        value_row = ctk.CTkFrame(modal, fg_color="transparent")
        value_row.pack(fill="x", padx=30, pady=12)
        e_value = ctk.CTkEntry(value_row, placeholder_text="App path, URL, or hotkey (e.g. ctrl+shift+t)")
        e_value.pack(side="left", fill="x", expand=True, padx=(0, 8))
        browse_btn = ctk.CTkButton(value_row, text="Browse", width=70, command=lambda: self.pick_file(e_value))
        browse_btn.pack(side="right")

        def on_type_change(choice):
            browse_btn.configure(state="normal" if choice == "Launch app / URL" else "disabled")
        type_menu.configure(command=on_type_change)

        current = action_widget.get().strip()
        if current.startswith("launch:"):
            e_value.insert(0, current[len("launch:"):])
        elif current.startswith("hotkey:"):
            type_menu.set("Press hotkey")
            on_type_change("Press hotkey")
            e_value.insert(0, current[len("hotkey:"):])

        def apply_custom_action():
            value = e_value.get().strip()
            if not value:
                return
            if type_menu.get() == "Launch app / URL":
                action = "launch:" + value
            else:
                action = "hotkey:" + value.lower().replace(" ", "")
            action_widget.configure(values=self.get_action_choices() + [action])
            action_widget.set(action)
            modal.destroy()

        btns = ctk.CTkFrame(modal, fg_color="transparent")
        btns.pack(fill="x", padx=30, pady=8)
        ctk.CTkButton(btns, text="Apply", fg_color="#2baf54", hover_color="#228b42", command=apply_custom_action).pack(side="right")
        ctk.CTkButton(btns, text="Cancel", fg_color="#7f8c8d", hover_color="#95a5a6", command=modal.destroy).pack(side="left")

    def add_blank_voice_row(self): self.render_voice_row("", "")
    def drop_voice_row(self, f, c):
        f.destroy()
        if c in self.voice_ui_rows:
            self.voice_ui_rows.remove(c)

    def add_blank_launcher_row(self): self.render_launcher_entry("", "")
    def add_blank_shortcut_row(self): self.render_shortcut_entry("", "", "", "")
    def drop_launcher(self, f, c): f.destroy(); self.dynamic_apps_ui_rows.remove(c)
    def drop_shortcut(self, f, c): f.destroy(); self.dynamic_shortcuts_ui_rows.remove(c)


    # ------------------------------------------------------------------
    # HAND MODEL PREVIEW (capture popup, 👁 list button, duplicate dialog)
    # ------------------------------------------------------------------
    @staticmethod
    def _tips_text(ext):
        names = [n.upper() for n in SettingsWindow.FINGER_ORDER if n in ext]
        return ", ".join(names) if names else "NONE (CLOSED HAND)"

    def _fill_preview_from_row(self, panel, row):
        """Draws the hand model for a gesture-list row: real landmarks if captured, template for built-ins."""
        name = row["name_widget"].get().strip() or "(unnamed)"
        action = row["action_widget"].get().strip() or "(no command)"
        header = f"{name.upper()} -> {action}"

        landmarks = (row.get("extra") or {}).get("landmarks")
        if landmarks and len(landmarks) == 21:
            pts = [(p[0], p[1]) for p in landmarks]
            ext = {n for n, v in self._analyze_hand(pts)["extended"].items() if v}
            panel.show(pts, ext, caption=f"{header}\nTIPS: {self._tips_text(ext)}")
            return

        spec = get_builtin_preview(name)
        if spec is None:
            panel.show_message(f"No hand model for '{name}'.\nCapture it with the camera to get one.")
            return

        ext = set(spec["fingers"])
        pose = set(spec.get("pose", spec["fingers"]))
        pts = build_template_hand(pose, spec.get("touch"), spec.get("flip", False))
        trail = spec.get("trail")
        if trail:
            word = ("RIGHT" if trail[0] > 0 else "LEFT") if trail[0] else ("DOWN" if trail[1] > 0 else "UP")
            info = f"MOTION: SWIPE {word}"
        else:
            info = f"TIPS: {self._tips_text(ext)}"
        panel.show(pts, ext, caption=f"{header}\n{info}", trail=trail)

    def show_gesture_preview(self, row):
        """👁 button: view-only window showing the hand model of a saved gesture."""
        win = ctk.CTkToplevel(self)
        win.title("Gesture Preview")
        win.geometry("400x400")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        panel = GesturePreviewPanel(win, self.custom_colors, width=340, height=260)
        panel.pack(padx=20, pady=(20, 8))
        self._fill_preview_from_row(panel, row)
        ctk.CTkButton(win, text="Close", fg_color="#7f8c8d", hover_color="#95a5a6", command=win.destroy).pack(pady=8)

    def prompt_save_captured_custom_pose(self):
        """Captures the current pose. If it already exists, offers to change that gesture's command instead."""
        landmarks_data = getattr(self, 'last_detected_landmarks_cache', None)
        if not landmarks_data:
            self.vision_telemetry_lbl.configure(text="❌ CANNOT CAPTURE: Present hand fully to radar tracking nodes.", text_color="#ff1744")
            return

        snapshot = list(landmarks_data)
        existing_row, reserved_key = self.find_existing_gesture(snapshot)

        if reserved_key:
            # Hard-coded built-in: keep the feed running and just explain why it can't be captured.
            self._calib_notice_until = time.monotonic() + 4.0
            self.vision_telemetry_lbl.configure(
                text=f"⚠ POSE RESERVED\n'{reserved_key}' is {self.RESERVED_BUILTIN_POSES[reserved_key]} and can't be reassigned.\nTry a different pose.",
                text_color="#ffab00"
            )
            return

        self._safely_kill_video_hardware()

        self.toggle_feed_btn.configure(text="📹 Resume Video Feed", fg_color="#393676", hover_color="#55529e")
        self.capture_pose_btn.configure(state="disabled")

        if existing_row is not None:
            self._show_duplicate_gesture_dialog(existing_row)
            return

        pts = [(p.x, p.y) for p in snapshot]
        info = self._analyze_hand(pts)
        ext = {n for n, v in info["extended"].items() if v}

        prompt_modal = ctk.CTkToplevel(self)
        prompt_modal.title("Register Captured Vision Custom Pose")
        prompt_modal.geometry("420x640")
        prompt_modal.resizable(False, False)
        prompt_modal.transient(self)
        prompt_modal.grab_set()

        ctk.CTkLabel(prompt_modal, text="🎯 Gesture Captured", font=ctk.CTkFont(weight="bold", size=14)).pack(pady=(14, 6))
        panel = GesturePreviewPanel(prompt_modal, self.custom_colors, width=340, height=250)
        panel.pack(padx=20, pady=4)
        panel.show(pts, ext, caption=f"DETECTED TIPS: {self._tips_text(ext)}\n{info['label']}")

        e_name = ctk.CTkEntry(prompt_modal, placeholder_text="Give this gesture a unique name (e.g. raw_pinch)...", width=340)
        e_name.pack(pady=(10, 5))

        drop_action = ctk.CTkComboBox(prompt_modal, values=self.get_action_choices(), width=340)
        drop_action.pack(pady=5)
        ctk.CTkButton(prompt_modal, text="⚙ Custom action (app / URL / hotkey)", width=340, fg_color="#2c2c3e", hover_color="#3e3e56",
                      command=lambda: self.open_custom_action_dialog(drop_action, parent=prompt_modal)).pack(pady=5)

        err_lbl = ctk.CTkLabel(prompt_modal, text="", text_color="#ff1744", font=ctk.CTkFont(size=11))
        err_lbl.pack()

        def commit_snapshot_profile_to_disk():
            g_name = e_name.get().strip().lower().replace(" ", "_")
            chosen_action = drop_action.get().strip()
            if not g_name or not chosen_action:
                err_lbl.configure(text="Enter a name and choose a command.")
                return

            used_names = {r["name_widget"].get().strip().lower().replace(" ", "_") for r in self.gesture_ui_rows}
            if g_name in used_names:
                err_lbl.configure(text=f"The name '{g_name}' is already used. Pick another one.")
                return

            landmarks = [[round(pt.x, 5), round(pt.y, 5), round(pt.z, 5)] for pt in snapshot]
            self.render_gesture_row({"name": g_name, "action": chosen_action, "landmarks": landmarks})
            self.vision_telemetry_lbl.configure(
                text=f"Tendon Frame State: PROFILE REGISTERED\nName: {g_name} -> Action: {chosen_action}",
                text_color="#00e676"
            )
            prompt_modal.destroy()

        btns = ctk.CTkFrame(prompt_modal, fg_color="transparent")
        btns.pack(fill="x", padx=40, pady=10)
        ctk.CTkButton(btns, text="Link Gesture Profile", fg_color="#2baf54", hover_color="#228b42", command=commit_snapshot_profile_to_disk).pack(side="right")
        ctk.CTkButton(btns, text="Discard", fg_color="#7f8c8d", hover_color="#95a5a6", command=prompt_modal.destroy).pack(side="left")

    def _show_duplicate_gesture_dialog(self, row):
        """Tells the user this gesture is already registered, shows its hand model, and lets them change its command."""
        name = row["name_widget"].get().strip() or "(unnamed)"
        current = row["action_widget"].get().strip()
        shown_current = current or "(no command set)"

        self.vision_telemetry_lbl.configure(
            text=f"⚠ GESTURE ALREADY EXISTS\nMatches: {name} -> {shown_current}",
            text_color="#ffab00"
        )

        modal = ctk.CTkToplevel(self)
        modal.title("Gesture Already Exists")
        modal.geometry("420x660")
        modal.resizable(False, False)
        modal.transient(self)
        modal.grab_set()

        ctk.CTkLabel(modal, text="⚠ This gesture already exists", font=ctk.CTkFont(weight="bold", size=14), text_color="#ffab00").pack(pady=(14, 6))
        panel = GesturePreviewPanel(modal, self.custom_colors, width=340, height=230)
        panel.pack(padx=20, pady=4)
        self._fill_preview_from_row(panel, row)

        ctk.CTkLabel(
            modal, justify="center", wraplength=380, text_color="#A2A1B6",
            text=f"It matches '{name}'. No need to register it again.\nIf you like, change its command instead:"
        ).pack(pady=(8, 4))

        drop_action = ctk.CTkComboBox(modal, values=self.get_action_choices(), width=340)
        drop_action.set(current)
        drop_action.pack(pady=(4, 4))
        ctk.CTkButton(modal, text="⚙ Custom action (app / URL / hotkey)", width=340, fg_color="#2c2c3e", hover_color="#3e3e56",
                      command=lambda: self.open_custom_action_dialog(drop_action, parent=modal)).pack(pady=4)

        def apply_new_command():
            value = drop_action.get().strip()
            if not value:
                return
            choices = self.get_action_choices()
            if value not in choices:
                choices.append(value)
            row["action_widget"].configure(values=choices)
            row["action_widget"].set(value)

            self.vision_telemetry_lbl.configure(
                text=f"Command updated: {name} -> {value}\nPress Save All Changes to apply.",
                text_color="#00e676"
            )
            # Briefly highlight the updated row in the gesture list.
            try:
                row["frame"].configure(fg_color="#3a3a6e")
                self.after(2500, lambda: row["frame"].winfo_exists() and row["frame"].configure(fg_color="#181824"))
            except Exception:
                pass
            modal.destroy()

        btns = ctk.CTkFrame(modal, fg_color="transparent")
        btns.pack(fill="x", padx=40, pady=10)
        ctk.CTkButton(btns, text="Change Command", fg_color="#2baf54", hover_color="#228b42", command=apply_new_command).pack(side="right")
        ctk.CTkButton(btns, text="Keep As Is", fg_color="#7f8c8d", hover_color="#95a5a6", command=modal.destroy).pack(side="left")

    def _safely_kill_video_hardware(self):
        """Stops the calibration stream (UI thread only). The worker thread owns and releases the camera itself."""
        self.is_calibration_stream_active = False
        self._calib_generation += 1  # tells any running worker to exit
        self.last_detected_landmarks_cache = None

        with self._calib_lock:
            self._calib_frame = None
            self._calib_telemetry = None
            self._calib_error = None

        if self._calib_poll_job is not None:
            try:
                self.after_cancel(self._calib_poll_job)
            except Exception:
                pass
            self._calib_poll_job = None

        try:
            if hasattr(self, 'camera_feed_lbl') and self.camera_feed_lbl.winfo_exists():
                self.camera_feed_lbl.configure(
                    image=None,
                    text="[ WEBCAM CORE STANDBY ]\n\nStart the video feed to track and capture poses."
                )
                # CTkLabel keeps a dead Tk image reference after image=None, which causes a repeating
                # TclError ("image pyimageN doesn't exist") on every redraw. Clear it explicitly.
                self.camera_feed_lbl._label.configure(image="")

            if hasattr(self, 'toggle_feed_btn') and self.toggle_feed_btn.winfo_exists():
                self.toggle_feed_btn.configure(text="📹 Start Video Feed", fg_color="#393676", hover_color="#55529e")

            if hasattr(self, 'capture_pose_btn') and self.capture_pose_btn.winfo_exists():
                self.capture_pose_btn.configure(state="disabled")
        except Exception as ui_err:
            print(f"[CALIBRATION] UI reset skipped: {ui_err}")

        self._calib_ctk_image = None

    def on_window_close_cleanup(self):
        """Window teardown destructor routing handler."""
        self._safely_kill_video_hardware()
        self.destroy()

    def destroy(self):
        """Override destroy to implement camera hardware death guardrail when closing via Cancel/Save."""
        if getattr(self, "_pulse_job", None):
            try:
                self.after_cancel(self._pulse_job)
            except Exception:
                pass
            self._pulse_job = None
        self._safely_kill_video_hardware()
        super().destroy()

    def save_all_settings(self):
        """Serializes current voice configurations, camera color choices, and custom snapshot gestures back to disk."""
        gesture_path = os.path.join(BASE_DIR, 'config', 'gestures.json')
        voice_path = os.path.join(BASE_DIR, 'config', 'voice_commands.json')

        updated_voice_commands = {}
        for row in self.voice_ui_rows:
            action_key = row["action_widget"].get().strip().lower().replace(" ", "_")
            raw_text = row["phrase_widget"].get()
            phrases_list = [p.strip().lower() for p in raw_text.split(",") if p.strip()]
            if action_key and phrases_list:
                merged = updated_voice_commands.setdefault(action_key, [])
                merged.extend(p for p in phrases_list if p not in merged)

        new_registry = {}
        if hasattr(self, 'dynamic_apps_ui_rows') and self.dynamic_apps_ui_rows:
            for row in self.dynamic_apps_ui_rows:
                p = row["path_widget"].get().strip()
                txt = row["phrase_widget"].get().strip()
                if p:
                    inner_cfg = row.get("inner_config", {"window_identifiers": [], "shortcuts": {}})
                    inner_cfg["launch_phrases"] = [item.strip().lower() for item in txt.split(",") if item.strip()]
                    new_registry[p] = inner_cfg

        updated_gestures = []
        seen_names = set()
        for row in self.gesture_ui_rows:
            g_name = row["name_widget"].get().strip().lower().replace(" ", "_")
            action_value = row["action_widget"].get().strip()
            if not g_name or not action_value or g_name in seen_names:
                continue
            seen_names.add(g_name)
            entry = dict(row.get("extra", {}))
            entry["name"] = g_name
            entry["action"] = action_value
            updated_gestures.append(entry)

        safe_cooldown_value = 0.50

        try:
            try:
                with open(gesture_path, 'r') as f:
                    payload_gestures = json.load(f)
            except Exception:
                payload_gestures = {}

            controls = dict(payload_gestures.get("safety_cooldown_controls", {}))
            controls.setdefault("global_gesture_delay", 2.2)
            controls["swipe_action_delay"] = safe_cooldown_value
            controls.setdefault("custom_action_delay", 3.0)

            payload_gestures["gestures"] = updated_gestures
            payload_gestures["colors"] = self.custom_colors
            payload_gestures["safety_cooldown_controls"] = controls
            payload_gestures.setdefault("pose_match_threshold", 0.25)

            with open(gesture_path, 'w') as f:
                json.dump(payload_gestures, f, indent=4)

            try:
                with open(voice_path, 'r') as f:
                    voice_payload = json.load(f)
            except Exception:
                voice_payload = {}
            voice_payload["voice_commands"] = updated_voice_commands
            voice_payload["dynamic_apps_registry"] = new_registry
            voice_payload.setdefault("commit_delay", 1.2)
            with open(voice_path, 'w') as f:
                json.dump(voice_payload, f, indent=4)

            import database_manager
            if hasattr(self, 'profile_field_entries') and self.profile_field_entries:
                for field_key, entry_widget in self.profile_field_entries.items():
                    current_value = entry_widget.get().strip()
                    database_manager.store_profile_data(field_key, current_value)

            self.copy_config_to_clipboard()
            self._safely_kill_video_hardware()
            self.parent_dashboard.load_and_display_json()
            self.destroy()
            
            print("[SYSTEM CONFIG] All system configuration tracks serialized and updated safely.")
        except Exception as e:
            print(f"[CRITICAL FILE SAVE ERROR] Serialization or update path failure: {e}")

    def copy_config_to_clipboard(self):
        """Pushes current layout hex configurations to clipboard using underlying tk capabilities."""
        try:
            formatted_hex_log = (
                f"THUMB:  {self.custom_colors['thumb']}\n"
                f"INDEX:  {self.custom_colors['index']}\n"
                f"MIDDLE: {self.custom_colors['middle']}\n"
                f"RING:   {self.custom_colors['ring']}\n"
                f"PINKY:  {self.custom_colors['pinky']}"
            )
            self.clipboard_clear()
            self.clipboard_append(formatted_hex_log)
            print("[SYSTEM] Vector hex data dumped to system clipboard layout.")
        except Exception as e:
            print(f"Clipboard operational failure: {e}")

def _accepts(func, param):
    """True if func takes a parameter with this name (lets main.py work with older module versions)."""
    try:
        return param in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


class HCIDashboard(ctk.CTk):
    UI_POLL_MS = 50          # how often work queued by background threads is applied to the widgets

    def __init__(self):
        super().__init__()

        self.title("Multimodal HCI Command Dashboard")
        self.geometry("1050x700")      # size used when the user restores (un-maximizes) the window
        self.minsize(900, 600)

        # Background threads (camera, voice, dispatcher) must never touch widgets directly: Tkinter is not
        # thread-safe. They hand work to this queue and the main thread applies it (see _drain_ui_queue).
        self._main_thread = threading.current_thread()
        self._ui_queue = queue.SimpleQueue()
        self._voice_preview_pending = None
        self._run_id = 0                       # bumped on every START/STOP; each run's loops check it
        self._start_lock = threading.Lock()
        self._engine_threads = []              # every camera/voice thread started, so a new run can wait for them

        self.bg_color = "#0A0A0F" 
        self.nav_color = "#0F0F14" 
        self.card_color = "#12121A" 
        self.log_container = "#12121A" 
        self.textbox_bg = "#050508" 
        self.btn_green = "#00e676"
        self.btn_red = "#ff1744" 

        self.color_thumb = "#00FF00"   # Pure Green
        self.color_index = "#0000FF"   # Pure Blue 
        self.color_middle = "#FF00FF"  # Magenta
        self.color_ring = "#00FFFF"    # Cyan
        self.color_pinky = "#FFFFFF"   # White

        self.configure(fg_color=self.bg_color)

        self.engine_active = False
        self.tracker_thread = None
        self.voice_thread = None

        self.setup_ui()

        import database_manager
        database_manager.init_db()

        self.reports_open = False
        self.sidebar_target_width = 650 
        self.current_sidebar_position_x = -self.sidebar_target_width

        self.camera_frame_queue = queue.Queue(maxsize=2)
        self.preview_window = None 

        self.analytics_sidebar = ReportDashboard(parent_dashboard=self, base_dir=BASE_DIR)

        self.mappings_display.configure(state="normal")
        self.mappings_display.insert("0.0", "🔄 INITIALIZING ENGINE CORE...\n\n"
                                             " • Loading local system threads...\n"
                                             " • Reading configuration tracks from storage disks...\n"
                                             "Please wait a moment.")
        self.mappings_display.configure(state="disabled")

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(50, self._fluid_splice_startup)
        # CustomTkinter re-applies the window state while it styles the Windows title bar right after start-up,
        # which undid a zoom set here and left the window small/minimized. Maximizing once the event loop runs
        # (and once more shortly after, in case the first call was overridden) makes it stick.
        self.after(0, self._maximize_window)
        self.after(250, self._maximize_window)
        self.after(self.UI_POLL_MS, self._drain_ui_queue)

    def _maximize_window(self):
        """Opens the dashboard maximized and in front (works on Windows, with a fallback for Linux)."""
        try:
            self.deiconify()
            try:
                self.state("zoomed")                 # Windows / macOS
            except tk.TclError:
                self.attributes("-zoomed", True)     # most Linux window managers
            self.lift()
            self.focus_force()
        except Exception as e:
            print(f"[SYSTEM] Could not maximize the window: {e}")

    def _fluid_splice_startup(self):
        # The mini HUD is a Toplevel of this window, created on the main thread. It used to be a second tk.Tk()
        # running its own mainloop in a background thread, which Tkinter does not support (random
        # "Tcl_AsyncDelete" crashes, and it could steal focus from the main window at start-up).
        try:
            self.mini_hud = start_hud_process(self)
        except Exception as e:
            self.mini_hud = None
            print(f"[SYSTEM] Mini HUD could not start: {e}")
        self.load_and_display_json()
        # Load the Whisper model in the background now, so pressing START does not have to wait for it.
        preload = getattr(voice_module, "preload_model", None)    # missing in older voice_module.py files
        if preload is not None:
            threading.Thread(target=preload, daemon=True).start()

    # ------------------------------------------------------------------
    # Thread-safe UI helpers
    # ------------------------------------------------------------------
    def _on_main_thread(self):
        return threading.current_thread() is self._main_thread

    def run_on_ui(self, func):
        """Runs func on the Tk main thread: immediately if already there, otherwise on the next UI poll."""
        if self._on_main_thread():
            func()
        else:
            self._ui_queue.put(func)

    def _drain_ui_queue(self):
        # apply a bounded batch per tick so a flood of log lines can never freeze the window
        for _ in range(200):
            try:
                func = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                func()
            except Exception as e:
                print(f"[UI] Deferred update failed: {e}")
        preview, self._voice_preview_pending = self._voice_preview_pending, None
        if preview is not None:
            self._apply_voice_preview(*preview)
        self.after(self.UI_POLL_MS, self._drain_ui_queue)

    def setup_ui(self):
        nav_ribbon = ctk.CTkFrame(self, width=70, corner_radius=15, fg_color=self.nav_color)
        nav_ribbon.pack(side="left", fill="y", padx=(15, 10), pady=15)
        nav_ribbon.pack_propagate(False)

        profile_btn = ctk.CTkButton(nav_ribbon, text="👤", width=40, height=40, corner_radius=20,
                                    fg_color="#393676", hover_color="#55529e", text_color="#ffffff",
                                    font=ctk.CTkFont(size=20), command=self.open_reports)
        profile_btn.pack(side="top", pady=20)

        self.settings_btn = ctk.CTkButton(nav_ribbon, text="⚙", width=40, height=40, corner_radius=10,
                                          fg_color="transparent", hover_color="#12121A", text_color="#ffffff",
                                          font=ctk.CTkFont(size=24), command=self.open_settings)
        self.settings_btn.pack(side="bottom", pady=20)

        content_wrapper = ctk.CTkFrame(self, fg_color="transparent")
        content_wrapper.pack(side="right", fill="both", expand=True, padx=(5, 15), pady=15)

        control_pane = ctk.CTkFrame(content_wrapper, height=100, corner_radius=20, fg_color=self.card_color)
        control_pane.pack(side="top", fill="x", pady=(0, 15))
        control_pane.pack_propagate(False)

        self.master_btn = ctk.CTkButton(control_pane, text="▶ START", width=340, height=46, corner_radius=23,
                                         fg_color=self.btn_green, hover_color="#00ff87", text_color="#0A0A0F",
                                         font=ctk.CTkFont(size=14, weight="bold"), command=self.toggle_engine_core)
        self.master_btn.pack(pady=27)

        bottom_split = ctk.CTkFrame(content_wrapper, fg_color="transparent")
        bottom_split.pack(side="bottom", fill="both", expand=True)

        guide_card = ctk.CTkFrame(bottom_split, width=380, corner_radius=20, fg_color=self.card_color)
        guide_card.pack(side="left", fill="y", padx=(0, 15))
        guide_card.pack_propagate(False)

        guide_title = ctk.CTkLabel(guide_card, text="Live Vector Hand Telemetry", font=ctk.CTkFont(size=15, weight="bold"), text_color="#E2E1E9")
        guide_title.pack(pady=(20, 2))
        subtitle = ctk.CTkLabel(guide_card, text="Skeletal hand coordinates mapped in real-time", font=ctk.CTkFont(size=11, slant="italic"), text_color="#6C6B80")
        subtitle.pack(pady=(0, 10))

        self.hand_canvas = ctk.CTkCanvas(guide_card, width=340, height=260, bg=self.card_color, highlightthickness=0)
        self.hand_canvas.pack(padx=20, pady=5)
        
        self.draw_2d_skeletal_hand()

        divider = ctk.CTkFrame(guide_card, height=2, fg_color="#1F1F2E")
        divider.pack(fill="x", padx=20, pady=(15, 10))

        self.mic_indicator = ctk.CTkLabel(guide_card, text="Audio: Offline 🔴", font=ctk.CTkFont(size=12), text_color="#6C6B80")
        self.mic_indicator.pack(anchor="w", padx=25, pady=4)

        self.cam_indicator = ctk.CTkLabel(guide_card, text="Camera: Offline 🔴", font=ctk.CTkFont(size=12), text_color="#6C6B80")
        self.cam_indicator.pack(anchor="w", padx=25, pady=4)

        self.voice_preview_lbl = ctk.CTkLabel(guide_card, text="Voice: idle", font=ctk.CTkFont(family="Consolas", size=11),
                                              justify="left", anchor="w", wraplength=330, text_color="#6C6B80")
        self.voice_preview_lbl.pack(anchor="w", fill="x", padx=25, pady=(8, 4))

        log_card = ctk.CTkFrame(bottom_split, corner_radius=20, fg_color=self.log_container)
        log_card.pack(side="right", fill="both", expand=True)

        config_label = ctk.CTkLabel(log_card, text="System Core Telemetry Logs", font=ctk.CTkFont(size=15, weight="bold"), text_color="#E2E1E9")
        config_label.pack(pady=(15, 5))

        self.mappings_display = ctk.CTkTextbox(log_card, font=ctk.CTkFont(family="Consolas", size=11),
                                               fg_color=self.textbox_bg, text_color="#A2A1B6", corner_radius=15,
                                               spacing1=5, spacing3=5)
        self.mappings_display.pack(pady=(5, 15), padx=15, fill="both", expand=True)
        self.mappings_display._textbox.configure(selectbackground="#222230", insertbackground=self.btn_green)

    def open_reports(self, tab=None):
        """Profile button: slides the Profile dashboard over the window (press again to close it).
        tab may be "Profile", "History" or "Progress" to jump straight to that tab."""
        if tab and self.reports_open:
            self.analytics_sidebar.select_tab(tab)
            return
        if tab:
            self.analytics_sidebar.select_tab(tab)
        self.toggle_reports_drawer()

    def close_reports(self):
        """Slides the dashboard back out."""
        if self.reports_open:
            self.toggle_reports_drawer()

    def toggle_reports_drawer(self):
        """Mounts the dashboard over the whole window and slides it in or out."""
        if not self.reports_open:
            self.update_idletasks()
            self.sidebar_target_width = max(self.winfo_width(), 700)
            self.current_sidebar_position_x = -self.sidebar_target_width

            # relwidth=1.0 keeps it covering the window even if the window is resized while it is open
            self.analytics_sidebar.place(x=self.current_sidebar_position_x, y=0, relwidth=1.0, relheight=1.0)
            self.analytics_sidebar.refresh_dashboard()

            target_x = 0
            self.reports_open = True
            self._execute_sidebar_slide_animation(target_x)
        else:
            target_x = -self.sidebar_target_width
            self.reports_open = False
            self._execute_sidebar_slide_animation(target_x)

    def _execute_sidebar_slide_animation(self, target_x):
        """Steps the dashboard horizontally until it reaches target_x."""
        step_velocity = max(60, self.sidebar_target_width // 10)

        if self.current_sidebar_position_x != target_x:
            if self.current_sidebar_position_x < target_x:
                self.current_sidebar_position_x = min(target_x, self.current_sidebar_position_x + step_velocity)
            else:
                self.current_sidebar_position_x = max(target_x, self.current_sidebar_position_x - step_velocity)

            self.analytics_sidebar.place(x=self.current_sidebar_position_x, y=0, relwidth=1.0, relheight=1.0)
            self.after(10, lambda: self._execute_sidebar_slide_animation(target_x))
        else:
            if not self.reports_open:
                self.analytics_sidebar.place_forget()

    def draw_2d_skeletal_hand(self):
        """Constructs an interactive vector graphic wireframe of a tracking hand schema."""
        self.hand_canvas.delete("all")

        wrist = [170, 240]
        palm_base = [170, 190]
        
        thumb_j = [110, 180]
        thumb_tip = [70, 150]
        
        index_j = [120, 120]
        index_tip = [115, 60]
        
        middle_j = [170, 110]
        middle_tip = [170, 45]
        
        ring_j = [215, 120]
        ring_tip = [220, 55]
        
        pinky_j = [255, 140]
        pinky_tip = [270, 85]

        def draw_bone(p1, p2):
            self.hand_canvas.create_line(p1[0], p1[1], p2[0], p2[1], fill="#1F1F35", width=3)

        draw_bone(wrist, palm_base)
        draw_bone(palm_base, thumb_j)
        draw_bone(thumb_j, thumb_tip)
        draw_bone(palm_base, index_j)
        draw_bone(index_j, index_tip)
        draw_bone(palm_base, middle_j)
        draw_bone(middle_j, middle_tip)
        draw_bone(palm_base, ring_j)
        draw_bone(ring_j, ring_tip)
        draw_bone(palm_base, pinky_j)
        draw_bone(pinky_j, pinky_tip)

        def draw_joint(pt, color, radius=7):
            self.hand_canvas.create_oval(pt[0]-radius, pt[1]-radius, pt[0]+radius, pt[1]+radius, fill=color, outline=self.card_color, width=1)

        draw_joint(wrist, "#393676", 5)
        draw_joint(palm_base, "#393676", 5)
        draw_joint(thumb_j, "#1F1F35", 4)
        draw_joint(index_j, "#1F1F35", 4)
        draw_joint(middle_j, "#1F1F35", 4)
        draw_joint(ring_j, "#1F1F35", 4)
        draw_joint(pinky_j, "#1F1F35", 4)

        draw_joint(thumb_tip, self.color_thumb, 7)
        draw_joint(index_tip, self.color_index, 7)
        draw_joint(middle_tip, self.color_middle, 7)
        draw_joint(ring_tip, self.color_ring, 7)
        draw_joint(pinky_tip, self.color_pinky, 7)

        self.hand_canvas.create_text(50, 130, text="THUMB", fill="#6C6B80", font=("Consolas", 9, "bold"))
        self.hand_canvas.create_text(115, 38, text="INDEX", fill="#6C6B80", font=("Consolas", 9, "bold"))
        self.hand_canvas.create_text(170, 25, text="MID", fill="#6C6B80", font=("Consolas", 9, "bold"))
        self.hand_canvas.create_text(225, 38, text="RING", fill="#6C6B80", font=("Consolas", 9, "bold"))
        self.hand_canvas.create_text(285, 68, text="PINKY", fill="#6C6B80", font=("Consolas", 9, "bold"))

    def open_settings(self):
        SettingsWindow(self)

    def open_history(self):
        """History now lives inside the Profile dashboard; this opens the dashboard on that tab."""
        self.open_reports(tab="History")

    def toggle_engine_core(self):
        if not self.engine_active:
            self.engine_active = True

            self.master_btn.configure(text="■ STOP", fg_color=self.btn_red, hover_color="#ff4f70", text_color="#ffffff")
            self.mic_indicator.configure(text="Audio: Active 🟢", text_color=self.btn_green)
            self.cam_indicator.configure(text="Camera: Active 🟢", text_color=self.btn_green)

            # (the tracker and the voice loop switch their own flags on when they start; setting them here could
            #  revive a previous run that had not finished stopping yet, leaving two copies fighting over the
            #  webcam / microphone)

            if self.preview_window is None or not self.preview_window.winfo_exists():
                try:
                    self.preview_window = CustomCameraPreview(
                        frame_queue=self.camera_frame_queue,
                        close_callback=self.toggle_engine_core
                    )
                except Exception as e:
                    # Roll back so the UI doesn't say "running" while nothing started.
                    self.preview_window = None
                    self.engine_active = False
                    self.master_btn.configure(text="▶ START", fg_color=self.btn_green, hover_color="#00ff87", text_color="#0A0A0F")
                    self.mic_indicator.configure(text="Audio: Offline 🔴", text_color="#6C6B80")
                    self.cam_indicator.configure(text="Camera: Offline 🔴", text_color="#6C6B80")
                    vision_module.TRACKER_LOOP_ACTIVE = False
                    voice_module.VOICE_LOOP_ACTIVE = False
                    self._safe_log_async(f"[SYSTEM] Camera preview failed to open: {e}")
                    return

            try:
                import database_manager
                database_manager.start_session()
            except Exception as e:
                print(f"[SYSTEM] Could not register session start: {e}")

            self._run_id += 1
            run_id = self._run_id

            def should_run():
                # The camera and voice loops of this run keep going only while this exact run is current.
                # A quick STOP -> START used to leave the old tracker running next to the new one (both fighting
                # over the webcam) because the loops only looked at a shared on/off flag.
                return self.engine_active and self._run_id == run_id

            def start_threads():
                with self._start_lock:          # starts happen one at a time
                    # let every previous run release the webcam and the microphone first
                    for old in self._engine_threads:
                        if old.is_alive():
                            old.join(timeout=10.0)     # voice may be in the middle of transcribing a phrase
                    self._engine_threads = [t for t in self._engine_threads if t.is_alive()]
                    if not should_run():
                        return                  # STOP was pressed while we were waiting
                    tracker = threading.Thread(target=self._run_tracker_safely, args=(should_run,), daemon=True)
                    voice = threading.Thread(target=self._run_voice_safely, args=(should_run,), daemon=True)
                    self.tracker_thread, self.voice_thread = tracker, voice
                    self._engine_threads += [tracker, voice]
                    tracker.start()
                    voice.start()
                self._safe_log_async("[SYSTEM] Engine activated. Hand vector nodes and voice loops deployed smoothly.")

            threading.Thread(target=start_threads, daemon=True).start()
        else:
            self.engine_active = False
            self._run_id += 1          # cancels a start that is still waiting for the previous run to finish

            try:
                import database_manager
                database_manager.end_session()
            except Exception as e:
                print(f"[SYSTEM] Could not register session end: {e}")

            self.master_btn.configure(text="▶ START", fg_color=self.btn_green, hover_color="#00ff87", text_color="#0A0A0F")
            self.mic_indicator.configure(text="Audio: Offline 🔴", text_color="#6C6B80")
            self.cam_indicator.configure(text="Camera: Offline 🔴", text_color="#6C6B80")

            vision_module.TRACKER_LOOP_ACTIVE = False
            voice_module.VOICE_LOOP_ACTIVE = False

            if self.preview_window and self.preview_window.winfo_exists():
                self.preview_window.destroy()
                self.preview_window = None
            
            self.tracker_thread = None
            self.voice_thread = None
            self.set_voice_preview("Voice: idle", "#6C6B80")
            self._safe_log_async("[SYSTEM] Processing channels closed down. Engine stopped safely.")

    def load_and_display_json(self):
        gesture_path = os.path.join(BASE_DIR, 'config', 'gestures.json')
        voice_path = os.path.join(BASE_DIR, 'config', 'voice_commands.json')
        formatted_text = "CURRENT MULTIMODAL CONFIGURATIONS:\n" + "="*35 + "\n\n"

        try:
            with open(gesture_path, 'r') as f:
                g_data = json.load(f)
                formatted_text += "[VISION MODEL PHASES]\n"
                for g in g_data.get('gestures', []):
                    formatted_text += f" • {g['name'].upper().replace('_', ' ')} -> {g['action'].upper()}\n"

                colors = g_data.get('colors', {})
                self.color_thumb = colors.get('thumb', "#00FF00")
                self.color_index = colors.get('index', "#0000FF")
                self.color_middle = colors.get('middle', "#FF00FF")
                self.color_ring = colors.get('ring', "#00FFFF")
                self.color_pinky = colors.get('pinky', "#FFFFFF")
                
                self.draw_2d_skeletal_hand()
        except Exception as e:
            formatted_text += f"Vision Log Read Error: {e}\n"
            
        formatted_text += "\n" + "="*35 + "\n\n[VOICE ENGINE GLOSSARY]\n"

        try:
            with open(voice_path, 'r') as f:
                v_data = json.load(f)
                for action, phrases in v_data.get('voice_commands', {}).items():
                    phrases_str = " OR ".join([f'"{p}"' for p in phrases])
                    formatted_text += f" • {action.upper()} triggered via:\n   {phrases_str}\n"
        except Exception as e:
            formatted_text += f"Voice Log Read Error: {e}\n"
            
        self.mappings_display.configure(state="normal")
        self.mappings_display.delete("0.0", "end")
        self.mappings_display.insert("0.0", formatted_text)
        self.mappings_display.configure(state="disabled")

    def set_voice_preview(self, text, color="#A2A1B6"):
        """Thread-safe: shows what the voice engine is currently interpreting, before it runs.
        The voice thread can call this ~10x a second; only the newest text is drawn on each UI poll."""
        if self._on_main_thread():
            self._apply_voice_preview(text, color)
        else:
            self._voice_preview_pending = (text, color)

    def _apply_voice_preview(self, text, color):
        try:
            if self.voice_preview_lbl.winfo_exists():
                self.voice_preview_lbl.configure(text=text, text_color=color)
        except Exception:
            pass

    def _run_tracker_safely(self, should_run=None):
        """Runs the hand tracker. If it cannot start or crashes (camera busy, model file missing...),
        the reason is shown in the log and the camera indicator instead of failing silently."""
        try:
            if _accepts(run_hand_tracker, "should_run"):
                run_hand_tracker(self.camera_frame_queue, should_run=should_run)
            else:   # an older vision_module.py is installed
                run_hand_tracker(self.camera_frame_queue)
        except SystemExit:
            self._safe_log_async("[CAMERA] Hand tracker could not start - the hand_landmarker.task model file "
                                 "could not be loaded (see the console for details).")
            self.run_on_ui(self._mark_camera_error)
        except BaseException as e:
            self._safe_log_async(f"[CAMERA] Hand tracker stopped: {type(e).__name__}: {e}")
            self.run_on_ui(self._mark_camera_error)

    def _run_voice_safely(self, should_run=None):
        """Runs the voice engine. If it cannot start or crashes (no microphone, model problem...), the reason is
        shown in the log and the audio indicator instead of leaving 'Voice: idle' forever."""
        try:
            if _accepts(run_voice_loop, "should_run"):
                run_voice_loop(self, should_run=should_run)
            else:   # an older voice_module.py is installed
                self._safe_log_async("[VOICE] Note: voice_module.py is an older version - replace it with the "
                                     "updated file for the latest fixes.")
                run_voice_loop(self)
        except BaseException as e:
            self._safe_log_async(f"[VOICE] Voice engine stopped: {type(e).__name__}: {e}")
            self.run_on_ui(self._mark_audio_error)

    def _mark_audio_error(self):
        if self.engine_active:
            self.mic_indicator.configure(text="Audio: Error 🔴", text_color=self.btn_red)

    def _mark_camera_error(self):
        if self.engine_active:
            self.cam_indicator.configure(text="Camera: Error 🔴", text_color=self.btn_red)

    def _safe_log_async(self, message):
        self._ui_queue.put(lambda: self._write_log(message))

    def log_message(self, message):
        """Safe to call from any thread (the voice loop and the dispatcher call it from background threads)."""
        if self._on_main_thread():
            self._write_log(message)
        else:
            self._ui_queue.put(lambda: self._write_log(message))

    LOG_MAX_LINES = 1000

    def _write_log(self, message):
        box = self.mappings_display
        box.configure(state="normal")
        box.insert("end", f"\n{message}")
        # keep the log bounded: a textbox with tens of thousands of lines makes every insert slower
        line_count = int(box.index("end-1c").split(".")[0])
        if line_count > self.LOG_MAX_LINES:
            box.delete("1.0", f"{line_count - self.LOG_MAX_LINES}.0")
        box.see("end")
        box.configure(state="disabled")

    def on_close(self):
        try:
            import database_manager
            database_manager.end_session()
        except Exception:
            pass
        print("\n[SYSTEM] Triggering immediate hard-kill protocol...")
        print("[SYSTEM] Killing process. Goodbye!")
        os._exit(0)

if __name__ == "__main__":
    app = HCIDashboard()
    app.mainloop()