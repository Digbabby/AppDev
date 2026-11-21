import os
import sys
import json
import time
import queue
import threading
import re
import sounddevice as sd
import numpy as np
import whisper
import system_state
import keyboard
import pygetwindow as gw
from camera_preview import add_subtitle_to_overlay, set_overlay_preview

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(SCRIPT_DIR, 'config')):
    BASE_DIR = SCRIPT_DIR
else:
    BASE_DIR = os.path.dirname(SCRIPT_DIR)

VOICE_LOOP_ACTIVE = False
VOICE_LOCKED = False          # True = the voice engine ignores everything except the unlock phrase
_lock_gui_ref = None          # dashboard handle used by the hotkey (it has no gui_ref of its own)
sample_rate = 16000
audio_queue = queue.Queue()
voice_lock = threading.Lock()

WHISPER_MODEL_NAME = "base"
_whisper_model = None
_whisper_model_lock = threading.Lock()


def get_whisper_model():
    """Loads the Whisper model once and reuses it. Loading took several seconds on every START before."""
    global _whisper_model
    with _whisper_model_lock:
        if _whisper_model is None:
            _whisper_model = whisper.load_model(WHISPER_MODEL_NAME)
        return _whisper_model


def preload_model():
    """Called in the background when the app opens, so the first START is fast."""
    try:
        get_whisper_model()
        print("[WHISPER] Model preloaded.")
    except Exception as e:
        print(f"[WHISPER] Preload failed (it will be retried on START): {e}")

# ---------------------------------------------------------------------------
# VOICE LOCK
# While locked the microphone is still open (it has to hear the unlock phrase), but nothing that is said is
# logged, shown as a subtitle or run, until you unlock again. Say "lock voice" / "unlock voice", press the
# dashboard button, or press Ctrl+Alt+M. The phrases can be changed in voice_commands.json with the keys
# "voice_lock_phrases" and "voice_unlock_phrases" (lists of strings).
# ---------------------------------------------------------------------------
DEFAULT_LOCK_PHRASES = ("lock voice", "voice lock", "lock voice commands", "stop listening")
DEFAULT_UNLOCK_PHRASES = ("unlock voice", "voice unlock", "unlock voice commands", "start listening", "resume listening")


def _clean_phrase(text):
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", str(text).lower()).split())


def _phrase_list(config_payload, key, defaults):
    custom = config_payload.get(key)
    items = custom if isinstance(custom, list) and custom else defaults
    return [c for c in (_clean_phrase(p) for p in items) if c]


def _is_exact_command(text, phrases):
    """True when the WHOLE utterance is the phrase (optionally with 'please' / 'now'), so that dictating a
    sentence that happens to contain 'lock voice' does not lock anything."""
    cleaned = _clean_phrase(text)
    cleaned = re.sub(r"^please ", "", cleaned)
    cleaned = re.sub(r" (please|now)$", "", cleaned)
    return cleaned in phrases


def _contains_command(text, phrases):
    """True when the phrase appears as whole words anywhere in the utterance (used for unlocking)."""
    cleaned = f" {_clean_phrase(text)} "
    return any(f" {p} " in cleaned for p in phrases)


def _lock_phrases(config_payload):
    """Built-in / configured lock phrases plus any phrase mapped to the 'voice_lock' action in the Voice Commands tab."""
    base = _phrase_list(config_payload, "voice_lock_phrases", DEFAULT_LOCK_PHRASES)
    extra = [_clean_phrase(p) for p in (config_payload.get("voice_commands", {}) or {}).get("voice_lock", [])]
    return base + [e for e in extra if e and e not in base]


def _unlock_phrases(config_payload):
    """Same for unlocking ('voice_unlock' action). These are the only phrases heard while locked."""
    base = _phrase_list(config_payload, "voice_unlock_phrases", DEFAULT_UNLOCK_PHRASES)
    extra = [_clean_phrase(p) for p in (config_payload.get("voice_commands", {}) or {}).get("voice_unlock", [])]
    return base + [e for e in extra if e and e not in base]


