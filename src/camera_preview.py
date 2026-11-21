import queue
import time
import tkinter as tk
import customtkinter as ctk
import cv2
from PIL import Image, ImageTk, ImageDraw, ImageFont
import system_state
import hud_bus

subtitle_overlay_history = []

# Longer memory of what was heard, shown under "RECENT COMMANDS" in the full HUD: (time, text, is_command)
speech_history = []

def _remember_speech(text, is_command):
    text = _overlay_safe(str(text))
    if not text:
        return
    if speech_history and speech_history[-1][1] == text:
        return   # same phrase repeated
    speech_history.append((time.time(), text, bool(is_command)))
    del speech_history[:-12]

def add_subtitle_to_overlay(text, is_command=False):
    prefix = "[CMD] " if is_command else ""
    subtitle_overlay_history.append((f"{prefix}{text}", is_command))
    _remember_speech(text, is_command)
    if len(subtitle_overlay_history) > 2:
        subtitle_overlay_history.pop(0)

# What the system is *currently interpreting* (not yet executed). Written by the voice
# thread, read by the Tk thread. A single tuple assignment is atomic, so no lock is needed.
_overlay_preview = None  # None, or (list_of_lines, (r, g, b))

def _overlay_safe(text):
    """Arial can't draw emoji; swap/drop them so the overlay doesn't show boxes."""
    text = text.replace("✔", "Done:")
    return "".join(c for c in text if ord(c) < 0x2200).strip()

