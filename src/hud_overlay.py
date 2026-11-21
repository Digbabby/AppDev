import tkinter as tk
import system_state

COMMAND_STYLE = ("🤖 SYSTEM COMMAND MODE", "#00FF00", "#1e1e1e")
WRITING_STYLE = ("📝 WRITING MODE ACTIVE", "#FFCC00", "#2d2200")


class MiniHUDOverlay:
    """Small always-on-top label in the top-right corner that shows the current mode.

    It is a Toplevel of the main window and runs on the main thread. (It used to create a second tk.Tk() and
    run its own mainloop in a background thread, which Tkinter does not support and which could crash the app
    or pull focus away from the dashboard.)
    """
    POLL_MS = 150
    WIDTH, HEIGHT, MARGIN = 240, 45, 25

    def __init__(self, master=None):
        self.root = tk.Toplevel(master) if master is not None else tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.85)

        x = self.root.winfo_screenwidth() - self.WIDTH - self.MARGIN
        self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{self.MARGIN}")

        self.status_label = tk.Label(self.root, font=("Consolas", 11, "bold"), padx=10, pady=10)
        self.status_label.pack(fill=tk.BOTH, expand=True)

        self._shown_writing = None          # forces the first paint
        system_state.hud_instance_ref = self
        self.sync_loop()

    def sync_loop(self):
        """Repaints only when the mode actually changed. It compares with the real mode flag instead of
        waiting for HUD_REFRESH_REQUIRED, so a mode switched by voice is shown too (voice never set that flag)."""
        writing = bool(system_state.WRITING_MODE_ACTIVE)
        if writing != self._shown_writing or system_state.HUD_REFRESH_REQUIRED:
            text, fg, bg = WRITING_STYLE if writing else COMMAND_STYLE
            self.status_label.config(text=text, fg=fg, bg=bg)
            self.root.config(bg=bg)
            self._shown_writing = writing
            system_state.HUD_REFRESH_REQUIRED = False
        self.root.after(self.POLL_MS, self.sync_loop)


def start_hud_process(master=None):
    """With a master window: creates the HUD on it and returns it (the normal case, from main.py).
    Without one: runs the HUD on its own, for testing this file by itself."""
    hud = MiniHUDOverlay(master)
    if master is None:
        hud.root.mainloop()
    return hud


if __name__ == "__main__":
    start_hud_process()