def set_voice_locked(locked, gui_ref=None):
    """Locks / unlocks voice commands. Safe to call from any thread."""
    global VOICE_LOCKED
    locked = bool(locked)
    if VOICE_LOCKED == locked:
        return
    VOICE_LOCKED = locked
    ui = gui_ref or _lock_gui_ref
    message = ("[VOICE LOCK] Voice commands LOCKED. Say \"unlock voice\" (or press Ctrl+Alt+M) to resume."
               if locked else "[VOICE LOCK] Voice commands UNLOCKED. Listening for commands.")
    try:
        if ui is not None:
            ui.log_message(message)
        else:
            print(message)
    except Exception:
        print(message)
    refresh = getattr(ui, "refresh_voice_lock_ui", None) if ui is not None else None
    if refresh is not None:
        try:
            run_on_ui = getattr(ui, "run_on_ui", None)
            run_on_ui(refresh) if run_on_ui is not None else refresh()
        except Exception:
            pass


def _toggle_voice_lock_hotkey():
    if VOICE_LOOP_ACTIVE:
        set_voice_locked(not VOICE_LOCKED, _lock_gui_ref)


try:
    keyboard.add_hotkey('ctrl+alt+m', _toggle_voice_lock_hotkey)
except Exception as _hk_err:
    print(f"[VOICE LOCK] Hotkey Ctrl+Alt+M not available: {_hk_err}")


def _dispatcher():
    """Imported on first use (action_dispatcher imports this package's modules, so a top-level import would loop)."""
    import action_dispatcher
    return action_dispatcher


def load_voice_configurations():
    config_path = os.path.join(BASE_DIR, 'config', 'voice_commands.json')
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"[VOICE CONFIG ERROR] {e}. Falling back to default system structures.")
        return {"voice_commands": {}, "custom_apps": {}, "window_contexts": {}}

def _active_window_title():
    try:
        active_window = gw.getActiveWindow()
        return (active_window.title or "").lower() if active_window else ""
    except Exception:
        return ""


def _foreground_exe_name():
    """File name of the program that owns the focused window, e.g. 'winword.exe' ('' when unknown)."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def _app_identity(target):
    """(exe file name or None, short name) used to recognise an app/site from the launcher registry."""
    t = str(target).strip().strip('"')
    if re.match(r"^(https?://|www\.)", t, re.IGNORECASE):
        host = re.sub(r"^(https?://)?(www\.)?", "", t, flags=re.IGNORECASE).split("/")[0]
        return None, host.split(".")[0].lower()          # youtube.com -> "youtube" (found in the tab title)
    base = os.path.basename(t.replace("\\", "/")).lower()
    exe = base if base.endswith(".exe") else None
    return exe, os.path.splitext(base)[0]


def get_active_window_context(window_contexts, title_lower=None):
    """Name of the window_contexts entry whose identifiers appear in the focused window's title, else None."""
    title_lower = _active_window_title() if title_lower is None else title_lower
    if not title_lower:
        return None
    for context_key, config in window_contexts.items():
        for identifier in config.get("window_identifiers", []):
            ident = str(identifier).lower().strip()
            if ident and ident in title_lower:
                return context_key
    return None


def find_window_shortcut(phrase_lower, config_payload):
    """Level 1 lookup: (hotkey, context name) for a phrase in the focused app, else None.

    Checks the hand-written "window_contexts" first, then the per-app shortcuts made with the Settings
    "Edit Configuration" editor (dynamic_apps_registry[...]["shortcuts"]). Those used to be saved but never
    read by the voice engine, so they did nothing.
    """
    phrase_lower = phrase_lower.strip()
    title_lower = _active_window_title()

    window_contexts = config_payload.get("window_contexts", {})
    context_key = get_active_window_context(window_contexts, title_lower)
    if context_key:
        for hotkey_str, trigger_phrases in window_contexts[context_key].get("shortcuts", {}).items():
            if phrase_lower in [str(p).lower().strip() for p in trigger_phrases]:
                return hotkey_str, context_key

    registry = config_payload.get("dynamic_apps_registry", {})
    wanted = [(target, node) for target, node in registry.items()
              if isinstance(node, dict) and any(
                  phrase_lower in [str(p).lower().strip() for p in phrases]
                  for phrases in node.get("shortcuts", {}).values())]
    if not wanted:
        return None
    fg_exe = _foreground_exe_name()
    for target, node in wanted:
        exe, short = _app_identity(target)
        focused = (exe and exe == fg_exe) or (len(short) >= 3 and short in title_lower)
        if not focused:
            continue
        for hotkey_str, trigger_phrases in node.get("shortcuts", {}).items():
            if phrase_lower in [str(p).lower().strip() for p in trigger_phrases]:
                return hotkey_str, short or target
    return None