def set_overlay_preview(text, color="#ffb300"):
    """Show the live 'interpreting...' panel on the camera overlay. Idle text hides it."""
    global _overlay_preview
    lines = [ln for ln in (_overlay_safe(l) for l in text.split("\n")) if ln]
    if not lines or lines[0].lower().startswith("voice: idle"):
        _overlay_preview = None
        return
    try:
        c = color.lstrip("#")
        rgb = (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except Exception:
        rgb = (255, 179, 0)
    _overlay_preview = (lines, rgb)

def _fit_text(draw, text, font, max_width):
    """Trim from the left (keep the most recent words) with an ellipsis if too wide."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength("..." + text, font=font) > max_width:
        text = text[1:]
    return "..." + text

def _clip(draw, text, font, max_width):
    """Trim from the right with an ellipsis if too wide."""
    if max_width <= 0:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "...", font=font) > max_width:
        text = text[:-1]
    return text + "..."


# ----------------------------------------------------------------------------------------------------------------
# Drawing (pure PIL/OpenCV, no Tk, so it can be tested without a window)
# ----------------------------------------------------------------------------------------------------------------
def _rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

def _mix(a, b, t):
    """Colour a laid over colour b at strength t (0..1)."""
    return tuple(int(round(b[i] + (a[i] - b[i]) * t)) for i in range(3))

BG = _rgb("#12121A")
PANEL = _rgb("#181824")
ROW = _rgb("#1F1F2E")
EDGE = _rgb("#2B2B42")
TXT = _rgb("#E2E1E9")
MUTED = _rgb("#6C6B80")
DIM = _rgb("#2A2A3C")
GREEN = _rgb("#00e676")
AMBER = _rgb("#ffb74d")
RED = _rgb("#ff1744")
BLUE = _rgb("#29b6f6")

STATUS_COLOR = {"ok": GREEN, "fail": RED, "blocked": AMBER}
STATUS_LABEL = {"ok": "OK", "fail": "FAILED", "blocked": "BLOCKED"}

def _load_font(names, size):
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


class HudRenderer:
    """Turns (camera frame + live data) into one finished picture. Compact = feed with small chips,
    full = feed with a tracking panel on the left and a recent-commands panel on the right."""
    FULL_MIN_W = 760
    FULL_MIN_H = 400
    TOP_H = 44

    def __init__(self):
        bold = ("arialbd.ttf", "DejaVuSans-Bold.ttf")
        reg = ("arial.ttf", "DejaVuSans.ttf")
        self.f_title = _load_font(bold, 11)
        self.f_ui = _load_font(bold, 13)
        self.f_small = _load_font(reg, 11)
        self.f_mono = _load_font(("consola.ttf", "DejaVuSansMono.ttf"), 12)
        self.f_mono_b = _load_font(("consolab.ttf", "DejaVuSansMono-Bold.ttf"), 13)
        self.f_sub = _load_font(bold, 15)

    # ---------- small helpers ----------
    @staticmethod
    def _t(d, xy, text, font, fill, anchor="la"):
        try:
            d.text(xy, text, font=font, fill=fill, anchor=anchor)
        except ValueError:      # bitmap fallback font has no anchor support
            d.text(xy, text, font=font, fill=fill)

    @staticmethod
    def _latency(tr, now):
        ts = tr.get("frame_ts")
        if not ts or tr.get("paused"):
            return None
        return max(0.0, (now - ts) * 1000.0)

    @staticmethod
    def _light_status(tr):
        b = tr.get("brightness")
        if b is None:
            return None
        if b < 55:
            return "TOO DARK", AMBER
        if b > 215:
            return "OVEREXPOSED", AMBER
        return "LIGHT OK", GREEN

    # ---------- main entry ----------
    def render(self, frame, size, mode, tr, events, now, ui_fps):
        cw, ch = size
        full = (mode == "full") and cw >= self.FULL_MIN_W and ch >= self.FULL_MIN_H
        img = Image.new("RGB", (cw, ch), BG)
        d = ImageDraw.Draw(img, "RGBA")

        if full:
            m, lw, rw = 10, 214, 240
            top = self.TOP_H
            left = (m, top + m, m + lw, ch - m)
            right = (cw - m - rw, top + m, cw - m, ch - m)
            feed = (left[2] + m, top + m, right[0] - m, ch - m)
        else:
            feed = (0, 0, cw, ch)

        placed = self._place_feed(img, d, frame, feed, rounded=full)

        if full:
            self._topbar(d, cw, tr, ui_fps, now)
            self._tracking_panel(d, left, tr, now)
            self._commands_panel(d, right, events)
        else:
            self._compact_chips(d, placed, tr, events, ui_fps, now)

        self._voice_overlays(d, placed)
        return img

    def _place_feed(self, img, d, frame, rect, rounded):
        x0, y0, x1, y1 = rect
        fw, fh = x1 - x0, y1 - y0
        if rounded:
            d.rounded_rectangle(rect, radius=12, fill=(8, 8, 12), outline=EDGE)
        if frame is None or fw < 8 or fh < 8:
            self._t(d, ((x0 + x1) // 2, (y0 + y1) // 2), "Waiting for camera...", self.f_ui, MUTED, "mm")
            return rect
        h, w = frame.shape[:2]
        scale = min(fw / w, fh / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        small = cv2.resize(frame, (nw, nh), interpolation=interp)
        pil = Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
        px, py = x0 + (fw - nw) // 2, y0 + (fh - nh) // 2
        img.paste(pil, (px, py))
        if rounded:
            d.rectangle((px, py, px + nw - 1, py + nh - 1), outline=EDGE)
        return (px, py, px + nw, py + nh)

    # ---------- full mode: top bar ----------
    def _topbar(self, d, cw, tr, ui_fps, now):
        th = self.TOP_H
        d.rectangle((0, 0, cw, th), fill=PANEL)
        d.line((0, th, cw, th), fill=EDGE)
        cy = th // 2

        paused = bool(tr.get("paused"))
        fps = tr.get("fps") or 0.0
        lat = self._latency(tr, now)

        fps_col = MUTED if (paused or not fps) else GREEN if fps >= 20 else AMBER if fps >= 12 else RED
        lat_col = MUTED if lat is None else GREEN if lat < 150 else AMBER if lat < 300 else RED
        items = (
            ("FPS", "--" if (paused or not fps) else f"{fps:.0f}", fps_col),
            ("LATENCY", "--" if lat is None else f"{lat:.0f} ms", lat_col),
            ("UI", f"{ui_fps:.0f}", MUTED),
        )
        x = 16
        for label, value, col in items:
            self._t(d, (x, cy), label, self.f_title, MUTED, "lm")
            x += int(d.textlength(label, font=self.f_title)) + 8
            self._t(d, (x, cy), value, self.f_mono_b, col, "lm")
            x += int(d.textlength(value, font=self.f_mono_b)) + 24

        rx = cw - 14
        if paused:
            rx = self._pill(d, rx, cy, "ENGINE PAUSED", AMBER)
        else:
            rx = self._pill(d, rx, cy, "UNLOCKED" if tr.get("unlocked") else "LOCKED",
                            GREEN if tr.get("unlocked") else AMBER)
            rx = self._pill(d, rx, cy, "WRITING MODE" if tr.get("writing_mode") else "COMMAND MODE",
                            _rgb("#ce93d8") if tr.get("writing_mode") else BLUE)
            ls = self._light_status(tr)
            if ls and ls[1] == AMBER:
                self._pill(d, rx, cy, ls[0], AMBER)

    def _pill(self, d, right_x, cy, text, color):
        w = int(d.textlength(text, font=self.f_title)) + 22
        x0 = right_x - w
        d.rounded_rectangle((x0, cy - 12, right_x, cy + 12), radius=12,
                            fill=_mix(color, PANEL, 0.2), outline=_mix(color, PANEL, 0.65))
        self._t(d, (x0 + 11, cy), text, self.f_title, color, "lm")
        return x0 - 8

    # ---------- full mode: panels ----------
    def _panel(self, d, rect, title):
        d.rounded_rectangle(rect, radius=12, fill=PANEL, outline=EDGE)
        self._t(d, (rect[0] + 14, rect[1] + 16), title, self.f_title, MUTED, "lm")
        return rect[0] + 14, rect[1] + 32

    def _tracking_panel(self, d, rect, tr, now):
        x, y = self._panel(d, rect, "TRACKING")
        x1, bottom = rect[2] - 14, rect[3] - 8
        inner_w = x1 - x

        hand = bool(tr.get("hand"))
        paused = bool(tr.get("paused"))
        action = tr.get("action")
        has_action = hand and action and action != "system_idle"

        # hand state
        if paused:
            txt, col = "ENGINE PAUSED", AMBER
        elif hand:
            txt, col = "HAND DETECTED", GREEN
        else:
            txt, col = "SEARCHING...", RED
        d.ellipse((x, y + 3, x + 9, y + 12), fill=col)
        self._t(d, (x + 17, y + 8), txt, self.f_ui, col, "lm")
        y += 28

        # gesture + mapped command
        if y + 38 > bottom:
            return
        gesture = (tr.get("gesture") or "").replace("_", " ").upper() if hand else "-"
        self._t(d, (x, y), "GESTURE", self.f_title, MUTED)
        self._t(d, (x, y + 14), _clip(d, gesture, self.f_mono_b, inner_w), self.f_mono_b, TXT)
        y += 38
        if y + 38 > bottom:
            return
        cmd = hud_bus.friendly_name(action) if has_action else "-"
        self._t(d, (x, y), "COMMAND", self.f_title, MUTED)
        self._t(d, (x, y + 14), _clip(d, cmd, self.f_mono, inner_w), self.f_mono, BLUE)
        y += 38

        # hold-to-confirm segments
        if y + 30 > bottom:
            return
        needed = max(1, int(tr.get("streak_needed") or 1))
        streak = max(0, min(int(tr.get("streak") or 0), needed))
        live = has_action and bool(tr.get("unlocked"))
        self._t(d, (x, y), "HOLD TO CONFIRM", self.f_title, MUTED)
        if not tr.get("unlocked") and not paused:
            self._t(d, (x1, y), "locked", self.f_small, AMBER, "ra")
        seg_w = (inner_w - (needed - 1) * 4) / needed
        for i in range(needed):
            sx = x + i * (seg_w + 4)
            filled = live and i < streak
            d.rounded_rectangle((sx, y + 17, sx + seg_w, y + 25), radius=3, fill=GREEN if filled else DIM)
        y += 36

        # wrist coordinates
        if y + 38 > bottom:
            return
        wx, wy = tr.get("wrist") or (None, None)
        coord = "X --    Y --" if (wx is None or not hand) else f"X {wx:.2f}    Y {wy:.2f}"
        self._t(d, (x, y), "WRIST (0-1)", self.f_title, MUTED)
        if hand and tr.get("hand_scale"):
            self._t(d, (x1, y), f"scale {tr['hand_scale']:.3f}", self.f_small, MUTED, "ra")
        self._t(d, (x, y + 14), coord, self.f_mono, TXT)
        y += 38

        # 3x3 zone grid + wake ring
        if y + 70 > bottom:
            return
        cell, gap = 20, 3
        sel = tr.get("grid_cell") if hand else None
        for r in range(3):
            for c in range(3):
                cx0, cy0 = x + c * (cell + gap), y + r * (cell + gap)
                on = sel == (r, c)
                d.rounded_rectangle((cx0, cy0, cx0 + cell, cy0 + cell), radius=4,
                                    fill=_mix(GREEN, PANEL, 0.85) if on else DIM,
                                    outline=GREEN if on else None)
        self._t(d, (x + 3 * (cell + gap) + 8, y + 2), "ZONE", self.f_title, MUTED)
        zone = f"{sel[0]},{sel[1]}" if sel else "-"
        self._t(d, (x + 3 * (cell + gap) + 8, y + 16), zone, self.f_mono_b, TXT)

        rcx, rcy, rr = x1 - 24, y + 28, 19
        d.ellipse((rcx - rr, rcy - rr, rcx + rr, rcy + rr), outline=DIM, width=4)
        prog = float(tr.get("wake_progress") or 0.0)
        if prog > 0:
            d.arc((rcx - rr, rcy - rr, rcx + rr, rcy + rr), -90, -90 + 360 * prog, fill=AMBER, width=4)
        self._t(d, (rcx, rcy), "WAKE", self.f_title, AMBER if prog > 0 else MUTED, "mm")
        y += 74

        # lighting
        if y + 30 > bottom:
            return
        ls = self._light_status(tr)
        b = tr.get("brightness")
        self._t(d, (x, y), "LIGHTING", self.f_title, MUTED)
        if ls:
            self._t(d, (x, y + 14), f"{b:.0f}/255  {ls[0]}", self.f_mono, ls[1])
        else:
            self._t(d, (x, y + 14), "-", self.f_mono, MUTED)
        y += 38

        # cooldown bars
        cds = tr.get("cooldowns") or {}
        # full-bar lengths = the real cooldowns in vision_module (TRACK_CHANGE_COOLDOWN, COOLDOWN_DEFAULT)
        for label, key, total in (("TRACK", "track", 1.5), ("PLAY", "play", 2.2), ("MODE", "mode", 2.2)):
            if y + 18 > bottom:
                return
            remaining = max(0.0, float(cds.get(key, 0) or 0) - now)
            frac = min(1.0, remaining / max(total, remaining, 0.001))
            self._t(d, (x, y + 6), label, self.f_title, MUTED, "lm")
            bx0, bx1 = x + 50, x1
            d.rounded_rectangle((bx0, y + 2, bx1, y + 10), radius=3, fill=DIM)
            if remaining > 0:
                d.rounded_rectangle((bx0, y + 2, bx0 + max(4, (bx1 - bx0) * frac), y + 10), radius=3, fill=AMBER)
            else:
                d.rounded_rectangle((bx0, y + 2, bx1, y + 10), radius=3, fill=_mix(GREEN, PANEL, 0.35))
            y += 18

    def _commands_panel(self, d, rect, events):
        x, y = self._panel(d, rect, "RECENT COMMANDS")
        x1, bottom = rect[2] - 14, rect[3] - 8
        if not events:
            self._t(d, ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2), "No commands yet",
                    self.f_small, MUTED, "mm")
            return

        speech = list(speech_history)
        speech_lines = min(4, len(speech)) if (bottom - y) > 340 else 0
        speech_h = (28 + speech_lines * 20) if speech_lines else 0
        speech_top = bottom - speech_h

        footer_h = 22
        row_h = 42
        shown = 0
        for i, ev in enumerate(events):
            ry = y + i * row_h
            if ry + row_h - 6 > speech_top - footer_h:
                break
            shown += 1
            fade = max(0.4, 1.0 - i * 0.09)
            col = STATUS_COLOR.get(ev["status"], GREEN)
            d.rounded_rectangle((x - 4, ry, x1 + 2, ry + row_h - 6), radius=8, fill=ROW)
            d.rounded_rectangle((x - 4, ry, x, ry + row_h - 6), radius=2, fill=_mix(col, ROW, fade))
            tag = STATUS_LABEL.get(ev["status"], "OK")
            tag_w = int(d.textlength(tag, font=self.f_title))
            self._t(d, (x1 - 6, ry + 6), tag, self.f_title, _mix(col, ROW, fade), "ra")
            name = _clip(d, ev["name"], self.f_ui, (x1 - 6 - tag_w - 8) - (x + 10))
            self._t(d, (x + 10, ry + 5), name, self.f_ui, _mix(TXT, ROW, fade))
            sub = f"{time.strftime('%H:%M:%S', time.localtime(ev['t']))}  -  {ev['source'].upper()}"
            if ev["status"] != "ok" and ev.get("detail"):
                sub += f"  -  {ev['detail']}"
            self._t(d, (x + 10, ry + 23), _clip(d, sub, self.f_small, x1 - x - 14), self.f_small,
                    _mix(MUTED, ROW, fade))

        ok = sum(1 for e in events if e["status"] == "ok")
        fail = sum(1 for e in events if e["status"] == "fail")
        blocked = sum(1 for e in events if e["status"] == "blocked")
        self._t(d, (x, speech_top - 4), f"last {len(events)}:  {ok} ok  -  {fail} failed  -  {blocked} blocked",
                self.f_small, MUTED, "ld")

        if speech_lines:
            d.line((x - 4, speech_top + 2, x1 + 2, speech_top + 2), fill=(255, 255, 255, 30), width=1)
            self._t(d, (x, speech_top + 10), "SPEECH HEARD", self.f_small, MUTED)
            for i, (ts, txt, is_cmd) in enumerate(reversed(speech[-speech_lines:])):
                fade = max(0.45, 1.0 - i * 0.18)
                col = GREEN if is_cmd else TXT
                line = _clip(d, ("> " if is_cmd else "") + txt, self.f_small, x1 - x - 8)
                self._t(d, (x, speech_top + 28 + i * 20), line, self.f_small, _mix(col, ROW, fade))

    # ---------- compact mode ----------
    def _compact_chips(self, d, rect, tr, events, ui_fps, now):
        x0, y0, x1, y1 = rect
        paused = bool(tr.get("paused"))
        fps = tr.get("fps") or 0.0
        lat = self._latency(tr, now)
        text = "PAUSED" if paused else f"{fps:.0f} FPS  -  {'--' if lat is None else f'{lat:.0f} ms'}"
        w = int(d.textlength(text, font=self.f_mono_b)) + 38
        d.rounded_rectangle((x0 + 8, y0 + 8, x0 + 8 + w, y0 + 34), radius=13, fill=(10, 10, 15, 190))
        dot = AMBER if (paused or not tr.get("unlocked")) else GREEN
        d.ellipse((x0 + 19, y0 + 16, x0 + 28, y0 + 25), fill=dot)
        self._t(d, (x0 + 36, y0 + 21), text, self.f_mono_b, TXT, "lm")

        nx = x0 + 8 + w + 8
        ls = self._light_status(tr)
        if ls and ls[1] == AMBER and not paused:
            lw = int(d.textlength(ls[0], font=self.f_title)) + 22
            d.rounded_rectangle((nx, y0 + 8, nx + lw, y0 + 34), radius=13, fill=(10, 10, 15, 190), outline=AMBER)
            self._t(d, (nx + 11, y0 + 21), ls[0], self.f_title, AMBER, "lm")

        if events and now - events[0]["t"] < 4.0:
            ev = events[0]
            col = STATUS_COLOR.get(ev["status"], GREEN)
            label = f"{ev['name']}  {STATUS_LABEL.get(ev['status'], '')}"
            label = _clip(d, label, self.f_ui, (x1 - x0) - 60)
            lw = int(d.textlength(label, font=self.f_ui)) + 34
            d.rounded_rectangle((x1 - 8 - lw, y0 + 40, x1 - 8, y0 + 66), radius=13, fill=(10, 10, 15, 200), outline=col)
            d.ellipse((x1 - 8 - lw + 11, y0 + 49, x1 - 8 - lw + 20, y0 + 58), fill=col)
            self._t(d, (x1 - 8 - lw + 27, y0 + 53), label, self.f_ui, col, "lm")

        if not subtitle_overlay_history and not _overlay_preview:
            self._t(d, (x1 - 10, y1 - 8), "double-click: expand", self.f_small, (255, 255, 255, 90), "rd")

    # ---------- voice overlays (same behaviour as before, positioned inside the feed) ----------
    def _voice_overlays(self, d, rect):
        x0, y0, x1, y1 = rect
        if (x1 - x0) < 140 or (y1 - y0) < 120:
            return   # too small to hold subtitles legibly
        history = list(subtitle_overlay_history)
        if history:
            d.rectangle([x0 + 12, y1 - 75, x1 - 12, y1 - 12], fill=(10, 10, 15, 185))
            for idx, (msg, is_cmd) in enumerate(history):
                text_color = (0, 230, 118, 255) if is_cmd else (242, 242, 247, 255)
                y_pos = (y1 - 48) + (idx * 22)
                msg = _fit_text(d, msg, self.f_sub, (x1 - x0) - 48)
                self._t(d, (x0 + 21, y_pos + 1), msg, self.f_sub, (0, 0, 0, 220))
                self._t(d, (x0 + 20, y_pos), msg, self.f_sub, text_color)

        preview = _overlay_preview
        if preview:
            lines, rgb = preview
            line_h = 22
            panel_h = 12 + line_h * len(lines)
            bottom = (y1 - 75 - 6) if history else (y1 - 12)
            top = max(bottom - panel_h, y0 + 4)
            d.rectangle([x0 + 12, top, x1 - 12, bottom], fill=(10, 10, 15, 200), outline=rgb + (255,), width=2)
            max_w = (x1 - x0) - 48
            for i, line in enumerate(lines):
                line = _fit_text(d, line, self.f_sub, max_w)
                y = top + 6 + i * line_h
                self._t(d, (x0 + 21, y + 1), line, self.f_sub, (0, 0, 0, 220))
                self._t(d, (x0 + 20, y), line, self.f_sub, rgb + (255,))


# ----------------------------------------------------------------------------------------------------------------
# The window
# ----------------------------------------------------------------------------------------------------------------
class CustomCameraPreview(ctk.CTkToplevel):
    COMPACT_SIZE = (480, 360)
    FULL_SIZE = (1040, 600)
    FLASH_COLORS = {"ok": "#00e676", "fail": "#ff1744", "blocked": "#ffb74d"}

    def __init__(self, frame_queue, close_callback=None):
        super().__init__()
        self.frame_queue = frame_queue
        self.close_callback = close_callback

        self.title("HCI Overlay")
        self.geometry("480x360+1400+40")
        self.minsize(260, 200)

        self.overrideredirect(True)
        self.attributes("-topmost", True)

        self.bg_key = "#010101"
        self.configure(fg_color=self.bg_key)
        self.wm_attributes("-transparentcolor", self.bg_key)

        self.outer_border_frame = ctk.CTkFrame(
            self,
            fg_color="#12121A",
            border_color="#00FF00",
            border_width=4,
            corner_radius=16
        )
        self.outer_border_frame.pack(fill="both", expand=True)

        self.display_canvas = tk.Canvas(self.outer_border_frame, bg="#12121A", highlightthickness=0)
        self.display_canvas.pack(fill="both", expand=True, padx=5, pady=5)

        self.outer_border_frame.bind("<Button-1>", self.start_window_drag)
        self.outer_border_frame.bind("<B1-Motion>", self.execute_window_drag)
        self.display_canvas.bind("<Button-1>", self.start_window_drag)
        self.display_canvas.bind("<B1-Motion>", self.execute_window_drag)

        # double-click toggles compact <-> full HUD
        self.outer_border_frame.bind("<Double-Button-1>", self.toggle_mode)
        self.display_canvas.bind("<Double-Button-1>", self.toggle_mode)

        self.outer_border_frame.bind("<Double-Button-3>", lambda e: self.on_close())
        self.display_canvas.bind("<Double-Button-3>", lambda e: self.on_close())

        self.bind("<Motion>", self.check_resize_zone)
        self.bind("<ButtonPress-1>", self.start_window_resize, add="+")
        self.bind("<B1-Motion>", self.execute_window_resize, add="+")
        self.bind("<ButtonRelease-1>", self.stop_window_resize)

        self.renderer = HudRenderer()
        self.mode = "compact"
        self._last_frame = None
        self._last_draw = 0.0
        self._last_ui_ts = None
        self.ui_fps = 0.0
        self._photo = None
        self._photo_size = None
        self._canvas_item = None
        self._border_color = None
        self._flash_until = 0.0
        self._flash_color = "#00FF00"
        newest = hud_bus.get_events(1)
        self._last_seq = newest[0]["seq"] if newest else 0   # don't flash for commands from before this window opened
        self._draw_error_reported = False

        self._drag_start_x = 0
        self._drag_start_y = 0
        self.cursor_shape = "arrow"
        self.resize_active = False
        self.update_feed_loop()

    def start_window_drag(self, event):
        if self.cursor_shape == "arrow":
            self._drag_start_x = event.x
            self._drag_start_y = event.y

    def execute_window_drag(self, event):
        if self.cursor_shape == "arrow" and not self.resize_active:
            delta_x = event.x - self._drag_start_x
            delta_y = event.y - self._drag_start_y
            new_x = self.winfo_x() + delta_x
            new_y = self.winfo_y() + delta_y
            self.geometry(f"+{new_x}+{new_y}")

    def check_resize_zone(self, event):
        margin = 30
        w = self.winfo_width()
        h = self.winfo_height()

        if event.x >= (w - margin) and event.y >= (h - margin):
            self.config(cursor="size_nw_se")
            self.cursor_shape = "resize"
        else:
            self.config(cursor="arrow")
            self.cursor_shape = "arrow"

    def start_window_resize(self, event):
        if self.cursor_shape == "resize":
            self.resize_active = True
            self._resize_start_width = self.winfo_width()
            self._resize_start_height = self.winfo_height()
            self._resize_start_x = event.x_root
            self._resize_start_y = event.y_root

    def execute_window_resize(self, event):
        if self.resize_active:
            delta_w = event.x_root - self._resize_start_x
            delta_h = event.y_root - self._resize_start_y
            new_width = max(self._resize_start_width + delta_w, 260)
            new_height = max(self._resize_start_height + delta_h, 200)
            self.geometry(f"{new_width}x{new_height}")

    def stop_window_resize(self, event):
        self.resize_active = False

    def toggle_mode(self, event=None):
        """Switches between the compact overlay and the full HUD, keeping the window on screen."""
        self.mode = "full" if self.mode == "compact" else "compact"
        w, h = self.FULL_SIZE if self.mode == "full" else self.COMPACT_SIZE
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(w, sw - 40), min(h, sh - 80)
        x = max(0, min(self.winfo_x(), sw - w - 10))
        y = max(0, min(self.winfo_y(), sh - h - 40))
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _set_border(self, color):
        if color != self._border_color:
            self._border_color = color
            try:
                self.outer_border_frame.configure(border_color=color)
            except Exception:
                pass

    def update_feed_loop(self):
        now = time.time()

        # border: magenta in writing mode, otherwise green; flashes the status colour on each new command
        events = hud_bus.get_events(40)
        if events and events[0]["seq"] != self._last_seq:
            self._last_seq = events[0]["seq"]
            self._flash_color = self.FLASH_COLORS.get(events[0]["status"], "#00FF00")
            self._flash_until = now + 0.45
        writing = bool(getattr(system_state, "WRITING_MODE_ACTIVE", False))
        base = "#FF00FF" if writing else "#00FF00"
        self._set_border(self._flash_color if now < self._flash_until else base)

        # newest frame wins; older queued frames are dropped
        new_frame = False
        try:
            while True:
                self._last_frame = self.frame_queue.get_nowait()
                new_frame = True
        except queue.Empty:
            pass

        if new_frame:
            if self._last_ui_ts is not None:
                dt = now - self._last_ui_ts
                if dt > 0:
                    inst = 1.0 / dt
                    self.ui_fps = inst if self.ui_fps == 0.0 else 0.9 * self.ui_fps + 0.1 * inst
            self._last_ui_ts = now

        # redraw on every new frame, and at least 4x a second so timers and the paused state keep updating
        if new_frame or (now - self._last_draw) > 0.25:
            self._last_draw = now
            try:
                self._draw(now, events)
            except Exception as e:
                if not self._draw_error_reported:
                    self._draw_error_reported = True
                    print(f"[HUD] Draw error (further errors suppressed): {e}")

        self.after(30, self.update_feed_loop)

    def _draw(self, now, events):
        canvas_w = self.display_canvas.winfo_width()
        canvas_h = self.display_canvas.winfo_height()
        if canvas_w < 50 or canvas_h < 50:
            return

        img = self.renderer.render(self._last_frame, (canvas_w, canvas_h), self.mode,
                                   hud_bus.get_tracking(), events, now, self.ui_fps)

        if self._photo is None or self._photo_size != (canvas_w, canvas_h):
            self._photo = ImageTk.PhotoImage(image=img)
            self._photo_size = (canvas_w, canvas_h)
            if self._canvas_item is None:
                self._canvas_item = self.display_canvas.create_image(0, 0, anchor="nw", image=self._photo)
            else:
                self.display_canvas.itemconfig(self._canvas_item, image=self._photo)
        else:
            self._photo.paste(img)   # reuse the same Tk image instead of allocating one per frame

    def on_close(self):
        # the callback (STOP) usually destroys this window already; destroying twice raised a Tcl error
        if self.close_callback:
            self.close_callback()
        try:
            if self.winfo_exists():
                self.destroy()
        except Exception:
            pass