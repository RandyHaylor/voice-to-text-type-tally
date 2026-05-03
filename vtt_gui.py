#!/usr/bin/env python3
"""
Cross-platform Tkinter GUI for the whisper_streaming voice-to-text family.

This is a NEW universal entry point that runs alongside the Linux-only
hotkey controller (whisper_streaming_hotkey_controller.py). It does NOT
modify any existing files.

Requirements:
- whisper_streaming server reachable at 127.0.0.1:43007.
- cross_platform_audio_sources.py (sibling module) provides the ffmpeg
  command builder and loopback availability helpers.
- pynput is used for cross-platform keystroke injection (already in
  requirements.txt).

Run:
    python3 vtt_gui.py
"""

import datetime
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext

try:
    from pynput.keyboard import Controller as KeyboardController
except Exception:  # pragma: no cover - pynput should be present per reqs
    KeyboardController = None

import cross_platform_audio_sources as audio_sources


SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 43007
SERVER_PROBE_TIMEOUT_SECONDS = 0.5
SERVER_READY_POLL_INTERVAL_SECONDS = 0.5
SERVER_READY_TIMEOUT_SECONDS = 60.0

TRANSCRIPTS_DIRECTORY = Path.home() / "vtt_recordings"

LOCAL_MODELS_PARENT_DIRECTORY = Path(SCRIPT_DIRECTORY) / "models"
DEFAULT_WHISPER_MODEL_NAME = "base"

HELP_DOCUMENT_PATH = Path(SCRIPT_DIRECTORY) / "HELP.md"


def list_available_nvidia_gpu_indices_with_names():
    """Return [(index_string, label_string), ...] from `nvidia-smi -L`.
    Empty list if no NVIDIA driver / no GPUs."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if result.returncode != 0:
            return []
        gpus_in_order = []
        for line_text in result.stdout.splitlines():
            stripped = line_text.strip()
            # Lines look like: "GPU 0: NVIDIA GeForce RTX 3090 Ti (UUID: ...)"
            if not stripped.startswith("GPU "):
                continue
            try:
                colon_index = stripped.index(":")
                index_part = stripped[len("GPU "):colon_index].strip()
                name_part = stripped[colon_index + 1 :].strip()
                # Drop the "(UUID: ...)" suffix to keep label short.
                if "(UUID:" in name_part:
                    name_part = name_part.split("(UUID:")[0].strip()
                gpus_in_order.append((index_part, name_part))
            except ValueError:
                continue
        return gpus_in_order
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def is_nvidia_gpu_available_for_whisper():
    """Probe `nvidia-smi -L`. Cross-platform — nvidia-smi exists wherever
    NVIDIA drivers are installed (Linux/Windows/Mac-with-eGPU). Returns
    False on any error (binary missing, no GPU, driver issue)."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        # `-L` lists GPUs; non-empty stdout AND exit 0 means a GPU is present.
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def list_locally_available_whisper_model_names():
    """
    Scan <repo>/models/ for subdirectories that contain a `model.bin` file.
    Returns a sorted list of model names. Used to populate the model
    dropdown — only what the user has on disk is shown. To add a new
    model, drop a faster-whisper / CTranslate2 model directory into
    <repo>/models/<name>/ and restart the GUI.
    """
    if not LOCAL_MODELS_PARENT_DIRECTORY.is_dir():
        return []
    model_names = []
    for child in LOCAL_MODELS_PARENT_DIRECTORY.iterdir():
        if child.is_dir() and (child / "model.bin").is_file():
            model_names.append(child.name)
    return sorted(model_names)

LINUX_SERVER_LAUNCHER_PATH = os.path.join(
    SCRIPT_DIRECTORY, "launch_whisper_streaming_server.sh"
)

MANUAL_SERVER_INSTRUCTIONS_BY_OS = {
    "Linux":
        "Run: bash launch_whisper_streaming_server.sh",
    "Darwin":
        "On macOS, manually start the server:\n"
        "  cd whisper_streaming\n"
        "  python3 whisper_online_server.py --host 127.0.0.1 --port 43007 \\\n"
        "      --backend faster-whisper --model_dir ../models/base --lan en",
    "Windows":
        "On Windows, manually start the server (PowerShell):\n"
        "  cd whisper_streaming\n"
        "  python whisper_online_server.py --host 127.0.0.1 --port 43007 "
        "--backend faster-whisper --model_dir ..\\models\\base --lan en",
}


# Mode labels.
MODE_MIC_PREVIEW = "mic_preview"
MODE_MIC_TYPING = "mic_typing"
MODE_MIC_TO_FILE = "mic_to_file"
MODE_SYSTEM_TO_FILE = "system_to_file"
MODE_MIXED_TO_FILE = "mixed_to_file"

# Map mode -> audio_mode_name expected by cross_platform_audio_sources.
MODE_TO_AUDIO_SOURCE_NAME = {
    MODE_MIC_PREVIEW: "mic",
    MODE_MIC_TYPING: "mic",
    MODE_MIC_TO_FILE: "mic",
    MODE_SYSTEM_TO_FILE: "system_audio",
    MODE_MIXED_TO_FILE: "mic_plus_system_mixed",
}

MODE_HUMAN_LABEL = {
    MODE_MIC_PREVIEW: "mic→window",
    MODE_MIC_TYPING: "mic→typing",
    MODE_MIC_TO_FILE: "mic→file",
    MODE_SYSTEM_TO_FILE: "system→file",
    MODE_MIXED_TO_FILE: "mic+system→file",
}

MODE_FILE_PREFIX = {
    MODE_MIC_TO_FILE: "mic_transcript",
    MODE_SYSTEM_TO_FILE: "system_audio_transcript",
    MODE_MIXED_TO_FILE: "mic_plus_system_transcript",
}


def is_server_reachable():
    """
    True if the whisper_streaming server is up. Uses two checks:

      (1) TCP connect probe to <host>:<port>. Confirms socket is bound.
      (2) Process existence check by name. The whisper_streaming server
          uses `s.listen(1)` (backlog=1), so during an active client
          session the TCP probe can time out even though the server is
          fine. Falling back to a process-name check avoids false DOWN.

    Either succeeding -> reachable. Both failing -> down.
    """
    try:
        with socket.create_connection(
            (SERVER_HOST, SERVER_PORT), timeout=SERVER_PROBE_TIMEOUT_SECONDS
        ):
            return True
    except (OSError, socket.timeout):
        pass
    return is_whisper_streaming_server_process_running()


_WINDOWS_SERVER_PROCESS_NAME_SUBSTRINGS = (
    "whisper_online_server.py",
    "whisper_streaming_server_runner_with_device_choice.py",
)


