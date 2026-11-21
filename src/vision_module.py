import cv2
import mediapipe as mp
import time
import json
import pyautogui
import numpy as np
import os
import sys
import keyboard
import queue
from mediapipe.tasks import python
from mediapipe.tasks.python import vision
import system_state
from camera_health import CameraKeeper

def get_dispatcher():
    """The action dispatcher, imported on first use (a top-level import would be circular)."""
    import action_dispatcher
    return action_dispatcher

try:
    import hud_bus
except Exception:
    hud_bus = None


def _hud_track(**fields):
    """Sends this frame's tracking data to the camera HUD (silently does nothing without hud_bus)."""
    if hud_bus is not None:
        try:
            hud_bus.set_tracking(**fields)
        except Exception:
            pass


pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.02   # default is 0.1 s after EVERY call, which froze the tracking loop on each key press / scroll


def _safe_gui(func, *args, **kwargs):
    """Runs a pyautogui call from inside the tracking loop. With FAILSAFE on, moving the mouse into a screen corner
    raises an exception; uncaught, it used to kill the whole tracker thread. Now that frame's input is skipped."""
    try:
        func(*args, **kwargs)
        return True
    except Exception as e:
        print(f"[VISION] Input skipped: {type(e).__name__}: {e}")
        return False

COOLDOWN_DEFAULT = 2.2        # play/pause and the writing-mode toggle
SWIPE_COOLDOWN = 0.3
TRACK_CHANGE_COOLDOWN = 1.5   # was 10.0s, which made 2nd swipe look dead

# Gesture timing is measured in SECONDS (not frames) so it feels the same at 12 FPS or 30 FPS.
SWIPE_WINDOW_SECONDS = 0.8    # how far back a swipe is measured (was 10 frames = ~0.8 s at the old ~12 FPS)
STREAK_SECONDS = 0.3          # how long the same gesture must be held before it fires (was 4 frames)
HAND_CONFIRM_SECONDS = 0.2    # how long a hand must be tracked before it counts (was 3 frames)
SCROLL_REF_DT = 0.18          # scroll update interval the speeds were tuned at (~83 ms frame + 0.1 s pyautogui pause)
STATS_PRINT_EVERY = 5.0       # seconds between the [VISION] speed report in the console

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ENGINE_ACTIVE = True
WAKE_HOLD_START = None
WAKE_HOLD_DURATION = 0.4
WAKE_NEEDS_RELEASE = False   # after a lock/unlock toggle the index point must be released before the next one
# The 3-finger pinch (toggle writing mode) used to fire on a SINGLE frame. While the hand moved into or out of a
# victory sign, one stray frame was enough to flip writing mode. It now has to be held, like the wake gesture.
PINCH_HOLD_START = None
PINCH_HOLD_DURATION = 0.5    # seconds the pinch must be held before it toggles
PINCH_NEEDS_RELEASE = False  # after a toggle the pinch must be released before the next one
# A real 3-finger pinch has index and middle tips touching the thumb, so they are close to EACH OTHER. A victory
# sign has them spread (> 0.40). Requiring this makes the two poses impossible to confuse.
PINCH_MAX_TIP_GAP = 0.35
active_intent = None
last_swipe_timestamp = 0
INTENT_RESET_DELAY = 0.5
GESTURE_COUNT = 0
TRACKER_LOOP_ACTIVE = False

# Actions the vision loop runs itself (immediately, with their own cooldowns) instead of after the hold streak.
IMMEDIATE_ACTIONS = {"volume_up", "volume_down", "previous_track", "next_track", "media_play_pause"}
# Gestures with built-in behaviour that is not "run the mapped action".
SPECIAL_GESTURES = {"index_point", "two_finger_scroll", "three_finger_pinch"}
# Presets that only flip the voice lock (handled in run_custom_action, they never reach the dispatcher).
VOICE_LOCK_ACTIONS = {"voice_lock", "voice_unlock"}
# Mapped actions that repeat while the pose is held (one step per hold streak), as before.
REPEATING_ACTIONS = {"maximize", "minimize", "zoom_in", "zoom_out"}

def toggle_engine():
    global ENGINE_ACTIVE
    ENGINE_ACTIVE = not ENGINE_ACTIVE
    print(f"\n[SYSTEM] Gesture Engine {'ENABLED' if ENGINE_ACTIVE else 'MUTED/PAUSED via Hotkey'}\n")

keyboard.add_hotkey('ctrl+shift+s', toggle_engine)

def _with_gesture_aliases(mapping):
    """The engine names the thumb poses thumb_up / thumb_down, but the built-in fallback table (and older config
    files) say thumbs_up / thumbs_down, so maximize / minimize silently never fired. Accept both spellings."""
    for alias, key in (("thumbs_up", "thumb_up"), ("thumbs_down", "thumb_down")):
        if alias in mapping and key not in mapping:
            mapping[key] = mapping[alias]
    return mapping