def audio_callback(indata, frames, time_info, status):
    if status:
        print(status, file=sys.stderr)
    audio_queue.put(indata.copy())

def interpret_voice_intent(phrase, config_payload):
    """Dry run of execute_voice_intent: reports what a phrase WOULD do without doing it.

    Returns (label, kind). kind is one of: mode, format, dictation, hotkey, launch, action, none.
    Keep the matching order in sync with execute_voice_intent.
    """
    phrase_lower = phrase.strip().replace("!", "").replace(".", "").lower()
    if not phrase_lower:
        return ("Nothing heard yet", "none")

    if _is_exact_command(phrase_lower, _lock_phrases(config_payload)):
        return ("Lock voice commands (say \"unlock voice\" to resume)", "mode")

    if "activate writing mode" in phrase_lower or "start typing" in phrase_lower:
        return ("Enter writing mode", "mode")
    if "disable writing mode" in phrase_lower or "stop typing" in phrase_lower:
        return ("Exit writing mode", "mode")

    if system_state.WRITING_MODE_ACTIVE:
        token = phrase.strip().lower().replace(".", "").replace(",", "").replace("!", "")
        macros = ["new paragraph", "new line", "period", "comma", "question mark",
                  "exclamation point", "backspace", "clear line"]
        if token in macros:
            return (f"Format: {token}", "format")
        return (f"Type \"{phrase.strip()}\" into the focused field", "dictation")

    # Level 1: shortcuts for the focused app
    shortcut = find_window_shortcut(phrase_lower, config_payload)
    if shortcut:
        return (f"Hotkey {shortcut[0]} in {shortcut[1]}", "hotkey")

    # Level 2: dynamic launchers
    target = find_launcher(phrase_lower, config_payload)
    if target:
        if _is_url(target):
            return (f"Open website {target}", "launch")
        return (f"Launch {os.path.basename(target) or target}", "launch")

    # Level 3: global commands
    action_key = find_global_action(phrase_lower, config_payload)
    if action_key:
        return (f"Run action: {action_key}", "action")

    return ("No command matched, will be ignored", "none")


def _is_url(target):
    return target.lower().startswith(("http://", "https://", "www."))


def find_launcher(phrase_lower, config_payload):
    """Level 2 lookup: the app path / URL whose launch phrase was said, else None."""
    apps_dictionary = config_payload.get("dynamic_apps_registry", config_payload.get("custom_apps", {}))
    for target_path, config_node in apps_dictionary.items():
        if isinstance(config_node, dict):
            phrases_list = config_node.get("launch_phrases", [])
        elif isinstance(config_node, list):
            phrases_list = config_node
        else:
            phrases_list = [str(config_node)]
        triggers = [str(p).lower().strip() for p in phrases_list if p]
        if phrase_lower in triggers or any(p in phrase_lower for p in triggers if p):
            return target_path.strip()
    return None


def find_global_action(phrase_lower, config_payload):
    """Level 3 lookup: the action whose phrase matches exactly, else None."""
    for action_key, phrases_list in config_payload.get("voice_commands", {}).items():
        if any(str(p).lower().strip() == phrase_lower for p in phrases_list):
            return action_key
    return None

_HALLUCINATION_PHRASES = {"thank you", "thanks for watching", "you", "bye", "thank you for watching"}

def _norm(t):
    return re.sub(r"[^a-z0-9' ]+", " ", t.lower()).split()

def is_hallucination(text, prompt):
    """Catches the classic Whisper failure modes on noise/silence."""
    words = _norm(text)
    if not words:
        return True
    joined = " ".join(words)
    # 1. Echoing the initial_prompt back (even partially, e.g. "...ed system execution ...")
    if prompt and len(words) >= 3 and joined in " ".join(_norm(prompt)):
        return True
    # 2. Stock phrases Whisper invents on silence
    if joined in _HALLUCINATION_PHRASES:
        return True
    # 3. Same sentence looped 3+ times ("It's pretty good. It's pretty good. It's pretty good.")
    sentences = [" ".join(_norm(x)) for x in re.split(r"[.!?]+", text) if _norm(x)]
    if len(sentences) >= 3 and len(set(sentences)) == 1:
        return True
    return False

