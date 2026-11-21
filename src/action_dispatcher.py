import os
import re
import time
import subprocess
import pyautogui
import webbrowser
import pyperclip
import keyboard
import system_state
import action_verifier
from action_verifier import ActionFailed      # re-exported: other modules raise action_dispatcher.ActionFailed
from database_manager import log_command_execution

try: 
    import hud_bus
except Exception:
    hud_bus = None


def _hud(source, action, status, detail=""):
    if hud_bus is not None and action != "index_point":   # pointing is constant, keep it out of the list
        try:
            hud_bus.push_event(source, action, status, detail)
        except Exception:
            pass

def _start_app(command):
    """Runs `start <command>` and reports a failure when Windows could not launch it."""
    code = os.system(f"start {command}")
    if code != 0:
        raise ActionFailed(f"Could not start '{command}' (exit code {code})")


def _open_url(url):
    if not webbrowser.open(url):
        raise ActionFailed(f"No web browser could open {url}")


def _record_execution(action_name, raw_phrase, gui_ref, status="success", reason="", window_context="Global",
                      verification="unverified"):
    """Counts and logs one executed command with its outcome: success / failed + reason, and whether a check
    confirmed the success (verification = confirmed / unverified / failed)."""
    if status == "success":
        try:
            import vision_module
            import voice_module
            if raw_phrase:
                voice_module.VOICE_COUNT = getattr(voice_module, "VOICE_COUNT", 0) + 1
            else:
                vision_module.GESTURE_COUNT = getattr(vision_module, "GESTURE_COUNT", 0) + 1
        except Exception:
            pass

    trigger_source = "Voice" if raw_phrase else "Vision"
    log_command_execution(action_name, trigger_source, window_context or "Global",
                          status=status, failure_reason=reason, verification=verification)
    _hud(trigger_source, action_name, "ok" if status == "success" else "fail", reason)

    if status == "success":
        tag = " (verified)" if verification == "confirmed" else ""
        log_msg = f"[DISPATCHER] Core Execution triggered: {action_name.upper()}{tag}"
    else:
        log_msg = f"[DISPATCHER] FAILED: {action_name.upper()} - {reason}"
    if gui_ref: gui_ref.log_message(log_msg)
    else: print(log_msg)

def dispatch_system_action(action_name, current_time=None, cooldowns=None, raw_phrase="", gui_ref=None,
                           window_context="Global", bypass_lock=False):
    """Runs one action and logs it. Gesture actions (no raw_phrase) are ignored while the system is locked,
    unless bypass_lock is set (fist play/pause works while locked).
    current_time / cooldowns are accepted for compatibility; cooldowns are managed by the vision loop."""
    if not action_name or action_name == "system_idle":
        return

    if action_name == "wake_gate":
        _record_execution(action_name, raw_phrase, gui_ref)
        system_state.SYSTEM_UNLOCKED = not system_state.SYSTEM_UNLOCKED
        status = "UNLOCKED" if system_state.SYSTEM_UNLOCKED else "LOCKED"
        print(f"[DISPATCHER] Master core system gate toggled to: {status}")
        return

    # Lock check comes before logging so blocked gestures never reach the usage report.
    if not system_state.SYSTEM_UNLOCKED and not raw_phrase and not bypass_lock:
        if action_name != "index_point":
            _hud("Vision", action_name, "blocked", "System is locked")
            return

    # 1. pre-check: can the command work at all? (a window to type into, something to paste, media to pause...)
    # 2. run it
    # 3. post-check: did the expected effect happen? This runs in the background and the outcome is logged
    #    when it is known, so the camera and voice loops never wait for it.
    # Anything that raises, or a check that sees the command had no effect, is stored as "failed" with the reason.
    try:
        plan = action_verifier.prepare(action_name, raw_phrase)
        _run_action(action_name, raw_phrase, gui_ref)
    except ActionFailed as exc:
        _record_execution(action_name, raw_phrase, gui_ref, "failed", str(exc), window_context)
        return
    except Exception as exc:
        _record_execution(action_name, raw_phrase, gui_ref, "failed", f"{type(exc).__name__}: {exc}", window_context)
        return

    if plan.post is None:
        # nothing to observe for this command (mouse clicks, scrolling, Enter...): it ran, but is not proven
        _record_execution(action_name, raw_phrase, gui_ref, "success", "", window_context, "unverified")
    else:
        action_verifier.confirm_later(
            plan, lambda status, reason, verification="unverified": _record_execution(
                action_name, raw_phrase, gui_ref, status, reason, window_context, verification))