def load_gesture_mappings():
    config_path = os.path.join(BASE_DIR, 'config', 'gestures.json')
    try:
        with open(config_path, 'r') as f:
            data = json.load(f)
            return _with_gesture_aliases({g['name']: g['action'] for g in data['gestures']})
    except Exception as e:
        print(f"Config error: {e}. Falling back to default mappings.")
        return _with_gesture_aliases({
            "fist": "media_play_pause",
            "index_point": "wake_gate", 
            "victory_sign": "trigger_screenshot",
            "swipe_up": "volume_up",
            "swipe_down": "volume_down",
            "swipe_left": "previous_track",
            "swipe_right": "next_track",
            "thumbs_up": "maximize",
            "thumbs_down": "minimize",
            "pinch_zoom_in": "zoom_in",
            "l_zoom_out": "zoom_out"
        })

def hex_to_bgr(hex_str):
    """Converts a #RRGGBB string to an OpenCV friendly (B, G, R) tuple."""
    hex_str = hex_str.lstrip('#')
    if len(hex_str) != 6:
        return (255, 255, 255)
    r, g, b = tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))
    return (b, g, r)

DEFAULT_FINGER_COLORS = {"thumb": "#00FF00", "index": "#0000FF", "middle": "#FF00FF", "ring": "#00FFFF", "pinky": "#FFFFFF"}
_FINGER_TIPS = {"thumb": 4, "index": 8, "middle": 12, "ring": 16, "pinky": 20}


def finger_colors_from_config(config_data):
    """{landmark id of each fingertip: BGR colour} from the 'colors' section of gestures.json."""
    c_map = (config_data or {}).get("colors", {}) or {}
    return {tip: hex_to_bgr(str(c_map.get(name, DEFAULT_FINGER_COLORS[name])))
            for name, tip in _FINGER_TIPS.items()}


def safety_delays_from_config(config_data):
    """(grid macro cooldown, custom action cooldown) in seconds from gestures.json."""
    safety = (config_data or {}).get("safety_cooldown_controls", {}) or {}
    try:
        grid_delay = float(safety.get("swipe_action_delay", 0.50))
    except (TypeError, ValueError):
        grid_delay = 0.50
    try:
        custom_delay = float(safety.get("custom_action_delay", 3.0))
    except (TypeError, ValueError):
        custom_delay = 3.0
    return grid_delay, custom_delay


def normalize_pose(points):
    """Turns 21 landmarks (objects with .x/.y or [x, y, z] lists) into a wrist-relative, scale-normalized (21, 2) array."""
    pts = []
    for p in points:
        if hasattr(p, 'x'):
            pts.append((p.x, p.y))
        else:
            pts.append((p[0], p[1]))
    arr = np.array(pts, dtype=np.float32)
    scale = float(np.linalg.norm(arr[9] - arr[0]))
    if scale < 1e-4:
        return None
    return (arr - arr[0]) / scale

def load_custom_poses():
    """Loads user-captured poses (gestures that carry saved landmarks) plus the match threshold."""
    config_path = os.path.join(BASE_DIR, 'config', 'gestures.json')
    poses = []
    threshold = 0.25
    try:
        with open(config_path, 'r') as f:
            data = json.load(f)
        threshold = float(data.get('pose_match_threshold', 0.25))
        for g in data.get('gestures', []):
            lm = g.get('landmarks')
            if lm and len(lm) == 21:
                ref = normalize_pose(lm)
                if ref is not None:
                    poses.append((g['name'], ref))
    except Exception as e:
        print(f"Custom pose load error: {e}")
    return poses, threshold

def match_custom_pose(hand_points, poses, threshold):
    """Returns the name of the closest captured pose within the threshold, else None."""
    current = normalize_pose(hand_points)
    if current is None:
        return None
    best_name = None
    best_dist = threshold
    for name, ref in poses:
        dist = float(np.mean(np.linalg.norm(current - ref, axis=1)))
        if dist < best_dist:
            best_dist = dist
            best_name = name
    return best_name

def push_history(hx, hy, ht, x, y, t, window=SWIPE_WINDOW_SECONDS):
    """Appends one hand position and drops samples older than `window` seconds."""
    hx.append(x); hy.append(y); ht.append(t)
    while len(ht) > 1 and t - ht[0] > window:
        hx.pop(0); hy.pop(0); ht.pop(0)

def swipe_window_ready(ht, window=SWIPE_WINDOW_SECONDS):
    """True once the recorded motion spans (most of) the swipe window."""
    return len(ht) >= 3 and (ht[-1] - ht[0]) >= window * 0.7


def run_custom_action(action, current_time, cooldowns, delay):
    """Runs a mapped gesture action at most once per `delay` seconds (so holding a pose doesn't launch an app
    ten times). The dispatcher does the work and logs the success/failure, like every other command."""
    cooldown_key = "custom:" + action
    if current_time < cooldowns.get(cooldown_key, 0):
        return
    cooldowns[cooldown_key] = current_time + delay
    if action.startswith(("launch:", "hotkey:")) and not action.partition(":")[2].strip():
        return      # empty custom action

    if action in VOICE_LOCK_ACTIONS:
        try:
            import voice_module
            voice_module.set_voice_locked(action == "voice_lock")
        except Exception as e:
            print(f"[VISION] Could not change the voice lock: {type(e).__name__}: {e}")
        return

    print(f"[VISION] Gesture action -> {action}")
    # A failing action (missing module, bad path, ...) used to raise out of the tracking loop and stop the whole
    # tracker. It is now reported and the camera keeps running.
    try:
        get_dispatcher().dispatch_system_action(action_name=action)
    except Exception as e:
        import traceback
        print(f"[VISION] Action '{action}' failed: {type(e).__name__}: {e}")
        traceback.print_exc()