def _format_pending_preview(text, label, kind, remaining):
    color = "#ffb300" if kind == "none" else "#00e676"
    return (f"“{text}…”\n→ {label}\n⏳ Running in {remaining:.1f}s (keep talking to change it)", color)

def run_voice_loop(gui_ref=None, should_run=None):
    """Listens and runs voice commands until VOICE_LOOP_ACTIVE is cleared or should_run() returns False."""
    global VOICE_LOOP_ACTIVE, _lock_gui_ref
    VOICE_LOOP_ACTIVE = True
    _lock_gui_ref = gui_ref
    set_voice_locked(False, gui_ref)      # every START begins unlocked
    should_run = should_run or (lambda: True)
    config_payload = load_voice_configurations()
    lock_phrases = _lock_phrases(config_payload)
    unlock_phrases = _unlock_phrases(config_payload)

    # Pickup range: how loud speech must be (relative to the room) to count. Louder = closer.
    # Set "mic_range" in voice_commands.json to "near", "normal" or "far".
    # Optional overrides: "min_volume" (absolute floor) and "noise_margin" (x louder than room noise).
    MIC_RANGE_PRESETS = {
        "near":   (0.040, 3.5),   # arm's length or closer, blocks most of the room
        "normal": (0.015, 2.0),   # previous behaviour
        "far":    (0.008, 1.5),   # picks up across the room
    }
    _range_key = str(config_payload.get("mic_range", "normal")).lower()
    VOLUME_THRESHOLD, NOISE_MARGIN = MIC_RANGE_PRESETS.get(_range_key, MIC_RANGE_PRESETS["normal"])
    try:
        VOLUME_THRESHOLD = float(config_payload.get("min_volume", VOLUME_THRESHOLD))
        NOISE_MARGIN = float(config_payload.get("noise_margin", NOISE_MARGIN))
    except (TypeError, ValueError):
        pass
    CHUNK_DURATION = 3.0
    ENDPOINT_SILENCE = 0.8        # silence after speech that closes the current audio slice early
    MIN_CHUNK_SECONDS = 0.4
    try:
        COMMIT_DELAY = max(0.0, float(config_payload.get("commit_delay", 1.2)))
    except (TypeError, ValueError):
        COMMIT_DELAY = 1.2
    background_energy_profile = []   # per-slice noise floor (low-percentile frame RMS)
    FRAME_SECONDS = 0.03
    MIN_VOICED_FRAMES = 8            # ~240 ms of real energy needed before Whisper is even called
    USE_SPECTRAL_SUBTRACTION = False # whole-chunk FFT subtraction creates artifacts Whisper hallucinates on

    def live_threshold():
        if background_energy_profile:
            return max(VOLUME_THRESHOLD, float(np.mean(background_energy_profile)) * NOISE_MARGIN)
        return VOLUME_THRESHOLD

    ambient_noise_magnitude = None

    def push_preview(text, color="#A2A1B6"):
        try:
            set_overlay_preview(text, color)
        except Exception:
            pass
        if gui_ref is not None and hasattr(gui_ref, "set_voice_preview"):
            try:
                gui_ref.set_voice_preview(text, color)
            except Exception:
                pass

    def idle_preview():
        if VOICE_LOCKED:
            push_preview("🔒 Voice locked - say “unlock voice”", "#ffb300")
        else:
            push_preview("Voice: idle", "#6C6B80")

    if gui_ref:
        gui_ref.log_message("[WHISPER] Loading Thread-Locked Context Filtering Engine...")
    model = get_whisper_model()
    if gui_ref:
        gui_ref.log_message("[WHISPER] Audio processing channels active with Adaptive Noise Reduction...")
    push_preview("Voice: idle", "#6C6B80")

    while not audio_queue.empty():
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            break

    pending_text = ""
    pending_label, pending_kind = "", "none"
    last_voice_time = 0.0
    chunk_has_voice = False
    last_preview_push = 0.0
    preview_clear_at = 0.0

    # Say which microphone is used (and fail loudly when there is none) instead of idling silently.
    try:
        mic_name = sd.query_devices(kind="input")["name"]
    except Exception as mic_error:
        raise RuntimeError(f"No microphone available ({mic_error}). Check Windows Settings > Sound > Input "
                           "and Privacy > Microphone.")
    if gui_ref:
        log_async = getattr(gui_ref, "_safe_log_async", gui_ref.log_message)
        log_async(f"[WHISPER] Listening on microphone: {mic_name}")

    if not should_run():          # stopped while the model was loading: don't open the microphone
        push_preview("Voice: idle", "#6C6B80")
        return

    with sd.InputStream(samplerate=sample_rate, channels=1, callback=audio_callback):
        accumulated_blocks = []
        start_tracking_time = time.time()
        last_lock_state = False

        while VOICE_LOOP_ACTIVE and should_run():
            if VOICE_LOCKED != last_lock_state:
                # The lock changed (spoken, button or hotkey): drop anything half-heard so it can't run later.
                last_lock_state = VOICE_LOCKED
                pending_text = ""
                accumulated_blocks = []
                chunk_has_voice = False
                start_tracking_time = time.time()
                if not preview_clear_at:
                    idle_preview()

            try:
                block = audio_queue.get(timeout=0.05)
                accumulated_blocks.append(block)
                if float(np.sqrt(np.mean(np.square(block)))) > live_threshold():
                    last_voice_time = time.time()
                    chunk_has_voice = True
            except queue.Empty:
                pass

            now = time.time()
            silence_for = now - last_voice_time

            if preview_clear_at and now >= preview_clear_at:
                preview_clear_at = 0.0
                if not pending_text:
                    idle_preview()

            # ---- Indicator + commit stage: hold the interpreted phrase until the speaker stops ----
            if pending_text:
                hold = 0.0 if system_state.WRITING_MODE_ACTIVE else COMMIT_DELAY

                if not chunk_has_voice and silence_for >= hold:
                    final_text = pending_text
                    pending_text = ""
                    label, kind = interpret_voice_intent(final_text, config_payload)

                    clean_final = final_text.strip().replace(".", "").replace(",", "").replace("!", "")
                    is_writing_toggle = "writing mode" in clean_final.lower() or "typing" in clean_final.lower()
                    matched_global = False
                    for act, phrases in config_payload.get("voice_commands", {}).items():
                        if any(p.lower().strip() in clean_final.lower() for p in phrases):
                            matched_global = True
                            break
                    is_lock_cmd = _is_exact_command(clean_final, lock_phrases)
                    add_subtitle_to_overlay(final_text, is_command=(is_writing_toggle or matched_global or is_lock_cmd))

                    execute_voice_intent(final_text, config_payload, gui_ref)

                    push_preview(f"✔ {label}", "#00e676" if kind != "none" else "#ffb300")
                    preview_clear_at = time.time() + 2.5
                    continue

                if now - last_preview_push >= 0.1:
                    last_preview_push = now
                    if chunk_has_voice:
                        push_preview(f"“{pending_text}…”\n\U0001F399 More input detected, still listening...", "#ffb300")
                    else:
                        text, color = _format_pending_preview(pending_text, pending_label, pending_kind, max(0.0, hold - silence_for))
                        push_preview(text, color)

            elif chunk_has_voice and not VOICE_LOCKED and now - last_preview_push >= 0.5:
                last_preview_push = now
                push_preview("\U0001F399 Listening...", "#ffb300")

            # ---- Audio slice stage: flush every CHUNK_DURATION, or early once speech has ended ----
            elapsed_chunk_time = now - start_tracking_time
            speech_ended = chunk_has_voice and silence_for >= ENDPOINT_SILENCE
            if elapsed_chunk_time >= CHUNK_DURATION or speech_ended:
                if not accumulated_blocks:
                    start_tracking_time = time.time()
                    continue

                audio_data = np.concatenate(accumulated_blocks, axis=0).ravel().astype(np.float32)
                accumulated_blocks = []
                start_tracking_time = time.time()
                chunk_has_voice = False

                if len(audio_data) < MIN_CHUNK_SECONDS * sample_rate:
                    continue

                # Per-frame energy: lets us (a) learn the noise floor even when it never goes fully
                # silent, and (b) require sustained speech instead of a single loud blip.
                frame_len = int(FRAME_SECONDS * sample_rate)
                n_frames = max(len(audio_data) // frame_len, 1)
                frames = audio_data[:n_frames * frame_len].reshape(n_frames, -1)
                frame_rms = np.sqrt(np.mean(frames ** 2, axis=1))
                gate = live_threshold()
                voiced_frames = int(np.sum(frame_rms > gate))

                background_energy_profile.append(float(np.percentile(frame_rms, 20)))
                if len(background_energy_profile) > 12:
                    background_energy_profile.pop(0)

                # The noise spectrum is only needed for spectral subtraction, which is switched off,
                # so skip the FFT on every audio slice unless it is turned back on.
                if USE_SPECTRAL_SUBTRACTION:
                    fft_data = np.fft.rfft(audio_data)
                    magnitude = np.abs(fft_data)
                    phase = np.angle(fft_data)

                if voiced_frames < MIN_VOICED_FRAMES:
                    if not USE_SPECTRAL_SUBTRACTION:
                        continue
                    if ambient_noise_magnitude is None:
                        ambient_noise_magnitude = magnitude
                    else:
                        if len(ambient_noise_magnitude) != len(magnitude):
                            ambient_noise_magnitude = np.resize(ambient_noise_magnitude, magnitude.shape)

                        ambient_noise_magnitude = 0.9 * ambient_noise_magnitude + 0.1 * magnitude
                    continue

                with voice_lock:
                    push_preview("\U0001F9E0 Interpreting speech...", "#ffb300")

                    if USE_SPECTRAL_SUBTRACTION and ambient_noise_magnitude is not None:
                        if len(ambient_noise_magnitude) != len(magnitude):
                            ambient_noise_magnitude = np.resize(ambient_noise_magnitude, magnitude.shape)
                        magnitude = np.maximum(magnitude - (ambient_noise_magnitude * 0.50), 0.0)
                        audio_data = np.fft.irfft(magnitude * np.exp(1j * phase)).astype(np.float32)

                    if system_state.WRITING_MODE_ACTIVE:
                        prompt_context = "Dictating continuous text into document. Standard casing, clean grammar, with formal punctuation."
                        current_beam = 5
                        current_temp = 0.0
                    else:
                        prompt_context = None
                        current_beam = 3
                        current_temp = 0.0

                    result = model.transcribe(
                        audio_data, fp16=False, language="en", temperature=current_temp,
                        beam_size=current_beam,
                        compression_ratio_threshold=2.4, no_speech_threshold=0.6,
                        condition_on_previous_text=False,
                        initial_prompt=prompt_context
                    )

                    kept = []
                    for seg in result.get("segments", []):
                        nsp, lp, cr = seg.get("no_speech_prob", 0), seg.get("avg_logprob", 0), seg.get("compression_ratio", 0)
                        if nsp > 0.6 or (lp < -1.0 and nsp > 0.3) or cr > 2.4:
                            continue
                        kept.append(seg["text"].strip())
                    text_output = " ".join(kept).strip()
                    if is_hallucination(text_output, prompt_context):
                        text_output = ""
                    clean_text = text_output.strip().replace(".", "").replace(",", "").replace("!", "")

                    if VOICE_LOCKED:
                        # Locked: nothing is logged, shown or run, except the phrase that unlocks again.
                        if clean_text and _contains_command(clean_text, unlock_phrases):
                            set_voice_locked(False, gui_ref)
                            push_preview("🔓 Voice unlocked", "#00e676")
                            preview_clear_at = time.time() + 2.5
                        continue

                    if len(clean_text) > 2 and clean_text.lower() not in ["uh", "oh", "um", "the"]:
                        if gui_ref:
                            gui_ref.log_message(f"[INPUT] User Said: \"{text_output}\"")
                        else:
                            print(f"[AI WHISPER] Heard: \"{text_output}\"")

                        # Don't run yet: merge with anything still pending and wait for the speaker to finish.
                        pending_text = (pending_text + " " + text_output).strip()
                        pending_label, pending_kind = interpret_voice_intent(pending_text, config_payload)
                        hold = 0.0 if system_state.WRITING_MODE_ACTIVE else COMMIT_DELAY
                        text, color = _format_pending_preview(pending_text, pending_label, pending_kind, hold)
                        push_preview(text, color)
                        last_preview_push = time.time()
                    elif not pending_text:
                        idle_preview()

    push_preview("Voice: idle", "#6C6B80")

def execute_voice_intent(phrase, config_payload, gui_ref=None):
    if not phrase:
        return

    phrase_clean = phrase.strip().replace("!", "").replace(".", "")
    phrase_lower = phrase_clean.lower()

    if _is_exact_command(phrase_lower, _lock_phrases(config_payload)):
        set_voice_locked(True, gui_ref)
        return

    if "activate writing mode" in phrase_lower or "start typing" in phrase_lower:
        if not system_state.WRITING_MODE_ACTIVE:
            system_state.WRITING_MODE_ACTIVE = True
            system_state.HUD_REFRESH_REQUIRED = True
            msg = "\n[OVERRIDE ACTIVE] Entering Dictation Mode. Keyboard emulation enabled.\n"
            if gui_ref: gui_ref.log_message(msg)
        return

    if "disable writing mode" in phrase_lower or "stop typing" in phrase_lower:
        if system_state.WRITING_MODE_ACTIVE:
            system_state.WRITING_MODE_ACTIVE = False
            system_state.HUD_REFRESH_REQUIRED = True
            msg = "\n[OVERRIDE TERMINATED] Exited Writing Mode. Standard command matrix restored.\n"
            if gui_ref: gui_ref.log_message(msg)
        return

    if system_state.WRITING_MODE_ACTIVE:
        token_check = phrase.strip().lower().replace(".", "").replace(",", "").replace("!", "")
        
        formatting_macros = {
            "new paragraph": lambda: keyboard.send("enter, enter"),
            "new line":      lambda: keyboard.send("enter"),
            "period":        lambda: keyboard.write(". "),
            "comma":         lambda: keyboard.write(", "),
            "question mark": lambda: keyboard.write("? "),
            "exclamation point": lambda: keyboard.write("! "),
            "backspace":     lambda: keyboard.send("backspace"),
            "clear line":    lambda: keyboard.send("shift+home, backspace")
        }

        if token_check in formatting_macros:
            formatting_macros[token_check]()
            msg = f"[FORMATTING] Executed Layout Action: {token_check.upper()}"
        else:
            processed_chunk = phrase.strip()
            keyboard.write(processed_chunk + " ") 
            msg = f"[DICTATION] Written to field: \"{processed_chunk}\""
            
        if gui_ref: gui_ref.log_message(msg)
        return  

    phrase_lower = phrase_lower.strip()
    dispatcher = _dispatcher()

    # LEVEL 1: shortcuts for the focused app. Goes through the dispatcher like every other command, so it is
    # logged with its outcome and shows up in the camera HUD (it used to bypass both).
    shortcut = find_window_shortcut(phrase_lower, config_payload)
    if shortcut:
        hotkey_str, context = shortcut
        if gui_ref:
            gui_ref.log_message(f"[CONTEXT MATCH] Focused: {str(context).upper()} -> Hotkey: {hotkey_str}")
        dispatcher.dispatch_system_action(action_name=f"hotkey:{hotkey_str}", raw_phrase=phrase_lower,
                                          gui_ref=gui_ref, window_context=str(context))
        return

    # LEVEL 2: launch phrases for apps and websites
    target_clean = find_launcher(phrase_lower, config_payload)
    if target_clean:
        if _is_url(target_clean):
            action_type = "launch_custom_url"
            log_tag = f"[URL LAUNCH] Web destination matched: {target_clean}"
        else:
            action_type = "launch_custom_executable"
            log_tag = f"[EXE LAUNCH] Local binary path matched: {os.path.basename(target_clean)}"
        if gui_ref:
            gui_ref.log_message(log_tag)
        else:
            print(log_tag)
        dispatcher.dispatch_system_action(action_name=action_type, raw_phrase=target_clean, gui_ref=gui_ref)
        return

    # LEVEL 3: global commands (exact phrase)
    matched_action = find_global_action(phrase_lower, config_payload)

    if matched_action in ("voice_lock", "voice_unlock"):
        set_voice_locked(matched_action == "voice_lock", gui_ref)
        return

    if matched_action:
        dispatcher.dispatch_system_action(action_name=matched_action, raw_phrase=phrase_lower, gui_ref=gui_ref)