def _run_action(action_name, raw_phrase, gui_ref):
    """Performs the action. Raises (ActionFailed or any exception) when it could not be carried out."""
    if action_name == "volume_up":
        pyautogui.press('volumeup')

    elif action_name.startswith("hotkey:"):
        keys = [k.strip() for k in action_name.partition(":")[2].lower().split("+") if k.strip()]
        if not keys:
            raise ActionFailed("No keys were defined for this hotkey")
        print(f"Executing Custom Action: Hotkey -> {keys}")
        pyautogui.hotkey(*keys)

    elif action_name.startswith("launch:"):
        payload = action_name.partition(":")[2].strip()
        if not payload:
            raise ActionFailed("No app or link was defined for this launcher")
        print(f"Executing Custom Action: Launch -> {payload}")
        if payload.lower().startswith(("http://", "https://", "www.")):
            _open_url(payload if payload.lower().startswith("http") else "https://" + payload)
        elif os.path.exists(payload):
            if hasattr(os, "startfile"):
                os.startfile(payload)
            else:
                subprocess.Popen([payload])
        else:
            _open_url("https://" + payload)

    elif action_name == "launch_custom_executable":
        if not raw_phrase:
            raise ActionFailed("No program was specified")
        if os.path.isabs(raw_phrase) and not os.path.exists(raw_phrase):
            raise ActionFailed(f"Program not found: {raw_phrase}")
        subprocess.Popen(f'start "" "{raw_phrase}"', shell=True)

    elif action_name == "launch_custom_url":
        if not raw_phrase:
            raise ActionFailed("No web address was specified")
        url_target = raw_phrase
        if url_target.lower().startswith("www."):
            url_target = "https://" + url_target
        _open_url(url_target)

    elif action_name == "volume_down":
        pyautogui.press('volumedown')

    elif action_name == "previous_track":
        pyautogui.press('prevtrack')

    elif action_name == "next_track":
        pyautogui.press('nexttrack')

    elif action_name == "media_play_pause":
        pyautogui.press('playpause')

    elif action_name == "trigger_screenshot":
        # Win + Print Screen saves a full-screen shot to Pictures > Screenshots. (This action was offered in
        # Settings and used by the default victory-sign mapping, but had no handler, so it always failed.)
        pyautogui.hotkey('win', 'printscreen')

    elif action_name == "maximize":
        pyautogui.hotkey('win', 'up')

    elif action_name == "minimize":
        pyautogui.hotkey('win', 'd')

    elif action_name == "mouse_click_left":
        pyautogui.click()
    elif action_name == "mouse_click_right":
        pyautogui.click(button='right')
    elif action_name == "mouse_double_click":
        pyautogui.doubleClick()
    elif action_name == "scroll_view_down":
        pyautogui.scroll(-300)
    elif action_name == "scroll_view_up":
        pyautogui.scroll(300)

    elif action_name == "launch_paint":
        _start_app("mspaint")
        
    elif action_name == "launch_calculator":
        _start_app("calc")
        
    elif action_name == "launch_file_explorer":
        _start_app("explorer")
        
    elif action_name == "launch_browser_google":
        _open_url("https://google.com")

    elif action_name == "toggle_screen_magnifier":
        pyautogui.hotkey('win', '+') 
    elif action_name == "toggle_high_contrast_theme":
        # 'left alt' / 'left shift' are not pyautogui key names, so this used to press only Print Screen
        pyautogui.hotkey('altleft', 'shiftleft', 'printscreen')

    elif action_name == "next_tab":
        pyautogui.hotkey('ctrl', 'tab')
    elif action_name == "prev_tab":
        pyautogui.hotkey('ctrl', 'shift', 'tab')

    elif action_name == "minimize_all_windows":
        pyautogui.hotkey('win', 'd')
    elif action_name == "cycle_active_applications":
        pyautogui.hotkey('alt', 'tab')
    elif action_name == "snap_window_left_side":
        pyautogui.hotkey('win', 'left')
    elif action_name == "snap_window_right_side":
        pyautogui.hotkey('win', 'right')

    elif action_name == "form_next_field":
        pyautogui.press('tab')
        
    elif action_name == "form_prev_field":
        pyautogui.hotkey('shift', 'tab') 
        
    elif action_name == "form_select_all":
        pyautogui.hotkey('ctrl', 'a')  
        
    elif action_name == "form_clear_field":
        pyautogui.hotkey('ctrl', 'a')
        time.sleep(0.05)
        pyautogui.press('backspace')  
        
    elif action_name == "form_submit":
        pyautogui.press('enter') 

    elif action_name == "edit_undo":
        pyautogui.hotkey('ctrl', 'z')
        
    elif action_name == "edit_redo":
        pyautogui.hotkey('ctrl', 'y')
        
    elif action_name == "edit_copy":
        pyautogui.hotkey('ctrl', 'c')
        
    elif action_name == "edit_paste":
        pyautogui.hotkey('ctrl', 'v')
        
    elif action_name == "edit_delete_line":
        pyautogui.hotkey('home')
        time.sleep(0.05)
        pyautogui.hotkey('shift', 'end')
        time.sleep(0.05)
        pyautogui.press('backspace')

    elif action_name == "launch_chrome":
        _start_app("chrome")
    elif action_name == "launch_youtube":
        _open_url("https://youtube.com")
    elif action_name == "launch_notepad":
        _start_app("notepad")
    elif action_name == "start_microsoft_word":
        _start_app("winword")
    elif action_name == "launch_gemini":
        _open_url("https://gemini.google.com")
    elif action_name == "launch_google_slides":
        _open_url("https://slides.google.com")
    elif action_name == "launch_google_docs":
        _open_url("https://docs.google.com")
    elif action_name == "launch_google_drive":
        _open_url("https://drive.google.com")        
    elif action_name == "new_tab":
        pyautogui.hotkey('ctrl', 't')       
    elif action_name == "launch_microsoft_excel":
        _start_app("excel")
    elif action_name == "launch_microsoft_powerpoint":
        _start_app("powerpnt")
    elif action_name == "launch_microsoft_outlook": 
        _start_app("outlook")
    elif action_name == "launch_microsoft_teams":
        _start_app("msteams:")
    elif action_name == "launch_microsoft_edge":
        _start_app("msedge")
    elif action_name == "launch_microsoft_onenote":
        _start_app("onenote:")
    elif action_name == "launch_microsoft_photos":
        _start_app("ms-photos:")
    elif action_name == "launch_microsoft_camera":
        _start_app("microsoft.windows.camera:")
    elif action_name == "launch_microsoft_paint":
        _start_app("mspaint")
    elif action_name == "launch_calendar":    
        _start_app("calendar")
    elif action_name == "enter":
        pyautogui.press('enter')
    elif action_name == "exit":
        pyautogui.hotkey('alt', "f4")

    elif action_name.startswith("autofill_"):
        target_field = action_name.replace("autofill_", "")

        import database_manager
        data_string = database_manager.get_profile_data(target_field)
        
        if data_string:
            msg = f"[PROFILE ENGINE] Form autofill writing matched record: {target_field.upper()}"
            if gui_ref: gui_ref.log_message(msg)
            else: print(msg)

            # keyboard.write sends Unicode, so names like "Peñaflor" are typed correctly
            # (pyautogui.write silently skipped characters such as ñ)
            keyboard.write(data_string)
        else:
            raise ActionFailed(f"No saved profile entry for '{target_field}'")
    
    elif action_name == "zoom_in":
        pyautogui.hotkey('ctrl', '+')
    elif action_name == "zoom_out":
        pyautogui.hotkey('ctrl', '-')

    elif action_name == "trigger_voice_search":
        _start_app('chrome "https://google.com"')
        time.sleep(1.2)
        pyautogui.hotkey('ctrl', 'shift', '.')

    elif action_name == "jump_timestamp":
        text_to_clean = raw_phrase.lower()
        word_digits = {
            "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
            "six": "6", "seven": "7", "eight": "8", "nine": "9", "zero": "0"
        }
        for word, digit in word_digits.items():
            text_to_clean = re.sub(r'\b' + word + r'\b', digit, text_to_clean)

        percent_match = re.search(r'(\d+)\s*(percent|%)', text_to_clean)
        if percent_match:
            pct_val = int(percent_match.group(1))
            target_key = str(pct_val // 10)
            if target_key in "0123456789":
                pyautogui.press(target_key)
                msg = f"[VIDEO ENGINE] Page-safe jump to {pct_val}%"
                if gui_ref: gui_ref.log_message(msg)
                else: print(msg)
                return

        hours = re.search(r'(\d+)\s*(hour|hr)', text_to_clean)
        minutes = re.search(r'(\d+)\s*(minute|min)', text_to_clean)
        seconds = re.search(r'(\d+)\s*(second|sec)', text_to_clean)
        
        h_val = int(hours.group(1)) if hours else 0
        m_val = int(minutes.group(1)) if minutes else 0
        s_val = int(seconds.group(1)) if seconds else 0
        
        if h_val == 0 and m_val == 0 and s_val == 0:
            raw_numbers = re.findall(r'\d+', text_to_clean)
            if len(raw_numbers) == 1:
                s_val = int(raw_numbers[0])
            elif len(raw_numbers) == 2:
                m_val, s_val = int(raw_numbers[0]), int(raw_numbers[1])
            elif len(raw_numbers) == 3:
                h_val, m_val, s_val = int(raw_numbers[0]), int(raw_numbers[1]), int(raw_numbers[2])
        
        total_seconds = (h_val * 3600) + (m_val * 60) + s_val

        if total_seconds > 0:
            msg = f"[VIDEO ENGINE] Navigation target set to: {total_seconds} seconds"
            if gui_ref: gui_ref.log_message(msg)
            else: print(msg)
            
            pyautogui.hotkey('ctrl', 'l')          
            time.sleep(0.15)
            pyautogui.hotkey('ctrl', 'c')          
            time.sleep(0.15)
            
            current_url = pyperclip.paste()
            if not str(current_url).lower().startswith(("http://", "https://")):
                pyautogui.press('esc')
                raise ActionFailed("Could not read the video's address (is a video page open in the browser?)")
            clean_url = re.sub(r'([&?](t|start)=\d+s?)', '', current_url)
            separator = '&' if '?' in clean_url else '?'
            new_url = f"{clean_url}{separator}t={total_seconds}s"
            
            pyperclip.copy(new_url)                
            time.sleep(0.1)
            pyautogui.hotkey('ctrl', 'v')          
            time.sleep(0.1)
            pyautogui.press('enter')               
        else:
            raise ActionFailed("Could not understand the time that was said")

    elif action_name == "index_point":
        pass  # pointing only drives the wake gate in the vision loop; nothing to execute here

    else:
        raise ActionFailed("No handler exists for this action")