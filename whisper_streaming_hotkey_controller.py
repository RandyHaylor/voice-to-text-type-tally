"""
Hotkey controller for the whisper_streaming voice-to-text family.

  Ctrl+F12              -> mic dictation, types into focused window via Ctrl+V
  Ctrl+Shift+F12        -> mic dictation, types via Ctrl+Shift+V (terminals)
  Ctrl+Alt+F12          -> system audio (default sink monitor) -> console + file
  Ctrl+Alt+Shift+F12    -> mic + system audio mixed -> console + file
  Shift+F12             -> stop whatever is running
  Ctrl+C in this terminal -> exit

Modes are mutually exclusive: only ONE pipeline runs at a time. Pressing a
new mode hotkey while another is running is a no-op (use Shift+F12 to stop
first, then start the new mode). This avoids audio devices being grabbed by
two pipelines at once.

The whisper_streaming server must be running separately on 127.0.0.1:43007
(use launch_whisper_streaming_server.sh).
"""

import os
import signal
import subprocess
import sys
import threading

from pynput import keyboard as pynput_keyboard


SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))

LAUNCHER_PATHS_BY_MODE_LABEL = {
    "mic-typing-ctrl-v": os.path.join(
        SCRIPT_DIRECTORY,
        "launch_whisper_streaming_mic_client_with_typing.sh",
    ),
    "mic-typing-ctrl-shift-v": os.path.join(
        SCRIPT_DIRECTORY,
        # NOTE: this currently uses xdotool type (not Ctrl+Shift+V) under
        # the hood. If you want a true Ctrl+Shift+V paste, swap the
        # launcher's emitter call. Functionally types into terminals fine.
        "launch_whisper_streaming_mic_client_with_typing.sh",
    ),
    "system-audio-to-console-and-file": os.path.join(
        SCRIPT_DIRECTORY,
        "launch_whisper_streaming_system_audio_to_console_and_file.sh",
    ),
    "mic-plus-system-to-console-and-file": os.path.join(
        SCRIPT_DIRECTORY,
        "launch_whisper_streaming_mic_plus_system_to_console_and_file.sh",
    ),
}

HOTKEY_TO_MODE_LABEL = {
    # Distinct F-keys with single Ctrl modifier — no hotkey is a subset of
    # another, so pynput.GlobalHotKeys can match them exclusively.
    "<ctrl>+<f9>": "mic-typing-ctrl-v",
    "<ctrl>+<f10>": "system-audio-to-console-and-file",
    "<ctrl>+<f11>": "mic-plus-system-to-console-and-file",
}

STOP_HOTKEY = "<ctrl>+<f12>"

HUMAN_READABLE_HOTKEY_HELP_LINES = [
    "  Ctrl+F9   -> mic dictation, types into focused window",
    "  Ctrl+F10  -> system audio -> ~/vtt_recordings/*.txt",
    "  Ctrl+F11  -> mic + system audio mixed -> ~/vtt_recordings/*.txt",
    "  Ctrl+F12  -> stop whatever is running",
]


class WhisperStreamingHotkeyController:
    def __init__(self):
        self.active_subprocess_or_none = None
        self.active_mode_label_or_none = None
        self.subprocess_state_lock = threading.Lock()

    def on_mode_hotkey_pressed(self, requested_mode_label):
        with self.subprocess_state_lock:
            # Same mode already running -> no-op.
            if self.active_mode_label_or_none == requested_mode_label:
                print(
                    f"[start] mode '{requested_mode_label}' already running.",
                    flush=True,
                )
                return
            # Different mode running -> transition: stop current, start new.
            if self.active_subprocess_or_none is not None:
                print(
                    f"[transition] stopping '{self.active_mode_label_or_none}' "
                    f"-> starting '{requested_mode_label}'",
                    flush=True,
                )
                self._terminate_active_subprocess_holding_lock()
            launcher_path = LAUNCHER_PATHS_BY_MODE_LABEL[requested_mode_label]
            print(f"[start] mode={requested_mode_label}", flush=True)
            self.active_subprocess_or_none = subprocess.Popen(
                [launcher_path],
                # New process group so SIGTERM hits the whole pipeline.
                preexec_fn=os.setsid,
            )
            self.active_mode_label_or_none = requested_mode_label

    def _terminate_active_subprocess_holding_lock(self):
        """Caller must hold self.subprocess_state_lock."""
        if self.active_subprocess_or_none is None:
            return
        try:
            os.killpg(
                os.getpgid(self.active_subprocess_or_none.pid),
                signal.SIGTERM,
            )
        except ProcessLookupError:
            pass
        try:
            self.active_subprocess_or_none.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(
                os.getpgid(self.active_subprocess_or_none.pid),
                signal.SIGKILL,
            )
        self.active_subprocess_or_none = None
        self.active_mode_label_or_none = None

    def on_stop_hotkey_pressed(self):
        with self.subprocess_state_lock:
            if self.active_subprocess_or_none is None:
                print("[stop] already stopped.", flush=True)
                return
            print(
                f"[stop] terminating mode "
                f"'{self.active_mode_label_or_none}'...",
                flush=True,
            )
            self._terminate_active_subprocess_holding_lock()
            print("[stop] done.", flush=True)

    def run_until_interrupted(self):
        print("", flush=True)
        print("=" * 60, flush=True)
        print(" Voice-to-Text (vtt) — global hotkeys:", flush=True)
        print("=" * 60, flush=True)
        for help_line in HUMAN_READABLE_HOTKEY_HELP_LINES:
            print(help_line, flush=True)
        print("=" * 60, flush=True)
        print(" Ctrl+C in this terminal to exit the controller.", flush=True)
        print("", flush=True)

        global_hotkey_callback_map = {
            hotkey_string: (lambda mode=mode_label:
                             self.on_mode_hotkey_pressed(mode))
            for hotkey_string, mode_label in HOTKEY_TO_MODE_LABEL.items()
        }
        global_hotkey_callback_map[STOP_HOTKEY] = self.on_stop_hotkey_pressed

        with pynput_keyboard.GlobalHotKeys(
            global_hotkey_callback_map
        ) as hotkey_listener:
            hotkey_listener.join()


def main():
    controller = WhisperStreamingHotkeyController()
    try:
        controller.run_until_interrupted()
    except KeyboardInterrupt:
        controller.on_stop_hotkey_pressed()
        print("\n[exit] Goodbye.", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