def find_whisper_streaming_server_process_ids_on_windows():
    """Returns a list of PID strings for python processes whose command
    line includes our server-runner script name. Uses `wmic` because
    `tasklist` doesn't expose the command line. Empty list if none."""
    try:
        result = subprocess.run(
            [
                "wmic",
                "process",
                "where",
                "Name='python.exe' or Name='pythonw.exe'",
                "get",
                "ProcessId,CommandLine",
                "/FORMAT:CSV",
            ],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if result.returncode != 0:
            return []
        matched_process_ids = []
        for line in result.stdout.splitlines():
            line_stripped = line.strip()
            if not line_stripped or line_stripped.startswith("Node,"):
                continue
            if not any(
                substring in line_stripped
                for substring in _WINDOWS_SERVER_PROCESS_NAME_SUBSTRINGS
            ):
                continue
            # CSV columns: Node, CommandLine, ProcessId
            csv_parts = line_stripped.rsplit(",", 1)
            if len(csv_parts) == 2 and csv_parts[1].strip().isdigit():
                matched_process_ids.append(csv_parts[1].strip())
        return matched_process_ids
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def is_whisper_streaming_server_process_running():
    """Cross-platform process-name check. Avoids importing psutil.
    Matches either the legacy direct invocation
    (`whisper_online_server.py`) OR our cross-platform wrapper
    (`whisper_streaming_server_runner_with_device_choice.py`)."""
    system_name = platform.system()
    try:
        if system_name == "Windows":
            return bool(find_whisper_streaming_server_process_ids_on_windows())
        # Linux + macOS — pgrep with a single regex matching either name.
        result = subprocess.run(
            [
                "pgrep",
                "-f",
                "whisper_online_server.py|whisper_streaming_server_runner_with_device_choice.py",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def find_pid_listening_on_tcp_port_on_windows(port_number):
    """Return the PID (string) of the process LISTENING on 127.0.0.1:port,
    or None. Uses `netstat -ano` — no dependency on the deprecated wmic."""
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=3.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    needle = ":" + str(port_number)
    for line in result.stdout.splitlines():
        if "LISTENING" not in line or needle not in line:
            continue
        parts = line.split()
        # Format: Proto  Local  Foreign  State  PID
        if len(parts) >= 5 and parts[-1].isdigit():
            local_addr = parts[1]
            if local_addr.endswith(needle):
                return parts[-1]
    return None


def _windows_taskkill_pid_tree(pid_string):
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", pid_string],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def kill_whisper_streaming_server_processes_on_windows():
    """Best-effort shutdown of the whisper_streaming server on Windows.
    Three layers, all idempotent:
      1. Kill whoever is LISTENING on SERVER_PORT (most reliable — direct).
      2. Kill the cmd window we spawned with title 'vtt-server' (gets the
         python child via /T).
      3. Kill any python processes whose command line still references our
         server scripts (catches stragglers; wmic-based, tolerant of failure).
    """
    port_listener_pid = find_pid_listening_on_tcp_port_on_windows(SERVER_PORT)
    if port_listener_pid:
        _windows_taskkill_pid_tree(port_listener_pid)

    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/FI", "WINDOWTITLE eq vtt-server*"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    for process_id_string in find_whisper_streaming_server_process_ids_on_windows():
        _windows_taskkill_pid_tree(process_id_string)


def wait_for_tcp_port_free(host, port, timeout_seconds=5.0):
    """Block until nothing is listening on host:port (i.e. a fresh bind
    will succeed), or timeout. Returns True if port is free."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect((host, port))
            probe.close()
            time.sleep(0.15)
            continue
        except (ConnectionRefusedError, socket.timeout, OSError):
            probe.close()
            return True
    return False


def parse_transcript_line(raw_line_text):
    """
    whisper_streaming emits "<begin_ms> <end_ms> <text>" per line.
    Return just <text>, or None if the line is empty / malformed.
    """
    stripped = raw_line_text.strip()
    if not stripped:
        return None
    parts = stripped.split(" ", 2)
    if len(parts) < 3:
        # Some server messages may not match; show them raw rather than drop.
        return stripped
    _begin_ms, _end_ms, text = parts
    return text


def open_folder_in_native_file_manager(folder_path):
    system_name = platform.system()
    try:
        if system_name == "Linux":
            subprocess.Popen(["xdg-open", str(folder_path)])
        elif system_name == "Darwin":
            subprocess.Popen(["open", str(folder_path)])
        elif system_name == "Windows":
            subprocess.Popen(["explorer", str(folder_path)])
    except Exception as error:
        messagebox.showerror(
            "Open folder failed", f"Could not open {folder_path}: {error}"
        )


class ModeRunner:
    """
    Runs an ffmpeg subprocess piping raw PCM into the whisper_streaming TCP
    server, reads transcript lines back, and dispatches them via the supplied
    callback. Owns its own thread.
    """

    def __init__(
        self,
        mode_label,
        ffmpeg_command_argv,
        on_transcript_text,
        on_finished,
        save_to_file_path_or_none,
        type_into_focused_window,
    ):
        self.mode_label = mode_label
        self.ffmpeg_command_argv = ffmpeg_command_argv
        self.on_transcript_text = on_transcript_text
        self.on_finished = on_finished
        self.save_to_file_path_or_none = save_to_file_path_or_none
        self.type_into_focused_window = type_into_focused_window

        self._stop_requested = threading.Event()
        self._ffmpeg_process_or_none = None
        self._socket_or_none = None
        self._save_file_handle_or_none = None
        self._keyboard_controller_or_none = (
            KeyboardController() if (type_into_focused_window and KeyboardController) else None
        )
        self._pump_thread = threading.Thread(
            target=self._run, name=f"vtt-mode-{mode_label}", daemon=True
        )

    def start(self):
        self._pump_thread.start()

    def stop(self):
        self._stop_requested.set()
        # Kill ffmpeg first; closing socket will unblock recv.
        if self._ffmpeg_process_or_none is not None:
            try:
                self._ffmpeg_process_or_none.terminate()
            except Exception:
                pass
        if self._socket_or_none is not None:
            try:
                self._socket_or_none.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._socket_or_none.close()
            except Exception:
                pass

    def _run(self):
        try:
            self._socket_or_none = socket.create_connection(
                (SERVER_HOST, SERVER_PORT), timeout=5.0
            )
            self._socket_or_none.settimeout(None)

            self._ffmpeg_process_or_none = subprocess.Popen(
                self.ffmpeg_command_argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )

            if self.save_to_file_path_or_none is not None:
                self._save_file_handle_or_none = open(
                    self.save_to_file_path_or_none, "a", encoding="utf-8"
                )

            sender_thread = threading.Thread(
                target=self._pump_audio_to_server,
                name=f"vtt-audio-pump-{self.mode_label}",
                daemon=True,
            )
            sender_thread.start()

            self._read_transcript_lines_from_server()
        except Exception as error:
            self.on_transcript_text(f"\n[error] {error}\n")
        finally:
            self._cleanup()
            self.on_finished(self.mode_label)

    def _pump_audio_to_server(self):
        try:
            assert self._ffmpeg_process_or_none is not None
            assert self._socket_or_none is not None
            stdout = self._ffmpeg_process_or_none.stdout
            while not self._stop_requested.is_set():
                chunk = stdout.read(4096)
                if not chunk:
                    break
                try:
                    self._socket_or_none.sendall(chunk)
                except OSError:
                    break
        except Exception:
            pass
        finally:
            # Half-close so the server flushes remaining transcript.
            if self._socket_or_none is not None:
                try:
                    self._socket_or_none.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

    def _read_transcript_lines_from_server(self):
        assert self._socket_or_none is not None
        line_buffer = b""
        while not self._stop_requested.is_set():
            try:
                data = self._socket_or_none.recv(4096)
            except OSError:
                break
            if not data:
                break
            line_buffer += data
            while b"\n" in line_buffer:
                raw_line, line_buffer = line_buffer.split(b"\n", 1)
                try:
                    decoded = raw_line.decode("utf-8", errors="replace")
                except Exception:
                    continue
                text_or_none = parse_transcript_line(decoded)
                if text_or_none is None:
                    continue
                self._dispatch_transcript_text(text_or_none)

    def _dispatch_transcript_text(self, text):
        # whisper_streaming already emits the right leading whitespace per
        # segment (a leading space before words, none before punctuation).
        # Pass it through as-is and don't add our own — adding a trailing
        # space here was producing doubles when the next segment also led
        # with a space, and adding one before punctuation produced "Hello ."
        if not text:
            return
        # UI update — suppressed in typing mode, otherwise the keyboard
        # typer (which targets the focused window, often the GUI itself)
        # would write the same text a second time, producing duplicates.
        if not self.type_into_focused_window:
            try:
                self.on_transcript_text(text)
            except Exception:
                pass
        # File
        if self._save_file_handle_or_none is not None:
            try:
                self._save_file_handle_or_none.write(text)
                self._save_file_handle_or_none.flush()
            except Exception:
                pass
        # Typing — same: trust whisper_streaming's spacing.
        if self._keyboard_controller_or_none is not None:
            try:
                self._keyboard_controller_or_none.type(text)
            except Exception:
                pass

    def _cleanup(self):
        if self._ffmpeg_process_or_none is not None:
            try:
                self._ffmpeg_process_or_none.terminate()
                try:
                    self._ffmpeg_process_or_none.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._ffmpeg_process_or_none.kill()
            except Exception:
                pass
        if self._socket_or_none is not None:
            try:
                self._socket_or_none.close()
            except Exception:
                pass
        if self._save_file_handle_or_none is not None:
            try:
                self._save_file_handle_or_none.close()
            except Exception:
                pass


class VttGuiApplication:
    def __init__(self):
        self.tk_root = tk.Tk()
        self.tk_root.title("Voice-to-Text-Type-Tally (vtttt)")
        self.tk_root.geometry("940x672")

        self.server_subprocess_or_none = None
        self.active_mode_runner_or_none = None
        self.active_mode_label_or_none = None
        self.runner_state_lock = threading.Lock()

        self._loopback_available = False
        try:
            self._loopback_available = audio_sources.is_system_audio_loopback_available()
        except Exception:
            self._loopback_available = False

        self._build_widgets()
        self.tk_root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        TRANSCRIPTS_DIRECTORY.mkdir(parents=True, exist_ok=True)

        # Auto-pick the compute device at first launch: GPU if available,
        # else CPU. The user can switch later via the Start server (GPU/CPU)
        # buttons.
        os.environ["WHISPER_DEVICE"] = (
            "cuda" if is_nvidia_gpu_available_for_whisper() else "cpu"
        )
        self._start_server_async()
        # Kick off the periodic server-health poll (updates the bottom-row
        # indicator label every 2s). Schedule via after so it runs on the
        # Tk main thread once the mainloop is up.
        self.tk_root.after(500, self._poll_server_health_loop)

    # ---- UI ---------------------------------------------------------------

    def _build_widgets(self):
        # Status text variable — the actual Label widget is created later
        # (inside the status_and_model_row_frame so it shares a row with
        # the model dropdown).
        self.status_var = tk.StringVar(value="Server: starting...   Mode: idle")

        button_frame = tk.Frame(self.tk_root)
        button_frame.pack(side=tk.TOP, fill=tk.X, padx=6, pady=6)

        self.button_mic_preview = tk.Button(
            button_frame,
            text="Mic — show in window only",
            command=lambda: self._on_mode_button_clicked(MODE_MIC_PREVIEW),
        )
        self.button_mic_typing = tk.Button(
            button_frame,
            text="Mic — type into focused window",
            command=lambda: self._on_mode_button_clicked(MODE_MIC_TYPING),
        )
        self.button_mic_to_file = tk.Button(
            button_frame,
            text="Mic — save to file",
            command=lambda: self._on_mode_button_clicked(MODE_MIC_TO_FILE),
        )
        self.button_system_to_file = tk.Button(
            button_frame,
            text="System audio — save to file",
            command=lambda: self._on_mode_button_clicked(MODE_SYSTEM_TO_FILE),
        )
        self.button_mixed_to_file = tk.Button(
            button_frame,
            text="Mic + System mixed — save to file",
            command=lambda: self._on_mode_button_clicked(MODE_MIXED_TO_FILE),
        )
        self.button_stop = tk.Button(
            button_frame,
            text="Stop",
            command=self._on_stop_button_clicked,
            state=tk.DISABLED,
        )

        # Lay buttons out in TWO ROWS via grid so they stay visible when the
        # window is narrow (a single horizontal row gets clipped on shrink).
        # Row 1: mic-related actions. Row 2: system-audio + stop.
        button_grid_padding = {"padx": 3, "pady": 3, "sticky": "ew"}
        self.button_mic_preview.grid(row=0, column=0, **button_grid_padding)
        self.button_mic_typing.grid(row=0, column=1, **button_grid_padding)
        self.button_mic_to_file.grid(row=0, column=2, **button_grid_padding)
        self.button_system_to_file.grid(row=1, column=0, **button_grid_padding)
        self.button_mixed_to_file.grid(row=1, column=1, **button_grid_padding)
        self.button_stop.grid(row=1, column=2, **button_grid_padding)
        # Make the three columns share width equally so buttons grow/shrink
        # with the window instead of clipping.
        for column_index in range(3):
            button_frame.grid_columnconfigure(column_index, weight=1)

        # Map mode label -> button widget so we can highlight the active
        # mode and unhighlight others when modes change.
        self.mode_label_to_button_widget = {
            MODE_MIC_PREVIEW: self.button_mic_preview,
            MODE_MIC_TYPING: self.button_mic_typing,
            MODE_MIC_TO_FILE: self.button_mic_to_file,
            MODE_SYSTEM_TO_FILE: self.button_system_to_file,
            MODE_MIXED_TO_FILE: self.button_mixed_to_file,
        }
        # Capture each mode button's default visual state so we can restore
        # it cleanly when the mode is no longer active.
        self.mode_button_default_visual_state_by_widget = {
            button_widget: {
                "relief": button_widget.cget("relief"),
                "bd": button_widget.cget("bd"),
                "background": button_widget.cget("background"),
                "foreground": button_widget.cget("foreground"),
            }
            for button_widget in self.mode_label_to_button_widget.values()
        }

        if not self._loopback_available:
            self.button_system_to_file.config(state=tk.DISABLED)
            self.button_mixed_to_file.config(state=tk.DISABLED)

        # Disable mode buttons until server ready.
        self._set_mode_buttons_enabled(False)

        # Small system-log widget (a few lines, light grey, read-only).
        # Receives [server] / [mode] / error messages — not transcript text.
        self.log_text_widget = scrolledtext.ScrolledText(
            self.tk_root,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("TkDefaultFont", 9),
            height=5,
            background="#eeeeee",
            foreground="#444444",
        )
        self.log_text_widget.pack(
            side=tk.TOP, fill=tk.X, padx=6, pady=(0, 4)
        )

        self._gpu_is_available = is_nvidia_gpu_available_for_whisper()
        # Server start/stop buttons are constructed BELOW after the
        # transcript_controls_frame exists (their parent).

        # ---- Row: Model dropdown (alone) -------------------------------
        model_row_frame = tk.Frame(self.tk_root)
        model_row_frame.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(0, 4))

        from tkinter import ttk as _tk_ttk_module
        locally_available_models = list_locally_available_whisper_model_names()
        if not locally_available_models:
            locally_available_models = [DEFAULT_WHISPER_MODEL_NAME]

        # Per-model human description strings shown inside the dropdown.
        # Models on disk are listed first; not-installed models are shown
        # afterward with a "(not installed)" suffix and won't actually load
        # if selected (we revert + log a hint about installing them).
        whisper_model_description_by_name = {
            "tiny":      "tiny      — ~75 MB · multilingual · fastest, lowest accuracy",
            "tiny.en":   "tiny.en   — ~75 MB · English-only · slightly more accurate than tiny for English",
            "base":      "base      — ~145 MB · multilingual · fast, decent accuracy",
            "base.en":   "base.en   — ~145 MB · English-only · slightly more accurate than base for English",
            "small":     "small     — ~485 MB · multilingual · good accuracy, sweet spot for many users",
            "small.en":  "small.en  — ~485 MB · English-only · slightly more accurate than small for English",
            "medium":    "medium    — ~1.5 GB · multilingual · great accuracy, slower",
            "medium.en": "medium.en — ~1.5 GB · English-only · great accuracy for English",
            "large-v1":  "large-v1  — ~3.0 GB · multilingual · older large variant",
            "large-v2":  "large-v2  — ~3.0 GB · multilingual · stronger large variant",
            "large-v3":  "large-v3  — ~3.0 GB · multilingual · best general accuracy",
            "large":     "large     — ~3.0 GB · multilingual · alias for the latest large model",
        }

        # Track which models are actually present on disk so the
        # selection-changed handler can refuse and revert when the user
        # picks a not-installed entry.
        self.locally_available_whisper_model_names_set = set(
            locally_available_models
        )

        # Build display strings for ALL supported models. Visual marker:
        #   ●  = installed locally (full / "darker" weight)
        #   ○  = not on disk     (hollow / "lighter" weight)
        # ttk.Combobox doesn't support per-row color theming portably, so
        # we use the filled-vs-hollow circle prefix as the visual cue.
        # Installed entries are listed FIRST so they appear at the top.
        self.whisper_model_dropdown_display_to_name_map = {}
        whisper_model_dropdown_display_strings = []

        # Installed first.
        for model_name in locally_available_models:
            description = whisper_model_description_by_name.get(
                model_name, f"{model_name}    — local model"
            )
            display_string = f"●  {description}"
            whisper_model_dropdown_display_strings.append(display_string)
            self.whisper_model_dropdown_display_to_name_map[display_string] = (
                model_name
            )

        # Then known-but-not-installed.
        for model_name in whisper_model_description_by_name:
            if model_name in self.locally_available_whisper_model_names_set:
                continue
            description = whisper_model_description_by_name[model_name]
            display_string = f"○  {description}"
            whisper_model_dropdown_display_strings.append(display_string)
            self.whisper_model_dropdown_display_to_name_map[display_string] = (
                model_name
            )

        initial_model_choice = (
            DEFAULT_WHISPER_MODEL_NAME
            if DEFAULT_WHISPER_MODEL_NAME in locally_available_models
            else locally_available_models[0]
        )
        # Find the display string for the initial choice.
        initial_display_string = next(
            (
                display
                for display, name in (
                    self.whisper_model_dropdown_display_to_name_map.items()
                )
                if name == initial_model_choice
            ),
            whisper_model_dropdown_display_strings[0],
        )
        self.selected_whisper_model_dropdown_display_var = tk.StringVar(
            value=initial_display_string
        )
        os.environ["WHISPER_MODEL"] = initial_model_choice
        # Pick a width wide enough that the longest description doesn't
        # clip; tk.Combobox width is in characters.
        widest_display_length = max(
            len(string) for string in whisper_model_dropdown_display_strings
        )
        self.whisper_model_dropdown = _tk_ttk_module.Combobox(
            model_row_frame,
            textvariable=self.selected_whisper_model_dropdown_display_var,
            values=whisper_model_dropdown_display_strings,
            state="readonly",
        )
        # Label on the left, dropdown fills the rest of the row.
        tk.Label(model_row_frame, text="Model: ").pack(side=tk.LEFT)
        self.whisper_model_dropdown.pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        self.whisper_model_dropdown.bind(
            "<<ComboboxSelected>>",
            lambda event: self._on_whisper_model_selection_changed(),
        )

        # ---- Row: Server controls (GPU/CPU/Stop + GPU index + status) --
        server_controls_row_frame = tk.Frame(self.tk_root)
        server_controls_row_frame.pack(
            side=tk.TOP, fill=tk.X, padx=6, pady=(0, 4)
        )
        self.button_start_server_gpu = tk.Button(
            server_controls_row_frame,
            text="Start server (GPU)",
            command=lambda: self._on_start_server_with_device_clicked("cuda"),
            state=tk.NORMAL if self._gpu_is_available else tk.DISABLED,
        )
        self.button_start_server_gpu.pack(side=tk.LEFT, padx=2)
        self.button_start_server_cpu = tk.Button(
            server_controls_row_frame,
            text="Start server (CPU)",
            command=lambda: self._on_start_server_with_device_clicked("cpu"),
        )
        self.button_start_server_cpu.pack(side=tk.LEFT, padx=2)
        self.button_stop_server = tk.Button(
            server_controls_row_frame,
            text="Stop server",
            command=self._on_stop_server_button_clicked,
        )
        self.button_stop_server.pack(side=tk.LEFT, padx=(2, 12))

        # GPU index dropdown — populated from `nvidia-smi -L`. Disabled if
        # no NVIDIA driver. Selecting an index sets CUDA_VISIBLE_DEVICES
        # for the next server launch.
        self.available_gpu_indices_with_names = (
            list_available_nvidia_gpu_indices_with_names()
        )
        gpu_index_dropdown_values = [
            f"{idx}: {name}"
            for idx, name in self.available_gpu_indices_with_names
        ] or ["(no GPU)"]
        initial_gpu_index_string = (
            os.environ.get("CUDA_VISIBLE_DEVICES")
            or (self.available_gpu_indices_with_names[0][0]
                if self.available_gpu_indices_with_names else "")
        )
        initial_gpu_index_display = next(
            (
                display_string
                for display_string in gpu_index_dropdown_values
                if display_string.startswith(f"{initial_gpu_index_string}:")
            ),
            gpu_index_dropdown_values[0],
        )
        self.selected_gpu_index_display_var = tk.StringVar(
            value=initial_gpu_index_display
        )
        if self.available_gpu_indices_with_names:
            os.environ["CUDA_VISIBLE_DEVICES"] = (
                self.available_gpu_indices_with_names[0][0]
                if not os.environ.get("CUDA_VISIBLE_DEVICES")
                else os.environ["CUDA_VISIBLE_DEVICES"]
            )
        tk.Label(server_controls_row_frame, text=" GPU index: ").pack(
            side=tk.LEFT
        )
        self.gpu_index_dropdown = _tk_ttk_module.Combobox(
            server_controls_row_frame,
            textvariable=self.selected_gpu_index_display_var,
            values=gpu_index_dropdown_values,
            state="readonly" if self.available_gpu_indices_with_names else tk.DISABLED,
            width=max(
                (len(string) for string in gpu_index_dropdown_values),
                default=10,
            ),
        )
        self.gpu_index_dropdown.pack(side=tk.LEFT, padx=(0, 12))
        self.gpu_index_dropdown.bind(
            "<<ComboboxSelected>>",
            lambda event: self._on_gpu_index_selection_changed(),
        )

        # Status bar at the right end of this row.
        self.status_bar_widget = tk.Label(
            server_controls_row_frame,
            textvariable=self.status_var,
            anchor="w",
            relief=tk.SUNKEN,
            bd=1,
            padx=6,
            pady=3,
        )
        self.status_bar_widget.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ---- Bottom controls row: server start/stop on the left, then
        # transcript-related buttons on the right side of the same row.
        # ---- Row: Transcript controls (Open / Clear / Copy / Help) ----
        transcript_controls_frame = tk.Frame(self.tk_root)
        transcript_controls_frame.pack(
            side=tk.TOP, fill=tk.X, padx=6, pady=(0, 6)
        )

        # Capture default visual state for GPU / CPU buttons so we can
        # restore them after un-highlighting.
        self.device_button_default_visual_state_by_widget = {
            button_widget: {
                "relief": button_widget.cget("relief"),
                "bd": button_widget.cget("bd"),
                "background": button_widget.cget("background"),
                "foreground": button_widget.cget("foreground"),
            }
            for button_widget in (
                self.button_start_server_gpu,
                self.button_start_server_cpu,
            )
        }

        # Right-justify the row: pack with side=RIGHT in REVERSE visual
        # order so the on-screen left-to-right order is
        # Open | Clear | Copy all | Help, all anchored to the right edge.
        tk.Button(
            transcript_controls_frame,
            text="Help",
            command=self._on_help_button_clicked,
        ).pack(side=tk.RIGHT, padx=2)
        tk.Button(
            transcript_controls_frame,
            text="Copy all",
            command=self._on_copy_all_transcript_button_clicked,
        ).pack(side=tk.RIGHT, padx=2)
        tk.Button(
            transcript_controls_frame,
            text="Clear",
            command=self._on_clear_transcript_button_clicked,
        ).pack(side=tk.RIGHT, padx=2)
        tk.Button(
            transcript_controls_frame,
            text="Open transcripts folder",
            command=lambda: open_folder_in_native_file_manager(TRANSCRIPTS_DIRECTORY),
        ).pack(side=tk.RIGHT, padx=(6, 2))
        # No "Quit" button — the window's X close button already triggers
        # _on_window_close via the WM_DELETE_WINDOW protocol binding.

        # ---- Main transcript widget (expands to fill the rest) ---------
        self.transcript_text = scrolledtext.ScrolledText(
            self.tk_root,
            wrap=tk.WORD,
            state=tk.NORMAL,
            font=("TkDefaultFont", 11),
            background="#ffffff",
        )
        self.transcript_text.pack(
            side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=(0, 6)
        )

    def _set_mode_buttons_enabled(self, is_enabled):
        normal_or_disabled = tk.NORMAL if is_enabled else tk.DISABLED
        self.button_mic_preview.config(state=normal_or_disabled)
        self.button_mic_typing.config(state=normal_or_disabled)
        self.button_mic_to_file.config(state=normal_or_disabled)
        if self._loopback_available:
            self.button_system_to_file.config(state=normal_or_disabled)
            self.button_mixed_to_file.config(state=normal_or_disabled)

    def _set_status(self, server_segment, mode_segment):
        self.status_var.set(f"Server: {server_segment}   Mode: {mode_segment}")

    def _append_transcript_text_threadsafe(self, text):
        # Marshal onto Tk main thread.
        self.tk_root.after(0, self._append_transcript_text, text)

    def _append_transcript_text(self, text):
        # Transcript widget is editable so the user can select/copy/edit.
        # Inserts go at the end regardless of cursor position so user edits
        # don't disrupt incoming text.
        self.transcript_text.insert(tk.END, text)
        self.transcript_text.see(tk.END)

    def _append_log_text(self, text):
        """Write a system/log message (server status, mode events, errors)
        into the small log widget at the top — NOT the transcript widget."""
        self.log_text_widget.config(state=tk.NORMAL)
        self.log_text_widget.insert(tk.END, text)
        self.log_text_widget.see(tk.END)
        self.log_text_widget.config(state=tk.DISABLED)

    def _set_active_device_button_highlight(self, active_device_or_none):
        """Highlight the GPU or CPU server button based on which device the
        running server is using. `active_device_or_none` is "cuda", "cpu",
        or None (no server running)."""
        device_to_button = {
            "cuda": self.button_start_server_gpu,
            "cpu": self.button_start_server_cpu,
        }
        for device_name, button_widget in device_to_button.items():
            default_visual_state = (
                self.device_button_default_visual_state_by_widget[button_widget]
            )
            if device_name == active_device_or_none:
                button_widget.config(
                    relief=tk.SUNKEN,
                    bd=3,
                    background="#a5d6a7",  # soft green for "currently running"
                    foreground="#000000",
                )
            else:
                button_widget.config(
                    relief=default_visual_state["relief"],
                    bd=default_visual_state["bd"],
                    background=default_visual_state["background"],
                    foreground=default_visual_state["foreground"],
                )

    def _set_active_mode_button_highlight(self, active_mode_label_or_none):
        """Visually highlight the button corresponding to the active mode
        and unhighlight the others. Call with None when no mode is active."""
        for mode_label, button_widget in self.mode_label_to_button_widget.items():
            default_visual_state = (
                self.mode_button_default_visual_state_by_widget[button_widget]
            )
            if mode_label == active_mode_label_or_none:
                button_widget.config(
                    relief=tk.SUNKEN,
                    bd=3,
                    background="#ffe082",  # warm amber for "currently running"
                    foreground="#000000",
                )
            else:
                button_widget.config(
                    relief=default_visual_state["relief"],
                    bd=default_visual_state["bd"],
                    background=default_visual_state["background"],
                    foreground=default_visual_state["foreground"],
                )

    def _on_whisper_model_selection_changed(self):
        """Stop the running server (if any) and restart with the new model
        name in the WHISPER_MODEL env var. If the user picked a model
        that isn't on disk, log a hint and revert the dropdown to the
        currently-running model."""
        selected_display_string = (
            self.selected_whisper_model_dropdown_display_var.get()
        )
        new_model_name = self.whisper_model_dropdown_display_to_name_map.get(
            selected_display_string, selected_display_string
        )

        if new_model_name not in self.locally_available_whisper_model_names_set:
            # Refuse the change; revert dropdown selection to the current
            # model and log a friendly hint.
            current_model_name = os.environ.get(
                "WHISPER_MODEL", DEFAULT_WHISPER_MODEL_NAME
            )
            current_display_string = next(
                (
                    display
                    for display, name in (
                        self.whisper_model_dropdown_display_to_name_map.items()
                    )
                    if name == current_model_name
                    and "(not installed)" not in display
                ),
                None,
            )
            if current_display_string is not None:
                self.selected_whisper_model_dropdown_display_var.set(
                    current_display_string
                )
            self._append_log_text(
                f"[model] '{new_model_name}' is not present locally — "
                f"read Help for download instructions.\n"
                f"        Currently using: {current_model_name}\n"
            )
            return
        os.environ["WHISPER_MODEL"] = new_model_name
        self._append_log_text(
            f"[model] switching to '{new_model_name}' — restarting server...\n"
        )
        # Reuse the stop+start machinery.
        self._on_stop_server_button_clicked()
        # Brief delay so pkill clears the listening socket before relaunch.
        self.tk_root.after(700, self._start_server_async)

    def _on_help_button_clicked(self):
        help_window = tk.Toplevel(self.tk_root)
        help_window.title("Voice-to-Text-Type-Tally — Help")
        help_window.geometry("720x540")
        help_text_widget = scrolledtext.ScrolledText(
            help_window,
            wrap=tk.WORD,
            font=("TkDefaultFont", 10),
        )
        help_text_widget.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        try:
            help_document_contents = HELP_DOCUMENT_PATH.read_text(encoding="utf-8")
        except Exception as read_error:
            help_document_contents = (
                f"Could not read {HELP_DOCUMENT_PATH}: {read_error}\n"
            )
        help_text_widget.insert(tk.END, help_document_contents)
        help_text_widget.config(state=tk.DISABLED)
        tk.Button(
            help_window, text="Close", command=help_window.destroy
        ).pack(side=tk.BOTTOM, pady=(0, 8))

    def _on_clear_transcript_button_clicked(self):
        self.transcript_text.delete("1.0", tk.END)

    def _on_copy_all_transcript_button_clicked(self):
        full_text = self.transcript_text.get("1.0", tk.END)
        try:
            self.tk_root.clipboard_clear()
            self.tk_root.clipboard_append(full_text)
            # Force tk to actually push the text into the X clipboard.
            self.tk_root.update_idletasks()
            self._append_log_text("[ui] transcript copied to clipboard.\n")
        except Exception as clipboard_error:
            self._append_log_text(
                f"[ui] copy failed: {clipboard_error}\n"
            )

    # ---- Server lifecycle -------------------------------------------------

    def _build_server_command_argv(self):
        """Cross-platform: build argv to invoke our wrapper that starts the
        whisper_streaming server with the user's WHISPER_DEVICE / WHISPER_MODEL
        env-driven choices. Returns the argv list (caller wraps in a
        terminal-spawning command for visibility)."""
        wrapper_script_path = os.path.join(
            SCRIPT_DIRECTORY,
            "whisper_streaming_server_runner_with_device_choice.py",
        )
        whisper_model_name = os.environ.get(
            "WHISPER_MODEL", DEFAULT_WHISPER_MODEL_NAME
        )
        local_model_directory = (
            LOCAL_MODELS_PARENT_DIRECTORY / whisper_model_name
        )
        if (local_model_directory / "model.bin").is_file():
            # Pass BOTH --model and --model_dir: --model_dir is used for the
            # actual load path; --model is purely for log readability so
            # the server's "Loading Whisper <name>" message reflects the
            # real choice instead of the argparse default ("large-v2").
            model_args = [
                "--model", whisper_model_name,
                "--model_dir", str(local_model_directory),
            ]
        else:
            model_args = ["--model", whisper_model_name]
        return [
            sys.executable,
            wrapper_script_path,
            "--host", SERVER_HOST,
            "--port", str(SERVER_PORT),
            "--backend", "faster-whisper",
            *model_args,
            "--lan", "en",
            "--min-chunk-size", "0.5",
            "--buffer_trimming", "segment",
            "--buffer_trimming_sec", "8",
            "--vad",
            "-l", "INFO",
        ]

    def _spawn_server_process_in_visible_window(self, server_argv):
        """Open a platform-appropriate visible terminal window running the
        server. Returns the Popen handle of the WRAPPER process (which may
        itself be a terminal launcher; the actual python child detaches)."""
        system_name = platform.system()
        popen_kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if hasattr(os, "setsid"):
            popen_kwargs["preexec_fn"] = os.setsid

        if system_name == "Linux":
            if shutil.which("gnome-terminal"):
                # Use bash -lc with a single string so the working dir is
                # the repo root. Trail with `; exec bash` so the terminal
                # stays open if the server crashes — otherwise the window
                # closes before the user can read any traceback.
                command_string = " ".join(
                    self._shell_quote(part) for part in server_argv
                )
                command_string += "; echo; echo '[vtt-server] process exited.'; exec bash"
                return subprocess.Popen(
                    [
                        "gnome-terminal",
                        "--title=vtt-server",
                        "--working-directory", SCRIPT_DIRECTORY,
                        "--",
                        "bash", "-lc", command_string,
                    ],
                    **popen_kwargs,
                )
            return subprocess.Popen(
                server_argv, cwd=SCRIPT_DIRECTORY, **popen_kwargs
            )

        if system_name == "Darwin":
            # macOS: open Terminal.app via osascript and run our command.
            command_string = (
                f"cd {self._shell_quote(SCRIPT_DIRECTORY)} && "
                + " ".join(self._shell_quote(part) for part in server_argv)
            )
            applescript_command = (
                f'tell application "Terminal" to do script "{command_string}"'
            )
            return subprocess.Popen(
                ["osascript", "-e", applescript_command],
                **popen_kwargs,
            )

        if system_name == "Windows":
            # Windows: spawn a new console window via `start`.
            quoted = " ".join(
                f'"{part}"' if " " in part else part for part in server_argv
            )
            command_string = f'start "vtt-server" cmd /K {quoted}'
            return subprocess.Popen(
                command_string, cwd=SCRIPT_DIRECTORY, shell=True
            )

        # Unknown OS → silent background.
        return subprocess.Popen(
            server_argv, cwd=SCRIPT_DIRECTORY, **popen_kwargs
        )

    @staticmethod
    def _shell_quote(text):
        """Minimal POSIX shell-quoting for embedding argv parts in a
        single bash -lc string."""
        if all(c.isalnum() or c in "/._-=:," for c in text):
            return text
        return "'" + text.replace("'", r"'\''") + "'"

    def _start_server_async(self):
        try:
            server_argv = self._build_server_command_argv()
            self.server_subprocess_or_none = (
                self._spawn_server_process_in_visible_window(server_argv)
            )
            self._set_status("starting...", "idle")
            self._append_log_text(
                f"[server] launched ({platform.system()}, "
                f"device={os.environ.get('WHISPER_DEVICE','cuda')}, "
                f"model={os.environ.get('WHISPER_MODEL', DEFAULT_WHISPER_MODEL_NAME)}).\n"
            )
            # On Linux/Mac, the new terminal window grabs focus; raise the
            # GUI back to the front shortly after.
            def raise_gui_window_to_front():
                try:
                    self.tk_root.lift()
                    self.tk_root.attributes("-topmost", True)
                    self.tk_root.after(
                        300,
                        lambda: self.tk_root.attributes("-topmost", False),
                    )
                    self.tk_root.focus_force()
                except Exception:
                    pass
            self.tk_root.after(600, raise_gui_window_to_front)
            self.tk_root.after(1500, raise_gui_window_to_front)
        except Exception as error:
            self._set_status(f"failed to launch ({error})", "idle")
            self._append_log_text(f"[server] launch error: {error}\n")

        threading.Thread(
            target=self._await_server_ready_then_enable_ui,
            name="vtt-server-probe",
            daemon=True,
        ).start()

    def _await_server_ready_then_enable_ui(self):
        deadline = time.time() + SERVER_READY_TIMEOUT_SECONDS
        while time.time() < deadline:
            if is_server_reachable():
                self.tk_root.after(0, self._on_server_ready)
                return
            time.sleep(SERVER_READY_POLL_INTERVAL_SECONDS)
        # Keep polling beyond deadline indefinitely but don't hang on failure.
        # (User can still try buttons; we'll show error if server not up.)
        self.tk_root.after(0, lambda: self._set_status(
            "not reachable (still trying)", "idle"
        ))
        # Continue polling forever in case user starts it manually.
        while True:
            if is_server_reachable():
                self.tk_root.after(0, self._on_server_ready)
                return
            time.sleep(SERVER_READY_POLL_INTERVAL_SECONDS)

    def _on_server_ready(self):
        self._set_status("ready", "idle")
        self._set_mode_buttons_enabled(True)
        self._set_active_device_button_highlight(
            os.environ.get("WHISPER_DEVICE", "cuda")
        )

    # ---- Server start/stop buttons + health indicator ---------------------

    def _on_start_server_button_clicked(self):
        if is_server_reachable():
            self._append_log_text(
                "[server] already reachable — start request ignored.\n"
            )
            return
        self._append_log_text("[server] starting...\n")
        self._start_server_async()

    def _on_gpu_index_selection_changed(self):
        """User picked a GPU from the GPU-index dropdown. Set the
        CUDA_VISIBLE_DEVICES env var so the next server launch targets
        that GPU. (Doesn't auto-restart — user clicks 'Start server (GPU)'
        to apply.)"""
        selected_display = self.selected_gpu_index_display_var.get()
        if ":" not in selected_display:
            return
        gpu_index_string = selected_display.split(":", 1)[0].strip()
        os.environ["CUDA_VISIBLE_DEVICES"] = gpu_index_string
        self._append_log_text(
            f"[gpu] CUDA_VISIBLE_DEVICES set to {gpu_index_string} — "
            f"click 'Start server (GPU)' to apply.\n"
        )

    def _on_start_server_with_device_clicked(self, device_name):
        """Stop any running server and (re)start it using the selected
        compute device ("cuda" or "cpu"). Sets WHISPER_DEVICE env var so
        the wrapper knows which path to take. Always tears down any
        existing server first (regardless of which device it was using)
        so the new launch can bind the port cleanly."""
        os.environ["WHISPER_DEVICE"] = device_name
        self._append_log_text(
            f"[server] requested start on {device_name.upper()}.\n"
        )
        # Always force-stop any running server first. is_server_reachable()
        # would miss a server that's still starting up (process exists,
        # socket not listening yet). The process-check is the safer gate.
        if (
            is_server_reachable()
            or is_whisper_streaming_server_process_running()
        ):
            self._on_stop_server_button_clicked()
            # Wait until the port is actually free before re-binding.
            # Fixed sleeps race on Windows (10048) when the previous
            # python child takes a moment to exit after taskkill.
            def _start_when_port_is_free():
                threading.Thread(
                    target=self._wait_for_port_then_start_server,
                    name="vtt-restart-wait",
                    daemon=True,
                ).start()
            self.tk_root.after(200, _start_when_port_is_free)
        else:
            self._start_server_async()

    def _wait_for_port_then_start_server(self):
        wait_for_tcp_port_free(SERVER_HOST, SERVER_PORT, timeout_seconds=8.0)
        self.tk_root.after(0, self._start_server_async)

    def _on_stop_server_button_clicked(self):
        # Use the same kill path as _on_window_close, but don't quit.
        self._append_log_text("[server] stopping...\n")
        try:
            if platform.system() == "Windows":
                kill_whisper_streaming_server_processes_on_windows()
            else:
                subprocess.run(
                    [
                        "pkill",
                        "-f",
                        "whisper_online_server.py|whisper_streaming_server_runner_with_device_choice.py",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if shutil.which("wmctrl"):
                subprocess.run(
                    ["wmctrl", "-c", "vtt-server"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception as stop_error:
            self._append_log_text(f"[server] stop error: {stop_error}\n")
        self._set_mode_buttons_enabled(False)
        self._set_status("stopped", "idle")
        self._set_active_device_button_highlight(None)

    def _poll_server_health_loop(self):
        """Run on the Tk main thread every 2s. Updates the status bar's
        Server: segment based on process-existence check. No TCP probe so
        we don't spam the server log with connect/close cycles."""
        # Preserve whatever Mode: segment is currently shown.
        current_status_text = self.status_var.get()
        current_mode_segment = "idle"
        if "Mode:" in current_status_text:
            current_mode_segment = current_status_text.split("Mode:", 1)[1].strip()
        if is_whisper_streaming_server_process_running():
            new_server_segment = "UP"
        else:
            new_server_segment = "DOWN"
        self._set_status(new_server_segment, current_mode_segment)
        # Re-schedule. Cancel happens implicitly when tk_root is destroyed.
        self.tk_root.after(2000, self._poll_server_health_loop)

    # ---- Mode switching ---------------------------------------------------

    def _on_mode_button_clicked(self, requested_mode_label):
        if not is_server_reachable():
            messagebox.showwarning(
                "Server not ready",
                "The whisper_streaming server is not reachable yet at "
                f"{SERVER_HOST}:{SERVER_PORT}. Please wait or start it manually.",
            )
            return

        if requested_mode_label in (MODE_SYSTEM_TO_FILE, MODE_MIXED_TO_FILE):
            try:
                if not audio_sources.is_system_audio_loopback_available():
                    messagebox.showinfo(
                        "System audio loopback unavailable",
                        audio_sources.get_human_readable_loopback_setup_instructions(),
                    )
                    return
            except Exception as error:
                messagebox.showerror("Loopback check failed", str(error))
                return

        with self.runner_state_lock:
            if self.active_mode_runner_or_none is not None:
                if self.active_mode_label_or_none == requested_mode_label:
                    return  # already running
                self._stop_active_runner_holding_lock()
            self._start_runner_holding_lock(requested_mode_label)

    def _start_runner_holding_lock(self, mode_label):
        audio_source_name = MODE_TO_AUDIO_SOURCE_NAME[mode_label]
        try:
            ffmpeg_command_argv = audio_sources.build_ffmpeg_command_for_audio_mode(
                audio_source_name
            )
        except getattr(audio_sources, "SystemAudioLoopbackUnavailableError", Exception) as error:
            messagebox.showerror("Audio source error", str(error))
            return
        except Exception as error:
            messagebox.showerror("ffmpeg command build failed", str(error))
            return

        save_path_or_none = None
        if mode_label in MODE_FILE_PREFIX:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path_or_none = TRANSCRIPTS_DIRECTORY / (
                f"{MODE_FILE_PREFIX[mode_label]}_{timestamp}.txt"
            )

        runner = ModeRunner(
            mode_label=mode_label,
            ffmpeg_command_argv=ffmpeg_command_argv,
            on_transcript_text=self._append_transcript_text_threadsafe,
            on_finished=self._on_runner_finished_threadsafe,
            save_to_file_path_or_none=save_path_or_none,
            type_into_focused_window=(mode_label == MODE_MIC_TYPING),
        )
        runner.start()
        self.active_mode_runner_or_none = runner
        self.active_mode_label_or_none = mode_label

        self._set_status("ready", MODE_HUMAN_LABEL[mode_label])
        self.button_stop.config(state=tk.NORMAL)
        self._set_active_mode_button_highlight(mode_label)
        self._append_log_text(
            f"[mode] started: {MODE_HUMAN_LABEL[mode_label]}"
            + (f"  -> {save_path_or_none}" if save_path_or_none else "")
            + "\n"
        )

    def _stop_active_runner_holding_lock(self):
        runner = self.active_mode_runner_or_none
        if runner is None:
            return
        runner.stop()
        # Don't join here — runner finishes asynchronously and calls back.
        self.active_mode_runner_or_none = None
        self.active_mode_label_or_none = None

    def _on_stop_button_clicked(self):
        with self.runner_state_lock:
            self._stop_active_runner_holding_lock()
        self._set_status("ready", "idle")
        self.button_stop.config(state=tk.DISABLED)
        # The async _on_runner_finished path won't clear the highlight in
        # this case because we just nulled active_mode_label_or_none above
        # — its equality check fails. Clear the highlight here directly.
        self._set_active_mode_button_highlight(None)

    def _on_runner_finished_threadsafe(self, finished_mode_label):
        self.tk_root.after(0, self._on_runner_finished, finished_mode_label)

    def _on_runner_finished(self, finished_mode_label):
        with self.runner_state_lock:
            # Only clear if this finishing runner is still the active one.
            if self.active_mode_label_or_none == finished_mode_label:
                self.active_mode_runner_or_none = None
                self.active_mode_label_or_none = None
                self._set_status("ready", "idle")
                self.button_stop.config(state=tk.DISABLED)
                self._set_active_mode_button_highlight(None)

    # ---- Shutdown ---------------------------------------------------------

    def _on_window_close(self):
        try:
            with self.runner_state_lock:
                self._stop_active_runner_holding_lock()
        except Exception:
            pass

        # Kill the whisper_streaming server. When we launched it inside
        # gnome-terminal, our Popen handle points at gnome-terminal which
        # has already detached from the actual python child — so we need
        # to find the server by name. pkill is the simplest portable path
        # on Linux/Mac. On Windows we use taskkill /IM via the python
        # executable name.
        try:
            if platform.system() == "Windows":
                kill_whisper_streaming_server_processes_on_windows()
            else:
                subprocess.run(
                    [
                        "pkill",
                        "-f",
                        "whisper_online_server.py|whisper_streaming_server_runner_with_device_choice.py",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            # Also close the gnome-terminal window if it's still up (Linux).
            if shutil.which("wmctrl"):
                subprocess.run(
                    ["wmctrl", "-c", "vtt-server"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

        # As a fallback, also try the original Popen-handle termination
        # in case we launched in headless mode (no gnome-terminal).
        if self.server_subprocess_or_none is not None:
            try:
                if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                    import signal as signal_module
                    try:
                        os.killpg(
                            os.getpgid(self.server_subprocess_or_none.pid),
                            signal_module.SIGTERM,
                        )
                    except (ProcessLookupError, PermissionError):
                        self.server_subprocess_or_none.terminate()
                else:
                    self.server_subprocess_or_none.terminate()
                try:
                    self.server_subprocess_or_none.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.server_subprocess_or_none.kill()
            except Exception:
                pass

        try:
            self.tk_root.destroy()
        except Exception:
            pass

    def run(self):
        self.tk_root.mainloop()


def main():
    app = VttGuiApplication()
    app.run()


if __name__ == "__main__":
    main()
