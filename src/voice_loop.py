import system_state
import keyboard 
import os
import sys

def execute_voice_intent(phrase, mappings, gui_ref=None):
    phrase_clean = phrase.strip().replace("!", "").replace(".", "")
    phrase_lower = phrase_clean.lower()

    if "activate writing mode" in phrase_lower or "start typing" in phrase_lower:
        if not system_state.WRITING_MODE_ACTIVE:
            system_state.WRITING_MODE_ACTIVE = True
            msg = "\n[OVERRIDE ACTIVE] Entering Dictation/Writing Mode. Shortcut parsing disabled.\n"
            if gui_ref: gui_ref.log_message(msg)
            else: print(msg)
        return

    elif "disable writing mode" in phrase_lower or "stop typing" in phrase_lower:
        if system_state.WRITING_MODE_ACTIVE:
            system_state.WRITING_MODE_ACTIVE = False
            msg = "\n[OVERRIDE TERMINATED] Exited Writing Mode. Standard command matrix restored.\n"
            if gui_ref: gui_ref.log_message(msg)
            else: print(msg)
        return

    # OVERRIDE
    if system_state.WRITING_MODE_ACTIVE:
        if phrase_clean:
            token_check = phrase_lower.strip()

            formatting_macros = {
                "new paragraph": lambda: keyboard.send("enter, enter"),
                "new line":      lambda: keyboard.send("enter"),
                "period":        lambda: keyboard.write(". "),
                "comma":         lambda: keyboard.write(", "),
                "question mark": lambda: keyboard.write("? "),
                "exclamation point": lambda: keyboard.write("! "),
                "backspace":     lambda: keyboard.send("backspace"),
                "clear line":    lambda: keyboard.send("shift+home, backspace"),
                "capslock":      lambda: keyboard.send("capslock")
            }

            if token_check in formatting_macros:
                formatting_macros[token_check]()
                msg = f"[FORMATTING] Executed Layout Action: {token_check.upper()}"
            else:
                processed_chunk = phrase_clean[0].upper() + phrase_clean[1:] if len(phrase_clean) > 1 else phrase_clean
                
                keyboard.write(processed_chunk + " ") 
                msg = f"[DICTATION] Written to field: \"{processed_chunk}\""
                
            if gui_ref: gui_ref.log_message(msg)
            else: print(msg)
        return

    # STANDARD SHORCUT ROUTING (Only reached if WRITING_MODE_ACTIVE is False)
    matched_action = None
    for map_phrase, action in mappings.items():
        if map_phrase in phrase_lower:
            matched_action = action
            break
            
    if matched_action:
        if 'action_dispatcher' not in sys.modules:
            import action_dispatcher
            
        sys.modules['action_dispatcher'].dispatch_system_action(
            action_name=matched_action, 
            raw_phrase=phrase_lower, 
            gui_ref=gui_ref
        )

    else:
        msg = "[SYSTEM] Command phrase recognized, but criteria not met."
        if gui_ref: gui_ref.log_message(msg)
        else: print(msg)
