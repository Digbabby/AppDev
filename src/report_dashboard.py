import calendar
import math
import os
from datetime import date, timedelta
import customtkinter as ctk
import hud_bus

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

BG = "#0A0A0F"
CARD = "#12121A"
NAV = "#0F0F14"
ROW = "#181824"
TEXT = "#E2E1E9"
MUTED = "#6C6B80"
GREEN = "#00e676"
BLUE = "#29b6f6"
PURPLE = "#ab47bc"
AMBER = "#ffb74d"
RED = "#ff5252"
ACCENT = "#393676"
ACCENT_HOVER = "#55529e"
UNVERIFIED = "#3f8fd6"   # ran without an error, but no check could see its effect
TRACK = "#1F1F2E"


def rate_color(rate):
    
    if rate is None:
        return MUTED
    if rate >= 90:
        return GREEN
    if rate >= 60:
        return AMBER
    return RED


def _pct_text(value):
    return f"{value:.0f}%" if value is not None else "-"


def _ci_text(ci):
    return f"{ci[0]:.0f}\u2013{ci[1]:.0f}%" if ci else "-"


class StackBar(ctk.CTkFrame):
    """Thin bar split into verified (green) / unverified (blue) / failed (red) parts."""

    def __init__(self, parent, height=10, **kw):
        super().__init__(parent, fg_color=TRACK, corner_radius=height // 2, height=height, **kw)
        self.pack_propagate(False)
        self.parts = [ctk.CTkFrame(self, fg_color=c, corner_radius=height // 2, height=height)
                      for c in (GREEN, UNVERIFIED, RED)]

    def set_counts(self, confirmed, unverified, failed):
        total = confirmed + unverified + failed
        x = 0.0
        for part, n in zip(self.parts, (confirmed, unverified, failed)):
            part.place_forget()
            if total and n:
                w = n / total
                part.place(relx=x, rely=0, relwidth=w, relheight=1)
                x += w


# --------------------------------------------------------------------------- #
# Date range controls (no extra packages: the calendar is drawn with customtkinter)
# --------------------------------------------------------------------------- #
class CalendarPopup(ctk.CTkToplevel):
    """Small month calendar that appears under a button. Calls on_pick(date) and closes when a day is clicked."""

    def __init__(self, anchor, initial, on_pick, max_date=None):
        super().__init__(anchor.winfo_toplevel())
        self.title("Pick a date")
        self.resizable(False, False)
        self.configure(fg_color=CARD)
        self.attributes("-topmost", True)
        self.on_pick = on_pick
        self.max_date = max_date
        self.selected = initial
        self.view_year, self.view_month = initial.year, initial.month

        nav = ctk.CTkFrame(self, fg_color="transparent")
        nav.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkButton(nav, text="\u25C0", width=30, height=28, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=lambda: self._shift(-1)).pack(side="left")
        self.month_lbl = ctk.CTkLabel(nav, text="", font=ctk.CTkFont(size=13, weight="bold"), text_color=TEXT)
        self.month_lbl.pack(side="left", expand=True)
        ctk.CTkButton(nav, text="\u25B6", width=30, height=28, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=lambda: self._shift(1)).pack(side="right")

        self.grid_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.grid_frame.pack(padx=10, pady=(0, 10))
        self._render()

        self.update_idletasks()
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height() + 4
        x = min(x, max(0, self.winfo_screenwidth() - self.winfo_reqwidth() - 10))
        self.geometry(f"+{x}+{y}")
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(120, self._focus)

    def _focus(self):
        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _shift(self, delta):
        month = self.view_month - 1 + delta
        self.view_year += month // 12
        self.view_month = month % 12 + 1
        self._render()

    def _render(self):
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.month_lbl.configure(text=f"{calendar.month_name[self.view_month]} {self.view_year}")
        for col, name in enumerate(("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa")):
            ctk.CTkLabel(self.grid_frame, text=name, width=34, text_color=MUTED,
                         font=ctk.CTkFont(size=10, weight="bold")).grid(row=0, column=col, padx=1, pady=(0, 2))
        today = date.today()
        weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(self.view_year, self.view_month)
        for r, week in enumerate(weeks, start=1):
            for c, day in enumerate(week):
                if day == 0:
                    continue
                d = date(self.view_year, self.view_month, day)
                chosen = d == self.selected
                btn = ctk.CTkButton(
                    self.grid_frame, text=str(day), width=34, height=28, corner_radius=6,
                    fg_color=ACCENT if chosen else "transparent", hover_color=ACCENT_HOVER,
                    text_color="#ffffff" if chosen else TEXT,
                    border_width=1 if d == today else 0, border_color=GREEN,
                    command=lambda d=d: self._choose(d))
                if self.max_date and d > self.max_date:
                    btn.configure(state="disabled", text_color="#44445a")
                btn.grid(row=r, column=c, padx=1, pady=1)

    def _choose(self, d):
        self.destroy()
        self.on_pick(d)


class DateRangeBar(ctk.CTkFrame):
    """Preset menu (Today / Last 7 days / ...) plus From / To day pickers. Calls on_change() after a change."""
    PRESETS = ["Today", "Last 7 days", "Last 30 days", "This month", "Custom range"]
    CUSTOM = "Custom range"

    def __init__(self, parent, on_change, default="Last 7 days"):
        super().__init__(parent, fg_color="transparent")
        self.on_change = on_change
        self.preset = default
        self.start = self.end = date.today()

        self.preset_menu = ctk.CTkOptionMenu(
            self, values=self.PRESETS, width=130, height=30, fg_color=ACCENT, button_color=ACCENT,
            button_hover_color=ACCENT_HOVER, command=self._on_preset)
        self.preset_menu.pack(side="left", padx=(0, 10))
        self.preset_menu.set(default)

        ctk.CTkLabel(self, text="From", text_color=MUTED, font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 4))
        self.from_btn = ctk.CTkButton(self, text="", width=116, height=30, fg_color=CARD, hover_color="#1a1a26",
                                      border_width=1, border_color="#2c2c3e", text_color=TEXT,
                                      command=lambda: self._pick("start"))
        self.from_btn.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(self, text="To", text_color=MUTED, font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 4))
        self.to_btn = ctk.CTkButton(self, text="", width=116, height=30, fg_color=CARD, hover_color="#1a1a26",
                                    border_width=1, border_color="#2c2c3e", text_color=TEXT,
                                    command=lambda: self._pick("end"))
        self.to_btn.pack(side="left")
        self.recompute(notify=False)

    # --- range maths ---
    def recompute(self, notify=True):
        """Re-evaluates the preset against today's date (keeps 'Last 7 days' correct if the app stays open)."""
        today = date.today()
        if self.preset == "Today":
            self.start = self.end = today
        elif self.preset == "Last 7 days":
            self.start, self.end = today - timedelta(days=6), today
        elif self.preset == "Last 30 days":
            self.start, self.end = today - timedelta(days=29), today
        elif self.preset == "This month":
            self.start, self.end = today.replace(day=1), today
        self._update_texts()
        if notify:
            self.on_change()

    def _update_texts(self):
        self.from_btn.configure(text=self.start.strftime("%b %d, %Y"))
        self.to_btn.configure(text=self.end.strftime("%b %d, %Y"))

    def get_range(self):
        return self.start.isoformat(), self.end.isoformat()

    def span_days(self):
        return (self.end - self.start).days + 1

    # --- events ---
    def _on_preset(self, value):
        self.preset = value
        if value == self.CUSTOM:
            self._update_texts()
            self.on_change()
        else:
            self.recompute(notify=True)

    def _pick(self, which):
        anchor = self.from_btn if which == "start" else self.to_btn
        initial = self.start if which == "start" else self.end
        CalendarPopup(anchor, initial, lambda d: self._picked(which, d), max_date=date.today())

    def _picked(self, which, d):
        if which == "start":
            self.start = d
            if self.end < d:
                self.end = d
        else:
            self.end = d
            if self.start > d:
                self.start = d
        self.preset = self.CUSTOM
        self.preset_menu.set(self.CUSTOM)
        self._update_texts()
        self.on_change()


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
class ReportDashboard(ctk.CTkFrame):
    TABS = ("Profile", "History", "Progress")

    def __init__(self, parent_dashboard, base_dir=None, *args, **kwargs):
        super().__init__(parent_dashboard, fg_color=BG, corner_radius=0, *args, **kwargs)
        self.parent_dashboard = parent_dashboard
        self.base_dir = base_dir or os.getcwd()
        self.history_panel = None
        self.current_tab = "Profile"

        self.metrics = {}
        self.prev_metrics = {}
        self.daily = []
        self.cmd_stats = []
        self.tried = set()

        self.setup_layout()
        self.refresh_dashboard()

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------
    @staticmethod
    def friendly_name(raw):
        """Readable label for a logged action name (shared with the camera HUD)."""
        return hud_bus.friendly_name(raw)

    @staticmethod
    def available_actions():
        """Built-in commands a user can try (from the history log's catalogue of actions)."""
        try:
            from history_log import ACTION_INFO, SYSTEM_EVENT_ACTIONS
        except Exception:
            return []
        skip = set(SYSTEM_EVENT_ACTIONS) | {"launch_custom_executable", "launch_custom_url"}
        return sorted(a for a in ACTION_INFO if a not in skip)

    # ------------------------------------------------------------------
    # DATA
    # ------------------------------------------------------------------
    def gather_analytics(self):
        import database_manager as db

        start, end = self.range_bar.get_range()
        self.metrics = db.get_overview_metrics(start, end)
        self.daily = db.get_daily_activity(start, end)
        self.cmd_stats = db.get_command_stats(start, end)
        self.tried = db.get_tried_actions()

        # the period right before the selected one, same length (for the progress trend)
        span = self.range_bar.span_days()
        prev_end = self.range_bar.start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=span - 1)
        self.prev_metrics = db.get_overview_metrics(prev_start.isoformat(), prev_end.isoformat())

        try:
            names = [db.get_profile_data("first_name"), db.get_profile_data("last_name")]
            self.user_name = " ".join(n for n in names if n)
        except Exception:
            self.user_name = ""

    # ------------------------------------------------------------------
    # LAYOUT (built once)
    # ------------------------------------------------------------------
    def setup_layout(self):
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=20, pady=16)

        # ---------- Header ----------
        header = ctk.CTkFrame(wrap, fg_color="transparent")
        header.pack(fill="x", pady=(0, 10))

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left")
        ctk.CTkLabel(title_box, text="\U0001F464  PROFILE DASHBOARD",
                     font=ctk.CTkFont(family="Consolas", size=16, weight="bold"),
                     text_color=TEXT).pack(anchor="w")
        self.user_lbl = ctk.CTkLabel(title_box, text="", font=ctk.CTkFont(size=11), text_color=MUTED)
        self.user_lbl.pack(anchor="w")

        ctk.CTkButton(header, text="\u2715", width=30, height=30, corner_radius=8,
                      text_color="#ffffff", fg_color="#ff1744", hover_color="#ff4f70",
                      font=ctk.CTkFont(size=11, weight="bold"),
                      command=self.parent_dashboard.close_reports).pack(side="right", padx=(6, 0))
        ctk.CTkButton(header, text="\U0001F504 REFRESH", width=90, height=30, corner_radius=8,
                      fg_color=CARD, hover_color="#1a1a26", text_color=GREEN,
                      font=ctk.CTkFont(size=11, weight="bold"),
                      command=self.refresh_dashboard).pack(side="right", padx=6)
        ctk.CTkButton(header, text="\U0001F5D1\uFE0F RESET LOGS", width=100, height=30, corner_radius=8,
                      fg_color="#e65100", hover_color="#bf360c", text_color="#ffffff",
                      font=ctk.CTkFont(size=11, weight="bold"),
                      command=self.trigger_hard_database_reset).pack(side="right")

        # ---------- Tabs + date range ----------
        controls = ctk.CTkFrame(wrap, fg_color="transparent")
        controls.pack(fill="x", pady=(0, 10))
        self.tab_selector = ctk.CTkSegmentedButton(
            controls, values=list(self.TABS), command=self.select_tab, height=32,
            fg_color=CARD, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=CARD, unselected_hover_color="#1a1a26", text_color=TEXT,
            font=ctk.CTkFont(size=12, weight="bold"))
        self.tab_selector.pack(side="left")
        self.range_bar = DateRangeBar(controls, on_change=self.refresh_dashboard)
        self.range_bar.pack(side="right")

        # ---------- Tab bodies ----------
        body = ctk.CTkFrame(wrap, fg_color="transparent")
        body.pack(fill="both", expand=True)
        self.tab_frames = {name: ctk.CTkFrame(body, fg_color="transparent") for name in self.TABS}
        self._build_profile_tab(self.tab_frames["Profile"])
        self._build_history_tab(self.tab_frames["History"])
        self._build_progress_tab(self.tab_frames["Progress"])
        self.select_tab("Profile")

    def _build_profile_tab(self, tab):
        # ---------- Metric cards ----------
        cards_frame = ctk.CTkFrame(tab, fg_color="transparent")
        cards_frame.pack(fill="x", pady=(0, 10))

        self.card_value, self.card_caption = {}, {}
        specs = [
            ("sessions", "SESSIONS DONE", BLUE),
            ("commands", "COMMANDS DONE", GREEN),
            ("rate", "VERIFIED RATE", GREEN),
            ("failed", "FAILED", RED),
            ("avg", "AVG SESSION", PURPLE),
        ]
        for idx, (key, title, color) in enumerate(specs):
            card = ctk.CTkFrame(cards_frame, fg_color=CARD, corner_radius=12, height=92)
            card.pack(side="left", fill="x", expand=True, padx=(0 if idx == 0 else 5, 0 if idx == len(specs) - 1 else 5))
            card.pack_propagate(False)
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=10, weight="bold"),
                         text_color=MUTED).pack(anchor="w", padx=12, pady=(8, 0))
            value = ctk.CTkLabel(card, text="0", font=ctk.CTkFont(family="Consolas", size=22, weight="bold"),
                                 text_color=color)
            value.pack(anchor="w", padx=12)
            caption = ctk.CTkLabel(card, text="", font=ctk.CTkFont(size=10), text_color=MUTED)
            caption.pack(anchor="w", padx=12, pady=(0, 4))
            self.card_value[key] = value
            self.card_caption[key] = caption

        # ---------- Voice vs Gesture split ----------
        split_card = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=12)
        split_card.pack(fill="x", pady=(0, 10))
        split_top = ctk.CTkFrame(split_card, fg_color="transparent")
        split_top.pack(fill="x", padx=14, pady=(8, 4))
        ctk.CTkLabel(split_top, text="INPUT SPLIT", font=ctk.CTkFont(size=10, weight="bold"),
                     text_color=MUTED).pack(side="left")
        self.split_text_lbl = ctk.CTkLabel(split_top, text="", font=ctk.CTkFont(family="Consolas", size=10),
                                           text_color=TEXT)
        self.split_text_lbl.pack(side="right")
        bar_holder = ctk.CTkFrame(split_card, fg_color="#1F1F2E", corner_radius=5, height=10)
        bar_holder.pack(fill="x", padx=14, pady=(0, 10))
        bar_holder.pack_propagate(False)
        self.split_voice_bar = ctk.CTkFrame(bar_holder, fg_color=PURPLE, corner_radius=5)
        self.split_gesture_bar = ctk.CTkFrame(bar_holder, fg_color=GREEN, corner_radius=5)

        # ---------- Chart + Top Commands ----------
        data_row = ctk.CTkFrame(tab, fg_color="transparent")
        data_row.pack(fill="both", expand=True)

        # pack the list card FIRST on the right so the chart can't squeeze it out
        list_card = ctk.CTkFrame(data_row, width=310, fg_color=CARD, corner_radius=12)
        list_card.pack(side="right", fill="y", padx=(8, 0))
        list_card.pack_propagate(False)
        ctk.CTkLabel(list_card, text="Top Commands", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=TEXT).pack(anchor="w", padx=15, pady=10)
        self.list_rows_frame = ctk.CTkFrame(list_card, fg_color="transparent")
        self.list_rows_frame.pack(fill="both", expand=True)

        chart_card = ctk.CTkFrame(data_row, fg_color=CARD, corner_radius=12)
        chart_card.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ctk.CTkLabel(chart_card, text="Daily Activity (verified \u00b7 unverified \u00b7 failed)",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=TEXT).pack(anchor="w", padx=15, pady=(10, 0))

        # Standalone Figure (no pyplot): no global matplotlib state, nothing to leak between refreshes.
        self.fig = Figure(figsize=(5.2, 2.8), dpi=100, facecolor=CARD)
        try:
            self.fig.set_layout_engine("constrained")
        except Exception:
            pass
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_card)   # resizes with the window automatically
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.configure(bg=CARD, highlightthickness=0)
        self.canvas_widget.pack(fill="both", expand=True, padx=10, pady=10)
        self.chart_empty_lbl = ctk.CTkLabel(
            chart_card, text="No activity in this date range.\nUse a gesture or voice command, or pick a wider range.",
            font=ctk.CTkFont(size=12), text_color=MUTED, justify="center", fg_color=CARD)

    def _build_history_tab(self, tab):
        try:
            from history_log import HistoryPanel   # lazy: a problem there can't stop the dashboard from opening
            self.history_panel = HistoryPanel(tab, base_dir=self.base_dir)
            self.history_panel.pack(fill="both", expand=True)
        except Exception as e:
            self.history_panel = None
            ctk.CTkLabel(tab, text=f"The history log could not be loaded:\n{e}", text_color=RED,
                         font=ctk.CTkFont(size=12), justify="center").pack(expand=True)

    def _build_progress_tab(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", pady=(0, 10))

        def summary_card(side_pad, title, stacked=False, bars=1):
            card = ctk.CTkFrame(top, fg_color=CARD, corner_radius=12, height=132)
            card.pack(side="left", fill="x", expand=True, padx=side_pad)
            card.pack_propagate(False)
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=10, weight="bold"),
                         text_color=MUTED).pack(anchor="w", padx=14, pady=(10, 0))
            value = ctk.CTkLabel(card, text="-", font=ctk.CTkFont(family="Consolas", size=22, weight="bold"),
                                 text_color=GREEN, justify="left", anchor="w")
            value.pack(anchor="w", padx=14)
            if stacked and bars > 1:     # one labelled bar per input
                bar = []
                for label in ("Voice", "Gesture")[:bars]:
                    line = ctk.CTkFrame(card, fg_color="transparent", height=14)
                    line.pack(fill="x", padx=14, pady=(2, 0))
                    ctk.CTkLabel(line, text=label, width=52, anchor="w", height=14,
                                 font=ctk.CTkFont(size=9, weight="bold"), text_color=MUTED).pack(side="left")
                    b = StackBar(line, height=8)
                    b.pack(side="left", fill="x", expand=True)
                    bar.append(b)
            elif stacked:
                bar = StackBar(card)
            else:
                bar = ctk.CTkProgressBar(card, height=10, corner_radius=5, fg_color=TRACK, progress_color=GREEN)
                bar.set(0)
            if not isinstance(bar, list):
                bar.pack(fill="x", padx=14, pady=(2, 4))
            caption = ctk.CTkLabel(card, text="", font=ctk.CTkFont(size=10), text_color=MUTED,
                                   justify="left", anchor="w", wraplength=300)
            caption.pack(anchor="w", padx=14)
            # wrap the caption to the card's real width
            card.bind("<Configure>", lambda e, c=caption: c.configure(wraplength=max(160, e.width - 30)), add="+")
            return value, bar, caption

        self.prog_rate_value, self.prog_rate_bar, self.prog_rate_caption = summary_card(
            (0, 5), "VERIFIED EXECUTION RATE", stacked=True)
        self.prog_input_value, self.prog_input_bar, self.prog_input_caption = summary_card(
            (5, 5), "VERIFIED RATE BY INPUT", stacked=True, bars=2)
        self.prog_explore_value, self.prog_explore_bar, self.prog_explore_caption = summary_card(
            (5, 0), "COMMANDS EXPLORED (ALL TIME)")

        list_card = ctk.CTkFrame(tab, fg_color=CARD, corner_radius=12)
        list_card.pack(fill="both", expand=True)
        head = ctk.CTkFrame(list_card, fg_color="transparent")
        head.pack(fill="x", padx=15, pady=(10, 2))
        ctk.CTkLabel(head, text="Results per command", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=TEXT).pack(side="left")
        legend = ctk.CTkFrame(head, fg_color="transparent")
        legend.pack(side="right")
        for color, label in ((GREEN, "verified: a check saw it work"),
                             (UNVERIFIED, "unverified: ran, effect can't be checked"),
                             (RED, "failed")):
            ctk.CTkFrame(legend, fg_color=color, width=10, height=10, corner_radius=3).pack(side="left", padx=(12, 4))
            ctk.CTkLabel(legend, text=label, font=ctk.CTkFont(size=10), text_color=MUTED).pack(side="left")
        self.progress_rows = ctk.CTkScrollableFrame(list_card, fg_color="transparent")
        self.progress_rows.pack(fill="both", expand=True, padx=6, pady=(2, 8))

    # ------------------------------------------------------------------
    # RENDER (updates the existing widgets in place)
    # ------------------------------------------------------------------
    def render_data(self):
        m = self.metrics
        commands, sessions = m.get("commands", 0), m.get("sessions", 0)

        self.user_lbl.configure(text=(f"{self.user_name}   \u00b7   " if self.user_name else "") +
                                     "sessions, commands and progress for the selected dates")

        self.card_value["sessions"].configure(text=str(sessions))
        self.card_caption["sessions"].configure(
            text="each START \u2192 STOP of the engine" if sessions else "none in this date range")
        self.card_value["commands"].configure(text=str(commands))
        self.card_caption["commands"].configure(text=f"gesture {m.get('gesture', 0)}  \u00b7  voice {m.get('voice', 0)}")

        rel = m.get("reliability") or {}
        vr = rel.get("verified_rate")
        self.card_value["rate"].configure(text=_pct_text(vr), text_color=GREEN if vr is not None else MUTED)
        self.card_caption["rate"].configure(
            text=f"95% CI {_ci_text(rel.get('verified_ci'))}  \u00b7  ran {_pct_text(rel.get('execution_rate'))}"
            if commands else "no commands yet")

        failed = m.get("failed", 0)
        self.card_value["failed"].configure(text=str(failed), text_color=RED if failed else GREEN)
        self.card_caption["failed"].configure(
            text="see History \u2192 Result: Failed" if failed else ("every command worked" if commands else "no commands yet"))

        self.card_value["avg"].configure(text=f"{m.get('avg_session_min', 0.0)}m")
        per = round(commands / sessions, 1) if sessions else 0.0
        self.card_caption["avg"].configure(text=f"{per} commands per session")

        self.render_split_bar()
        self.render_top_commands()
        self.render_chart()
        self.render_progress()

    def render_split_bar(self):
        voice, gesture = self.metrics.get("voice", 0), self.metrics.get("gesture", 0)
        total = voice + gesture

        self.split_voice_bar.place_forget()
        self.split_gesture_bar.place_forget()
        if total <= 0:
            self.split_text_lbl.configure(text="No commands in this range")
            return

        voice_pct = voice / total
        self.split_voice_bar.place(relx=0, rely=0, relwidth=voice_pct, relheight=1)
        self.split_gesture_bar.place(relx=voice_pct, rely=0, relwidth=1 - voice_pct, relheight=1)
        self.split_text_lbl.configure(
            text=f"Voice {voice} ({round(voice_pct * 100)}%)   \u00b7   Gesture {gesture} ({round((1 - voice_pct) * 100)}%)")

    def render_top_commands(self):
        for child in self.list_rows_frame.winfo_children():
            child.destroy()

        if not self.cmd_stats:
            ctk.CTkLabel(self.list_rows_frame, text="Nothing logged in this range.", font=ctk.CTkFont(size=11),
                         text_color=MUTED).pack(pady=20)
            return

        for item in self.cmd_stats[:5]:
            row = ctk.CTkFrame(self.list_rows_frame, fg_color=ROW, corner_radius=6, height=36)
            row.pack(fill="x", padx=10, pady=3)
            row.pack_propagate(False)

            ctk.CTkLabel(row, text=f"{item['total']}x", font=ctk.CTkFont(family="Consolas", size=10, weight="bold"),
                         text_color=BLUE).pack(side="right", padx=(4, 8))
            if item["failed"]:
                ctk.CTkLabel(row, text=f"{item['failed']} failed", font=ctk.CTkFont(size=9, weight="bold"),
                             text_color=RED).pack(side="right", padx=4)
            elif item.get("verified"):
                ctk.CTkLabel(row, text=f"{item['verified']} verified", font=ctk.CTkFont(size=9),
                             text_color=GREEN).pack(side="right", padx=4)
            else:
                ctk.CTkLabel(row, text="ran ok", font=ctk.CTkFont(size=9), text_color=UNVERIFIED).pack(side="right", padx=4)

            name = self.friendly_name(item["action"])
            ctk.CTkLabel(row, text=name if len(name) <= 16 else name[:15] + "\u2026",
                         font=ctk.CTkFont(size=10, weight="bold"), text_color=TEXT).pack(side="left", padx=8)

    def render_chart(self):
        """One stacked bar per day: green = verified, blue = ran but unverified, red = failed."""
        fig = self.fig
        fig.clear()

        if not any(d["ok"] + d["failed"] for d in self.daily):
            self.canvas_widget.pack_forget()
            self.chart_empty_lbl.pack(fill="both", expand=True, padx=10, pady=10)
            return
        self.chart_empty_lbl.pack_forget()
        self.canvas_widget.pack(fill="both", expand=True, padx=10, pady=10)

        ax = fig.add_subplot(111)
        ax.set_facecolor(CARD)

        n = len(self.daily)
        xs = list(range(n))
        ok = [d["ok"] for d in self.daily]
        conf = [d.get("verified", 0) for d in self.daily]
        unver = [o - c for o, c in zip(ok, conf)]
        bad = [d["failed"] for d in self.daily]
        width = 0.6 if n > 1 else 0.3
        gap = dict(edgecolor=CARD, linewidth=1.5)   # thin gap between the stacked parts
        ax.bar(xs, conf, color=GREEN, width=width, label="Verified", **gap)
        ax.bar(xs, unver, bottom=conf, color=UNVERIFIED, width=width, label="Unverified", **gap)
        ax.bar(xs, bad, bottom=ok, color=RED, width=width, label="Failed", **gap)
        ax.set_ylim(0, max(max(o + b for o, b in zip(ok, bad)), 3) * 1.2)
        if n == 1:
            ax.set_xlim(-1, 1)

        # keep the x axis readable however many days there are
        step = max(1, math.ceil(n / 8))
        ticks = xs[::step]
        ax.set_xticks(ticks)
        ax.set_xticklabels([self.daily[i]["label"] for i in ticks])
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))

        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#2d2d3d")
        ax.tick_params(colors=MUTED, labelsize=9)
        ax.grid(True, axis="y", linestyle="--", alpha=0.1, color="#ffffff")

        handles, labels = ax.get_legend_handles_labels()
        if handles:
            try:
                legend = fig.legend(handles, labels, loc="outside lower center", ncol=3, fontsize=8,
                                    facecolor=CARD, edgecolor="none")
            except Exception:
                legend = ax.legend(handles, labels, loc="upper left", ncol=1, fontsize=8,
                                   facecolor=CARD, edgecolor="none")
            for text in legend.get_texts():
                text.set_color(TEXT)

        self.canvas.draw_idle()

    def render_progress(self):
        m, prev = self.metrics, self.prev_metrics

        # ---- verified execution rate (+ trend against the previous period of the same length) ----
        # C = confirmed by a check, U = ran but unverifiable, F = failed, N = C + U + F
        rel = m.get("reliability") or {}
        vr = rel.get("verified_rate")
        self.prog_rate_value.configure(text=_pct_text(vr), text_color=GREEN if vr is not None else MUTED)
        self.prog_rate_bar.set_counts(rel.get("confirmed", 0), rel.get("unverified", 0), rel.get("failed", 0))
        if vr is None:
            caption = "No commands in this date range yet."
        else:
            n = rel["total"]
            caption = (f"{rel['confirmed']} of {n} commands were confirmed by a check "
                       f"(95% CI {_ci_text(rel.get('verified_ci'))}).\n"
                       f"Ran without failing: {_pct_text(rel.get('execution_rate'))}  \u00b7  ")
            if rel.get("checkable_rate") is not None:
                caption += (f"worked when checkable: {_pct_text(rel['checkable_rate'])} "
                            f"({rel['confirmed']} of {rel['confirmed'] + rel['failed']})")
            else:
                caption += "none of these commands could be checked"
            prev_vr = (prev.get("reliability") or {}).get("verified_rate")
            if prev_vr is None:
                caption += "\nNo commands in the previous period to compare with."
            else:
                diff = round(vr - prev_vr, 1)
                arrow = "\u25B2" if diff > 0 else ("\u25BC" if diff < 0 else "=")
                caption += (f"\n{arrow} {abs(diff):g} points vs the previous {self.range_bar.span_days()} "
                            f"day(s) ({prev_vr:.0f}%).")
        self.prog_rate_caption.configure(text=caption)

        # ---- the same rate per input (voice vs gesture) ----
        by = m.get("by_trigger") or {}
        voice, gesture = by.get("voice") or {}, by.get("gesture") or {}
        if not voice.get("total") and not gesture.get("total"):
            self.prog_input_value.configure(text="-", text_color=MUTED)
            for bar in self.prog_input_bar:
                bar.set_counts(0, 0, 0)
            self.prog_input_caption.configure(text="No commands in this date range yet.")
        else:
            self.prog_input_value.configure(
                text=f"V {_pct_text(voice.get('verified_rate'))}  \u00b7  G {_pct_text(gesture.get('verified_rate'))}",
                text_color=TEXT)
            for bar, r in zip(self.prog_input_bar, (voice, gesture)):
                bar.set_counts(r.get("confirmed", 0), r.get("unverified", 0), r.get("failed", 0))
            lines = []
            for label, r in (("Voice", voice), ("Gesture", gesture)):
                if r.get("total"):
                    lines.append(f"{label}: {r['confirmed']} verified \u00b7 {r['unverified']} unverified \u00b7 "
                                 f"{r['failed']} failed  (n={r['total']}, CI {_ci_text(r.get('verified_ci'))})")
                else:
                    lines.append(f"{label}: no commands")
            self.prog_input_caption.configure(text="\n".join(lines))

        # ---- how many built-in commands have been used successfully at least once ----
        available = self.available_actions()
        if available:
            tried = [a for a in available if a in self.tried]
            untried = [a for a in available if a not in self.tried]
            share = len(tried) / len(available)
            self.prog_explore_value.configure(text=f"{len(tried)} / {len(available)}")
            self.prog_explore_bar.set(share)
            hint = "Try next: " + ", ".join(self.friendly_name(a) for a in untried[:6]) if untried else "You have used every built-in command."
            self.prog_explore_caption.configure(text=f"{round(share * 100)}% of the built-in commands used successfully.  {hint}")
        else:
            self.prog_explore_value.configure(text="-")
            self.prog_explore_bar.set(0)
            self.prog_explore_caption.configure(text="The command catalogue could not be loaded.")

        # ---- per-command success bars ----
        for child in self.progress_rows.winfo_children():
            child.destroy()
        if not self.cmd_stats:
            ctk.CTkLabel(self.progress_rows, text="No commands in this date range.", font=ctk.CTkFont(size=11),
                         text_color=MUTED).pack(pady=24)
            return
        for item in sorted(self.cmd_stats, key=lambda s: (-s["total"], s["action"])):
            conf, unver = item.get("verified", 0), item.get("unverified", item["ok"])
            row = ctk.CTkFrame(self.progress_rows, fg_color=ROW, corner_radius=8, height=38)
            row.pack(fill="x", padx=4, pady=3)
            row.pack_propagate(False)
            name = self.friendly_name(item["action"])
            ctk.CTkLabel(row, text=name if len(name) <= 26 else name[:25] + "\u2026", width=200, anchor="w",
                         font=ctk.CTkFont(size=11, weight="bold"), text_color=TEXT).pack(side="left", padx=(10, 6))
            if conf + item["failed"]:      # a check could judge at least one run
                text = f"verified {conf}/{item['total']}  \u00b7  {_pct_text(item.get('verified_rate'))}"
                color = rate_color(item.get("checkable_rate"))
            else:
                text = f"ran {item['ok']}/{item['total']}  \u00b7  not checkable"
                color = UNVERIFIED
            ctk.CTkLabel(row, text=text, width=230, anchor="e",
                         font=ctk.CTkFont(family="Consolas", size=11, weight="bold"),
                         text_color=color).pack(side="right", padx=(6, 10))
            bar = StackBar(row)
            bar.pack(side="left", fill="x", expand=True, padx=6)
            bar.set_counts(conf, unver, item["failed"])

    # ------------------------------------------------------------------
    # ACTIONS
    # ------------------------------------------------------------------
    def select_tab(self, name):
        if name not in self.tab_frames:
            return
        for frame in self.tab_frames.values():
            frame.pack_forget()
        self.tab_frames[name].pack(fill="both", expand=True)
        self.tab_selector.set(name)
        self.current_tab = name
        if name == "History" and self.history_panel is not None:
            self.history_panel.refresh(force=True)
        elif name == "Profile":
            self.canvas.draw_idle()

    def trigger_hard_database_reset(self):
        """Asks first, then drops all logs and refreshes the view."""
        from tkinter import messagebox
        if not messagebox.askyesno("Reset logs",
                                   "Delete ALL command logs and session history?\nThis cannot be undone.",
                                   parent=self):
            return
        from database_manager import clear_all_analytics_logs
        if clear_all_analytics_logs():
            self.refresh_dashboard()

    def refresh_dashboard(self):
        """Re-reads the database for the selected date range and updates every tab in place."""
        self.range_bar.recompute(notify=False)
        self.gather_analytics()
        self.render_data()
        if self.history_panel is not None:
            start, end = self.range_bar.get_range()
            self.history_panel.set_range(start, end)


ProfileDashboard = ReportDashboard   # clearer name for new code; main.py keeps using ReportDashboard