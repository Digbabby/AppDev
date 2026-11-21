"""
camera_health.py - keeps the webcam feed alive.

Why this exists: the tracker only noticed a camera problem when cap.read() FAILED. A faulty camera or
driver usually keeps "succeeding" while it sends black, frozen or garbled (static) frames, so the app never
reacted and the user had to stop/start the engine by hand. This module:
  * opens the camera with settings that are lighter on the hardware (MJPG, 640x480, 30 fps)
  * checks every frame for black / frozen / garbled output
  * restarts the camera automatically when a problem lasts more than a moment
    (the same thing as turning the camera off and on by hand), with a back-off so it never thrashes

Use in vision_module.py:
    keeper = CameraKeeper()
    cap = keeper.open()
    ...
    success, frame = keeper.read()      # instead of cap.read()
"""
import time

import cv2
import numpy as np

try:
    import hud_bus
except Exception:
    hud_bus = None

# Order the camera drivers are tried in. DirectShow first keeps the old behaviour.
# If the glitch keeps happening, try putting Media Foundation first: many newer Intel laptop cameras
# (MIPI / IPU cameras) are more stable through it.
BACKENDS = (
    ("DirectShow", cv2.CAP_DSHOW),
    ("Media Foundation", cv2.CAP_MSMF),
    ("default", cv2.CAP_ANY),
)
CAMERA_INDEXES = (0, 1, 2)
USE_MJPG = True                 # compressed frames = less USB bandwidth; ignored by cameras that don't support it
WIDTH, HEIGHT, FPS = 640, 480, 30

DEBUG = False                   # True = print brightness / noise numbers once a second (to tune NOISE_LEVEL)


def _configure(cap):
    """Asks for a light, common video mode. Every setting is optional; unsupported ones are ignored."""
    try:
        if USE_MJPG:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))   # must come before the size
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, FPS)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)    # always hand over the newest frame, not a stale one
    except Exception:
        pass


def open_camera():
    """Opens the first webcam that really delivers a picture. Raises RuntimeError when none works."""
    for index in CAMERA_INDEXES:
        for name, backend in BACKENDS:
            cap = cv2.VideoCapture(index, backend)
            if cap.isOpened():
                _configure(cap)
                for _ in range(10):                 # some webcams need a moment before the first frame
                    ok, _frame = cap.read()
                    if ok:
                        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        print(f"[CAMERA] Opened camera {index} using {name} ({w}x{h})")
                        return cap
                    time.sleep(0.1)
            cap.release()
    raise RuntimeError("No working camera found. Close other apps that use the webcam (Teams, Zoom, Camera app, "
                       "a previous copy of this program) and check Windows Settings > Privacy > Camera.")