_active_keeper = None   # the webcam currently held by the tracker (so it can always be released)


def run_hand_tracker(frame_queue=None, should_run=None):
    """Runs the tracker and ALWAYS frees the webcam afterwards (even after an error),
    so pressing START again can open it. should_run() returning False stops this run."""
    global _active_keeper
    try:
        _run_hand_tracker(frame_queue, should_run or (lambda: True))
    finally:
        keeper, _active_keeper = _active_keeper, None
        if keeper is not None:
            keeper.release()


def _run_hand_tracker(frame_queue, should_run):
    global WAKE_HOLD_START, WAKE_NEEDS_RELEASE, active_intent, last_swipe_timestamp
    global PINCH_HOLD_START, PINCH_NEEDS_RELEASE
    global TRACKER_LOOP_ACTIVE, _active_keeper

    TRACKER_LOOP_ACTIVE = True
    
    mappings = load_gesture_mappings()
    custom_poses, pose_threshold = load_custom_poses()

    config_path = os.path.join(BASE_DIR, 'config', 'gestures.json')
    try:
        with open(config_path, 'r') as f:
            config_data = json.load(f)
    except Exception as e:
        print(f"CONFIG FILE ERROR: {e}")
        config_data = {}
    finger_colors = finger_colors_from_config(config_data)

    task_path = os.path.join(BASE_DIR, 'hand_landmarker.task')
    try:
        with open(task_path, 'rb') as f:
            model_bytes = f.read()
    except Exception as e:
        print(f"CRITICAL MODEL FILE ERROR: {e}")
        sys.exit(1)
        
    base_options = python.BaseOptions(model_asset_buffer=model_bytes)
    options = vision.HandLandmarkerOptions(
        base_options=base_options,
        running_mode=vision.RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=0.85, 
        min_tracking_confidence=0.85 
    )
    
    detector = vision.HandLandmarker.create_from_options(options)
    if not should_run():          # stopped while the model was loading: don't grab the camera
        detector.close()
        return
    # CameraKeeper restarts the webcam by itself when the feed goes black, freezes or turns to static
    # (a faulty camera/driver keeps "succeeding" while sending garbage, so the old code never noticed).
    keeper = CameraKeeper()
    keeper.open()
    _active_keeper = keeper

    consecutive_hand_frames = 0
    FRAME_CONFIRMATION_MIN = 3

    current_swipe_cooldown_limit, custom_action_delay = safety_delays_from_config(config_data)

    cooldowns = {
        "volume_up": 0,
        "volume_down": 0,
        "mode_toggle": 0,
        "media_play_pause": 0,
        "media_track_change": 0,
        "grid_macro": 0,
    }

    history_x = []
    history_y = []
    history_t = []
    swipe_threshold = 0.8    # in hand-lengths (displacement / hand_scale); 0.15 was just jitter

    scroll_ref_y, scroll_ref_t = None, 0.0   # two-finger scroll: last position / time a scroll step was applied
    last_zone_scroll_ts = 0.0                # zone scroll: last time a scroll step was sent

    print("Phase 2 Engine Active. Press Ctrl+Shift+S globally to pause or resume tracking.")

    TARGET_WIDTH = 700
    TARGET_HEIGHT = 500

    last_confirmed_gesture = "system_idle"
    gesture_frame_streak = 0
    STREAK_REQUIRED = 4

    # ---- speed bookkeeping ----
    fps_ema = 0.0
    last_loop_ts = time.time()
    read_ms_ema = detect_ms_ema = 0.0
    last_stats_print = time.time()
    last_config_check = time.time()
    try:
        last_config_mtime = os.path.getmtime(config_path)
    except OSError:
        last_config_mtime = None
    last_brightness_ts = 0.0
    brightness = None
    last_detect_ms = 0

    while TRACKER_LOOP_ACTIVE and should_run():
        current_time = time.time()
        loop_dt = current_time - last_loop_ts
        last_loop_ts = current_time
        if 0 < loop_dt < 2.0:
            # Capped at 60 FPS (no webcam here is faster). The first loop pass takes almost no time, which made the
            # estimate start in the tens of thousands; while it decayed, the hold times were hundreds of frames
            # long, so gestures were ignored for the first ~2 seconds after every START.
            inst_fps = min(1.0 / loop_dt, 60.0)
            fps_ema = inst_fps if fps_ema == 0.0 else (0.9 * fps_ema + 0.1 * inst_fps)
        if fps_ema > 0:   # frame counts follow the real speed, so the time you must hold a gesture stays constant
            STREAK_REQUIRED = max(4, int(round(fps_ema * STREAK_SECONDS)))
            FRAME_CONFIRMATION_MIN = max(3, int(round(fps_ema * HAND_CONFIRM_SECONDS)))

        # Re-read the config only when the file changed (checked twice a second), not on every frame.
        if current_time - last_config_check > 0.5:
            last_config_check = current_time
            try:
                cfg_mtime = os.path.getmtime(config_path)
            except OSError:
                cfg_mtime = None
            if cfg_mtime != last_config_mtime:
                last_config_mtime = cfg_mtime
                # Settings were saved: apply everything now (gesture mappings and custom poses used to need a
                # restart of the engine; only the colours and the grid were reloaded).
                try:
                    with open(config_path, 'r') as f:
                        config_data = json.load(f)
                    mappings = load_gesture_mappings()
                    custom_poses, pose_threshold = load_custom_poses()
                    current_swipe_cooldown_limit, custom_action_delay = safety_delays_from_config(config_data)
                    print("[VISION] Gesture settings reloaded.")
                except Exception as e:
                    print(f"[VISION] Could not reload gesture settings, keeping the previous ones: {e}")
                finger_colors = finger_colors_from_config(config_data)

        if not ENGINE_ACTIVE:
            fps_ema = 0.0
            _hud_track(paused=True)
            time.sleep(0.2) 
            continue

        _t0 = time.time()
        success, frame = keeper.read()      # restarts the camera itself if it dies or sends garbage
        _t1 = time.time()
        if not success:
            continue
        capture_ts = time.time()   # latency in the HUD is measured from here

        frame = cv2.resize(cv2.flip(frame, 1), (TARGET_WIDTH, TARGET_HEIGHT))
        h, w, _ = frame.shape 

        # average scene brightness (0-255), sampled twice a second on a tiny copy so it costs almost nothing
        if capture_ts - last_brightness_ts > 0.5:
            last_brightness_ts = capture_ts
            brightness = float(cv2.mean(cv2.cvtColor(cv2.resize(frame, (64, 48)), cv2.COLOR_BGR2GRAY))[0])
        
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
        
        # VIDEO mode needs strictly increasing timestamps (two frames in the same millisecond would raise)
        frame_timestamp_ms = max(int(current_time * 1000), last_detect_ms + 1)
        last_detect_ms = frame_timestamp_ms
        _t2 = time.time()
        detection_result = detector.detect_for_video(mp_image, frame_timestamp_ms)
        _t3 = time.time()

        read_ms = (_t1 - _t0) * 1000.0
        detect_ms = (_t3 - _t2) * 1000.0
        read_ms_ema = read_ms if read_ms_ema == 0.0 else 0.9 * read_ms_ema + 0.1 * read_ms
        detect_ms_ema = detect_ms if detect_ms_ema == 0.0 else 0.9 * detect_ms_ema + 0.1 * detect_ms
        if current_time - last_stats_print >= STATS_PRINT_EVERY:
            last_stats_print = current_time
            print(f"[VISION] {fps_ema:.1f} FPS | camera read {read_ms_ema:.0f} ms | hand detect {detect_ms_ema:.0f} ms")


        gesture_key = "open_hand"
        gesture_text = "OPEN HAND"
        color = (0, 255, 0)

        hand_detected = bool(detection_result.hand_landmarks)
        hand_verified = False
        hand_scale = 1.0

        if hand_detected:
            hand_points = detection_result.hand_landmarks[0]

            wrist = hand_points[0]
            middle_mcp = hand_points[9]

            hand_scale = ((wrist.x - middle_mcp.x)**2 + (wrist.y - middle_mcp.y)**2)**0.5

            if hand_scale > 0.04:
                consecutive_hand_frames += 1
            else:
                consecutive_hand_frames = 0

            if consecutive_hand_frames >= FRAME_CONFIRMATION_MIN:
                hand_verified = True
        else: 
            consecutive_hand_frames = 0

        if hand_verified:
            hand_points = detection_result.hand_landmarks[0]
            if hand_scale == 0: hand_scale = 1.0

            wrist_landmark = hand_points[0]
            w_x = max(0.0, min(0.99, wrist_landmark.x))
            w_y = max(0.0, min(0.99, wrist_landmark.y))
            
            GRID_DENSITY = 3
            current_col = int(w_x * GRID_DENSITY)
            current_row = int(w_y * GRID_DENSITY)
            cell_coords_key = f"{current_row},{current_col}"

            for lm in hand_points:
                cx, cy = int(lm.x * w), int(lm.y * h)
                cv2.circle(frame, (cx, cy), 5, (0, 255, 255), cv2.FILLED)

            for tip_id, ring_color in finger_colors.items():
                tip_lm = hand_points[tip_id]
                cv2.circle(frame, (int(tip_lm.x * w), int(tip_lm.y * h)), 12, ring_color, 2)

            thumb_tip = hand_points[4]
            index_mcp = hand_points[5]
            thumb_to_index = ((thumb_tip.x - index_mcp.x)**2 + (thumb_tip.y - index_mcp.y)**2)**0.5
            normalized_thumb_gap = thumb_to_index / hand_scale

            pinky_mcp = hand_points[17]

            thumb_to_pinky_base = ((thumb_tip.x - pinky_mcp.x)**2 + (thumb_tip.y - pinky_mcp.y)**2)**0.5

            normalized_thumb_stretch = thumb_to_pinky_base / hand_scale
            
            is_fist_allowed = normalized_thumb_gap <= 0.28

            is_thumb_open = normalized_thumb_stretch > 0.65 

            is_thumb_pointing_down = thumb_tip.y > wrist.y

            tips = [8, 12, 16, 20]      # Index, Middle, Ring, Pinky tips
            bases = [5, 9, 13, 17]      # Corresponding MCP knuckles
            
            fingers_open = []
            for tip, mcp in zip(tips, bases):
                tip_to_wrist = ((hand_points[tip].x - wrist.x)**2 + (hand_points[tip].y - wrist.y)**2)**0.5
                mcp_to_wrist = ((hand_points[mcp].x - wrist.x)**2 + (hand_points[mcp].y - wrist.y)**2)**0.5

                if tip_to_wrist > mcp_to_wrist + (0.15 * hand_scale):
                    fingers_open.append(True)
                else:
                    fingers_open.append(False)

            is_index_open  = fingers_open[0]
            is_middle_open = fingers_open[1]
            is_ring_open   = fingers_open[2]
            is_pinky_open  = fingers_open[3]

            all_fingers_closed = not any(fingers_open)
            three_fingers_closed = not is_middle_open and not is_ring_open and not is_pinky_open

            index_tip  = hand_points[8]
            middle_tip = hand_points[12]

            thumb_to_index  = ((thumb_tip.x - index_tip.x)**2  + (thumb_tip.y - index_tip.y)**2)**0.5
            thumb_to_middle = ((thumb_tip.x - middle_tip.x)**2 + (thumb_tip.y - middle_tip.y)**2)**0.5
            
            gap_index  = thumb_to_index / hand_scale
            gap_middle = thumb_to_middle / hand_scale

            two_fingers_closed = not is_ring_open and not is_pinky_open
            index_middle_gap = (((index_tip.x - middle_tip.x)**2 + (index_tip.y - middle_tip.y)**2)**0.5) / hand_scale
            # Index + middle held together = scroll; spread apart (> 0.40) = victory sign. Without the spread check
            # the scroll branch caught every peace sign while unlocked, so the victory gesture never fired.
            is_scroll_posture  = (is_index_open and is_middle_open and two_fingers_closed and (gap_index >= 0.25)
                                  and index_middle_gap <= 0.40)

            three_fingers_closed = not is_middle_open and not is_ring_open and not is_pinky_open
            is_pinch_posture     = (gap_index < 0.25) and is_index_open and three_fingers_closed

            is_l_posture = is_thumb_open and is_index_open and three_fingers_closed and (gap_index >= 0.25)

            is_three_finger_pinch = ((gap_index < 0.28) and (gap_middle < 0.28) and is_index_open and is_middle_open
                                     and two_fingers_closed and (index_middle_gap <= PINCH_MAX_TIP_GAP))

            hand_x = hand_points[0].x
            hand_y = hand_points[0].y
            
            push_history(history_x, history_y, history_t, hand_x, hand_y, current_time)

            index_tip = hand_points[8]
            middle_tip = hand_points[12]

            custom_match_name = match_custom_pose(hand_points, custom_poses, pose_threshold) if custom_poses else None

            if custom_match_name:
                gesture_key = custom_match_name
                gesture_text = f"CUSTOM POSE: {custom_match_name.upper()}"
                color = (0, 200, 255)

            elif is_pinch_posture:
                gesture_key = "pinch_zoom_in"
                gesture_text = "PINCH IN: ZOOM IN"
                color = (215, 0, 215)

            elif is_l_posture:
                gesture_key = "l_zoom_out"
                gesture_text = "PINCH OUT: ZOOM OUT"
                color = (0, 215, 215)

            elif is_three_finger_pinch:
                gesture_key = "three_finger_pinch"
                gesture_text = "3-FINGER PINCH: TOGGLE WRITING"
                color = (255, 105, 180)

            elif is_scroll_posture and system_state.SYSTEM_UNLOCKED:
                gesture_key = "two_finger_scroll"
                color = (0, 255, 255)

                wrist_point = hand_points[0]
                current_x_pos = wrist_point.x 

                SCREEN_CENTER = 0.50
                NEUTRAL_DEADZONE = 0.08

                center_offset = current_x_pos - SCREEN_CENTER

                if center_offset < -NEUTRAL_DEADZONE:
                    push_intensity = abs(center_offset) - NEUTRAL_DEADZONE
                    scroll_speed = int(push_intensity * 600) + 2
                    
                    if current_time - last_zone_scroll_ts >= SCROLL_REF_DT:
                        _safe_gui(pyautogui.scroll, -scroll_speed)
                        last_zone_scroll_ts = current_time
                    gesture_text = f"LEFT DISPLAY ZONE: SCROLLING DOWN ▼ (SPEED: {scroll_speed})"

                elif center_offset > NEUTRAL_DEADZONE:
                    push_intensity = center_offset - NEUTRAL_DEADZONE
                    scroll_speed = int(push_intensity * 600) + 2
                    
                    if current_time - last_zone_scroll_ts >= SCROLL_REF_DT:
                        _safe_gui(pyautogui.scroll, scroll_speed)
                        last_zone_scroll_ts = current_time
                    gesture_text = f"RIGHT DISPLAY ZONE: SCROLLING UP ▲ (SPEED: {scroll_speed})"

                else:
                    gesture_text = "ZONE SCROLL ENGINE: NEUTRAL CENTER"

            elif not any(fingers_open) and is_fist_allowed:
                gesture_key = "fist"
                gesture_text = "FIST: PLAY/PAUSE"
                color = (0, 0, 255)
                
            elif is_index_open and not is_middle_open and not is_ring_open and not is_pinky_open:
                gesture_key = "index_point"
                gesture_text = "INDEX: POINTING"
                color = (255, 0, 0)
                
            elif is_index_open and is_middle_open and not is_ring_open and not is_pinky_open:
                index_tip = hand_points[8]
                middle_tip = hand_points[12]
                finger_gap = ((index_tip.x - middle_tip.x)**2 + (index_tip.y - middle_tip.y)**2)**0.5
                normalized_gap = finger_gap / hand_scale

                if normalized_gap > 0.40:  
                    gesture_key = "victory_sign"
                    gesture_text = "VICTORY SIGN"
                    color = (255, 0, 255)
                else:
                    gesture_key = "two_finger_scroll"
                    gesture_text = "SCROLL MODE ACTIVE"
                    color = (0, 255, 255)

                    if scroll_ref_y is None or (current_time - scroll_ref_t) > 0.5:
                        scroll_ref_y, scroll_ref_t = history_y[-1], current_time   # (re)start measuring
                    elif (current_time - scroll_ref_t) >= SCROLL_REF_DT:
                        y_delta = (history_y[-1] - scroll_ref_y) / hand_scale
                        scroll_ref_y, scroll_ref_t = history_y[-1], current_time
                        if abs(y_delta) > 0.012 and system_state.SYSTEM_UNLOCKED:
                            _safe_gui(pyautogui.scroll, int(-y_delta * 450))

            elif is_thumb_open and not is_thumb_pointing_down and all_fingers_closed:
                gesture_key = "thumb_up"
                gesture_text = "THUMB UP: MAXIMIZE"
                color = (0, 255, 255)

            elif is_thumb_open and is_thumb_pointing_down and all_fingers_closed:
                gesture_key = "thumb_down"
                gesture_text = "THUMB DOWN: MINIMIZE"
                color = (255, 0, 255)

            elif all(fingers_open) and swipe_window_ready(history_t):
                start_y = history_y[0]
                end_y = history_y[-1]
                y_displacement = (end_y - start_y) / hand_scale

                start_x = history_x[0]
                end_x = history_x[-1]
                x_displacement = (end_x - start_x) / hand_scale

                RECOVERY_DURATION = 1.6

                if active_intent and (current_time - last_swipe_timestamp < RECOVERY_DURATION):
                    time_passed = current_time - last_swipe_timestamp
                    progress_pct = time_passed / RECOVERY_DURATION
                    remaining_ms = int((RECOVERY_DURATION - time_passed) * 1000)
                    
                    if "UP" in active_intent:
                        gesture_text = f"PHYSICS LOCKOUT: DROPDOWN IGNORED ({remaining_ms}ms)"
                    else:
                        gesture_text = f"PHYSICS LOCKOUT: REBOUND IGNORED ({remaining_ms}ms)"
                    
                    color = (0, 165, 255) 
                    is_swiping_up = is_swiping_down = is_swiping_left = is_swiping_right = False

                    mid_w, mid_h = w // 2, h // 2
                    line_w = int(100 * progress_pct)
                    cv2.line(frame, (mid_w - 50, mid_h), (mid_w - 50 + line_w, mid_h), (0, 165, 255), 4)
                else:
                    if active_intent and (current_time - last_swipe_timestamp > INTENT_RESET_DELAY):
                        active_intent = None
                    
                    is_swiping_up = is_swiping_down = is_swiping_left = is_swiping_right = False
                    if abs(x_displacement) >= abs(y_displacement):
                        is_swiping_left = x_displacement < -swipe_threshold
                        is_swiping_right = x_displacement > swipe_threshold
                    else:
                        is_swiping_up = y_displacement < -swipe_threshold
                        is_swiping_down = y_displacement > swipe_threshold

                if is_swiping_up:
                    if start_y < 0.45:
                        gesture_text = "BLOCKED: UP SWIPE MUST START LOWER"
                        color = (0, 0, 255)
                        is_swiping_up = False
                    else:
                        gesture_key = "swipe_up"
                        gesture_text = "SWIPE UP: VOLUME +"
                        color = (0, 255, 255)

                elif is_swiping_down:
                    if start_y > 0.55:
                        gesture_text = "PHYSICS DROPDOWN IGNORED (STARTED LOW)"
                        color = (0, 165, 255) 
                        is_swiping_down = False
                    else:
                        gesture_key = "swipe_down"
                        gesture_text = "SWIPE DOWN: VOL -"
                        color = (0, 165, 255)

                elif is_swiping_left:
                    if current_time < cooldowns["media_track_change"]:
                        remaining = round(cooldowns["media_track_change"] - current_time, 1)
                        gesture_text = f"SWIPE LEFT BLOCKED: COOLDOWN ({remaining}s)"
                        color = (0, 0, 255)
                    else:
                        gesture_key = "swipe_left"
                        gesture_text = "SWIPE LEFT: PREV TRACK"
                        color = (255, 165, 0)
                elif is_swiping_right:
                    if current_time < cooldowns["media_track_change"]:
                        remaining = round(cooldowns["media_track_change"] - current_time, 1)
                        gesture_text = f"SWIPE RIGHT BLOCKED: COOLDOWN ({remaining}s)"
                        color = (0, 0, 255)
                    else:
                        gesture_key = "swipe_right"
                        gesture_text = "SWIPE RIGHT: NEXT TRACK"
                        color = (0, 255, 255)

        # Grid zone macros: checked once the gesture is known. (They used to be checked before classification,
        # while the gesture was still the "open_hand" default, so bindings for any other pose never fired. They
        # also skipped the lock and the history log; they now go through the dispatcher like everything else.)
        if hand_verified and system_state.SYSTEM_UNLOCKED and current_time > cooldowns["grid_macro"]:
            binding = config_data.get("user_2d_grid_matrix", {}).get("zone_bindings", {}).get(cell_coords_key)
            if binding and binding.get("gesture_trigger") == gesture_key:
                keys = [str(k).strip().lower() for k in binding.get("execution_payload", []) if str(k).strip()]
                if keys:
                    print(f"[GRID ACTION MASTER] Zone [{cell_coords_key}] -> {'+'.join(keys)}")
                    get_dispatcher().dispatch_system_action(action_name="hotkey:" + "+".join(keys))
                    cooldowns["grid_macro"] = current_time + current_swipe_cooldown_limit

        action = mappings.get(gesture_key, "system_idle")

        if gesture_key == "index_point":
            if WAKE_NEEDS_RELEASE:
                pass    # just toggled: wait until the finger is lowered before counting a new hold
            elif WAKE_HOLD_START is None:
                WAKE_HOLD_START = current_time
            elif current_time - WAKE_HOLD_START >= WAKE_HOLD_DURATION:
                get_dispatcher().dispatch_system_action("wake_gate")
                # Before, the hold timer simply restarted 1.5 s later, so keeping the finger up flipped the lock
                # again ~1.9 s after unlocking. Now one hold = one toggle.
                WAKE_HOLD_START = None
                WAKE_NEEDS_RELEASE = True

            if system_state.SYSTEM_UNLOCKED:
                gesture_text = "READY: INDEX_POINT"
                color = (0, 255, 0)
            else:
                gesture_text = "HOLDING INDEX..."
                color = (0, 165, 255)
        else:
            WAKE_HOLD_START = None
            WAKE_NEEDS_RELEASE = False

        if gesture_key == "three_finger_pinch":
            if PINCH_NEEDS_RELEASE:
                pass    # just toggled: release the pinch before a new hold can count
            elif PINCH_HOLD_START is None:
                PINCH_HOLD_START = current_time
            elif (current_time - PINCH_HOLD_START >= PINCH_HOLD_DURATION
                  and current_time > cooldowns["mode_toggle"]):
                system_state.WRITING_MODE_ACTIVE = not system_state.WRITING_MODE_ACTIVE
                system_state.HUD_REFRESH_REQUIRED = True

                mode_text = "WRITING MODE" if system_state.WRITING_MODE_ACTIVE else "COMMAND MODE"
                print(f"[GESTURE INTERACT] Three-Finger Pinch held! Swapping context to: {mode_text}")

                cooldowns["mode_toggle"] = current_time + COOLDOWN_DEFAULT
                PINCH_HOLD_START = None
                PINCH_NEEDS_RELEASE = True
        else:
            PINCH_HOLD_START = None
            PINCH_NEEDS_RELEASE = False

        if system_state.SYSTEM_UNLOCKED:
            if gesture_key != "index_point" and not gesture_text.startswith("SWIPE"):
                gesture_text = f"READY: {gesture_key.upper()}"

            if action == last_confirmed_gesture:
                gesture_frame_streak += 1
            else:
                last_confirmed_gesture = action
                gesture_frame_streak = 0

            if gesture_frame_streak >= STREAK_REQUIRED and action != "system_idle":
                # Every mapped action now runs once the pose is held. Before, only maximize / minimize / zoom and
                # custom launch:/hotkey: actions were executed; anything else picked in Settings (copy, calculator,
                # screenshot, ...) silently did nothing.
                if (action in IMMEDIATE_ACTIONS or action == "wake_gate"
                        or gesture_key in SPECIAL_GESTURES):
                    pass    # handled by their own code paths
                elif action in REPEATING_ACTIONS:
                    get_dispatcher().dispatch_system_action(action_name=action)
                else:
                    run_custom_action(action, current_time, cooldowns, custom_action_delay)

                gesture_frame_streak = 0

        swipe_action = None
        if system_state.SYSTEM_UNLOCKED and action in ("volume_up", "volume_down") and current_time > cooldowns[action]:
            swipe_action = action
            cooldowns[action] = current_time + SWIPE_COOLDOWN
            last_swipe_timestamp = current_time
            active_intent = action.upper()
        elif (system_state.SYSTEM_UNLOCKED and action in ("previous_track", "next_track")
              and current_time > cooldowns["media_track_change"]):
            swipe_action = action
            cooldowns["media_track_change"] = current_time + TRACK_CHANGE_COOLDOWN
        elif action == "media_play_pause" and current_time > cooldowns["media_play_pause"]:
            # Play/pause also works while locked. It goes through the dispatcher in both cases now, so it is
            # logged and the HUD shows it as done (it used to show "BLOCKED" even though the media paused).
            cooldowns["media_play_pause"] = current_time + COOLDOWN_DEFAULT
            get_dispatcher().dispatch_system_action(action_name=action, bypass_lock=True)
        elif not system_state.SYSTEM_UNLOCKED and gesture_key != "index_point" and not hand_verified:
            gesture_text = "SYSTEM LOCKED (MEDIA READY)"
            color = (0, 0, 255)
        elif gesture_key != "index_point" and not hand_verified:
            gesture_text = "SYSTEM LOCKED (INDEX TO WAKE)"
            color = (0, 0, 255)

        if swipe_action:
            print(f"Executing Action: {swipe_action}")
            get_dispatcher().dispatch_system_action(action_name=swipe_action)
            history_x.clear(); history_y.clear(); history_t.clear()

        dim_color = (60, 60, 60)      
        arrow_thickness = 3
        active_thickness = 7
        mid_w, mid_h = w // 2, h // 2
        offset = 40                   

        is_up_active = (gesture_key == "swipe_up") or (gesture_text.startswith("BLOCKED") and "UP" in gesture_text)
        c_up = color if is_up_active else dim_color
        t_up = active_thickness if is_up_active else arrow_thickness
        cv2.arrowedLine(frame, (mid_w, mid_h - offset), (mid_w, mid_h - offset - 50), c_up, t_up, tipLength=0.4)

        is_down_active = (gesture_key == "swipe_down") or (gesture_text.startswith("BLOCKED") and "DOWN" in gesture_text)
        c_down = color if is_down_active else dim_color
        t_down = active_thickness if is_down_active else arrow_thickness
        cv2.arrowedLine(frame, (mid_w, mid_h + offset), (mid_w, mid_h + offset + 50), c_down, t_down, tipLength=0.4)

        is_left_active = (gesture_key == "swipe_left") or ("LEFT" in gesture_text and "BLOCKED" in gesture_text)
        c_left = color if is_left_active else dim_color
        t_left = active_thickness if is_left_active else arrow_thickness
        cv2.arrowedLine(frame, (mid_w - offset, mid_h), (mid_w - offset - 50, mid_h), c_left, t_left, tipLength=0.4)

        is_right_active = (gesture_key == "swipe_right") or ("RIGHT" in gesture_text and "BLOCKED" in gesture_text)
        c_right = color if is_right_active else dim_color
        t_right = active_thickness if is_right_active else arrow_thickness
        cv2.arrowedLine(frame, (mid_w + offset, mid_h), (mid_w + offset + 50, mid_h), c_right, t_right, tipLength=0.4)

        cv2.putText(frame, gesture_text, (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

        if not hand_verified:
            history_x.clear()
            history_y.clear(); history_t.clear()
            WAKE_HOLD_START = None 
            cv2.putText(frame, "Searching Hand...", (30, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        # ---- publish this frame's tracking data for the camera HUD ----
        wrist_x = wrist_y = grid_cell = None
        if hand_verified:
            try:
                wrist_x, wrist_y = float(hand_points[0].x), float(hand_points[0].y)
                grid_cell = (int(max(0.0, min(0.99, wrist_y)) * 3), int(max(0.0, min(0.99, wrist_x)) * 3))
            except Exception:
                pass
        wake_progress = 0.0
        if gesture_key == "index_point" and WAKE_HOLD_START is not None and WAKE_HOLD_DURATION > 0:
            wake_progress = max(0.0, min(1.0, (current_time - WAKE_HOLD_START) / WAKE_HOLD_DURATION))
        _hud_track(
            paused=False,
            frame_ts=capture_ts,
            fps=fps_ema,
            hand=bool(hand_verified),
            gesture=gesture_key,
            action=action,
            streak=gesture_frame_streak,
            streak_needed=STREAK_REQUIRED,
            wrist=(wrist_x, wrist_y),
            hand_scale=float(hand_scale) if hand_verified else None,
            grid_cell=grid_cell,
            wake_progress=wake_progress,
            brightness=brightness,
            unlocked=bool(system_state.SYSTEM_UNLOCKED),
            writing_mode=bool(getattr(system_state, "WRITING_MODE_ACTIVE", False)),
            cooldowns={
                "track": cooldowns["media_track_change"],
                "play": cooldowns["media_play_pause"],
                "mode": cooldowns["mode_toggle"],
            },
        )

        if frame_queue is not None:
            # newest frame wins: drop the oldest one if the preview has not picked it up yet
            if frame_queue.full():
                try:
                    frame_queue.get_nowait()
                except queue.Empty:
                    pass
            try:
                frame_queue.put_nowait(frame)
            except queue.Full:
                pass

    print("[SYSTEM] Safely breaking tracking loop...")
    keeper.release()
    try:
        detector.close()
    except Exception:
        pass
    print("[SYSTEM] Webcam successfully disconnected.")

if __name__ == "__main__":
    run_hand_tracker()