class FrameHealth:
    """Decides whether frames look broken. A problem must last a while before it counts,
    so a hand moving fast, a quick flash or a covered lens for a second never trigger a restart."""
    BLACK_MEAN = 10          # average brightness below this...
    BLACK_STD = 4            # ...with almost no detail = black frame
    NOISE_LEVEL = 35         # average jump between neighbouring pixels: normal scenes ~3-15, a harsh pattern ~25,
                             # static like your screenshot ~60+. Turn DEBUG on to see your own numbers.
    BAD_SECONDS = 1.5        # how long black / garbled output must last
    FROZEN_SECONDS = 3.0     # how long the picture must be pixel-for-pixel identical

    def __init__(self):
        self._last_debug = 0.0
        self.reset()

    def reset(self):
        self.prev = None
        self.bad_since = None
        self.same_since = None

    @staticmethod
    def measure(frame):
        """(small gray image, brightness, contrast, noise) for one BGR frame."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        g = gray.astype(np.int16)
        vertical = float(np.abs(np.diff(g[:, ::2], axis=0)).mean())     # row to row
        horizontal = float(np.abs(np.diff(g[::2, :], axis=1)).mean())   # column to column
        small = gray[::8, ::8]
        return small, float(gray.mean()), float(gray.std()), max(vertical, horizontal)

    def check(self, frame, now=None):
        """Returns 'black', 'frozen' or 'garbled' once the camera should be restarted, otherwise None."""
        now = time.monotonic() if now is None else now
        small, mean, std, noise = self.measure(frame)

        if DEBUG and now - self._last_debug >= 1.0:
            self._last_debug = now
            print(f"[CAMERA HEALTH] brightness {mean:.0f}  contrast {std:.0f}  noise {noise:.1f} "
                  f"(garbled above {self.NOISE_LEVEL})")

        # frozen: a live sensor always changes a little, so an identical picture means the driver stalled
        if self.prev is not None and self.prev.shape == small.shape and np.array_equal(small, self.prev):
            if self.same_since is None:
                self.same_since = now
            elif now - self.same_since >= self.FROZEN_SECONDS:
                return "frozen"
        else:
            self.same_since = None
        self.prev = small

        if mean < self.BLACK_MEAN and std < self.BLACK_STD:
            problem = "black"
        elif noise > self.NOISE_LEVEL:
            problem = "garbled"
        else:
            self.bad_since = None
            return None

        if self.bad_since is None:
            self.bad_since = now
            return None
        return problem if now - self.bad_since >= self.BAD_SECONDS else None


class CameraKeeper:
    """Owns the webcam: reads frames, spots a broken feed and restarts the camera by itself."""
    FAILED_READS_BEFORE_RESTART = 30     # ~0.6 s without any frame
    QUICK_RETRY = 3.0                    # seconds between restarts normally
    SLOW_RETRY = 30.0                    # after several restarts in a row didn't help
    MAX_OPEN_FAILURES = 10               # reopening failed this many times in a row -> give up (shows Camera: Error)

    def __init__(self):
        self.cap = None
        self.health = FrameHealth()
        self.failed_reads = 0
        self.next_restart_at = 0.0
        self.recent_restarts = []
        self.open_failures = 0
        self.total_restarts = 0

    def open(self):
        self.cap = open_camera()
        self.health.reset()
        self.failed_reads = 0
        return self.cap

    def read(self):
        """Like cap.read(). Returns (False, None) while the camera is broken or being restarted."""
        if self.cap is None:
            self._restart("not connected")
            time.sleep(0.05)
            return False, None

        ok, frame = self.cap.read()
        if not ok or frame is None:
            self.failed_reads += 1
            if self.failed_reads >= self.FAILED_READS_BEFORE_RESTART:
                self._restart("not sending frames")
            else:
                time.sleep(0.02)
            return False, None
        self.failed_reads = 0

        problem = self.health.check(frame)
        if problem:
            self._restart(problem)
            return False, None
        return True, frame

    def _restart(self, reason):
        now = time.monotonic()
        if now < self.next_restart_at:
            return
        self.total_restarts += 1
        print(f"[CAMERA] Feed looks {reason}. Restarting the camera (restart #{self.total_restarts})...")
        if hud_bus is not None:
            try:
                hud_bus.push_event("Vision", "camera_restart", "blocked", reason)
            except Exception:
                pass

        self.release()
        time.sleep(0.8)                     # give the driver a moment, like unplugging and replugging
        try:
            self.open()
            self.open_failures = 0
        except RuntimeError:
            self.cap = None
            self.open_failures += 1
            if self.open_failures >= self.MAX_OPEN_FAILURES:
                raise RuntimeError("The camera keeps failing and could not be reopened. "
                                   "Check the camera in Windows Camera / Device Manager.")

        # back off when restarts keep happening, so a truly dead camera doesn't get hammered
        self.recent_restarts = [t for t in self.recent_restarts if now - t < 60.0] + [now]
        wait = self.SLOW_RETRY if len(self.recent_restarts) >= 3 else self.QUICK_RETRY
        self.next_restart_at = time.monotonic() + wait
        self.health.reset()

    def release(self):
        cap, self.cap = self.cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
