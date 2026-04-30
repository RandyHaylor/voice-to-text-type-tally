"""
Cross-platform system-audio (loopback) capture to a WAV file.

  Linux (PulseAudio/PipeWire): runs `ffmpeg -f pulse -i @DEFAULT_MONITOR@`,
                               which captures the default sink's monitor.
  Windows (WASAPI):            uses sounddevice + WasapiSettings(loopback=True)
                               on the default output device.
  macOS:                       not supported by the OS without a virtual
                               cable (BlackHole, Loopback, etc.).

Output: 16 kHz mono 16-bit PCM WAV.

Run:    python3 capture_system_audio_to_wav.py [--output OUT.wav]
Stop:   Ctrl+C
"""

import argparse
import datetime
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading


CAPTURE_SAMPLE_RATE_HZ = 16000
CAPTURE_CHANNEL_COUNT = 1


def capture_via_ffmpeg_pulse_on_linux(output_wav_path):
    if shutil.which("ffmpeg") is None:
        print("[error] ffmpeg not found. Install with: sudo apt install ffmpeg",
              flush=True)
        sys.exit(1)
    # Resolve the default sink's monitor source name. PulseAudio's source
    # for "what is being played on the default sink" is "<sink>.monitor".
    pulse_source_name = os.environ.get("PULSE_MONITOR_SOURCE_NAME")
    if not pulse_source_name:
        if shutil.which("pactl") is None:
            print("[error] pactl not found. Install with: sudo apt install pulseaudio-utils",
                  flush=True)
            sys.exit(1)
        try:
            default_sink_name = subprocess.check_output(
                ["pactl", "get-default-sink"], text=True
            ).strip()
        except subprocess.CalledProcessError as pactl_error:
            print(f"[error] pactl get-default-sink failed: {pactl_error}",
                  flush=True)
            sys.exit(1)
        if not default_sink_name:
            print("[error] pactl returned an empty default sink name.",
                  flush=True)
            sys.exit(1)
        pulse_source_name = f"{default_sink_name}.monitor"
    print(f"[device] Linux ffmpeg+pulse source: {pulse_source_name}", flush=True)
    print(f"[output] {output_wav_path}", flush=True)
    print(f"[format] {CAPTURE_SAMPLE_RATE_HZ} Hz mono int16 WAV", flush=True)
    print("[ctrl-c] stops capture and finalizes the file.", flush=True)

    ffmpeg_command_arguments = [
        "ffmpeg",
        "-loglevel", "warning",
        "-f", "pulse",
        "-i", pulse_source_name,
        "-ac", str(CAPTURE_CHANNEL_COUNT),
        "-ar", str(CAPTURE_SAMPLE_RATE_HZ),
        "-acodec", "pcm_s16le",
        "-y",
        output_wav_path,
    ]
    ffmpeg_subprocess = subprocess.Popen(
        ffmpeg_command_arguments,
        stdin=subprocess.PIPE,
    )

    def forward_stop_signal_to_ffmpeg(*unused):
        try:
            # Send 'q' to ffmpeg stdin for a clean stop with proper WAV trailer.
            if ffmpeg_subprocess.stdin and not ffmpeg_subprocess.stdin.closed:
                ffmpeg_subprocess.stdin.write(b"q")
                ffmpeg_subprocess.stdin.flush()
        except (BrokenPipeError, OSError):
            ffmpeg_subprocess.terminate()

    signal.signal(signal.SIGINT, forward_stop_signal_to_ffmpeg)
    signal.signal(signal.SIGTERM, forward_stop_signal_to_ffmpeg)

    print("[capture] started — play audio now.", flush=True)
    ffmpeg_exit_code = ffmpeg_subprocess.wait()
    if ffmpeg_exit_code != 0:
        print(f"[error] ffmpeg exited with {ffmpeg_exit_code}.", flush=True)
        sys.exit(ffmpeg_exit_code)
    print(f"\n[done] saved to {output_wav_path}", flush=True)


def capture_via_sounddevice_wasapi_loopback_on_windows(output_wav_path):
    import numpy as np
    import sounddevice as sound_device_module
    import soundfile as soundfile_module

    try:
        wasapi_loopback_settings = sound_device_module.WasapiSettings(loopback=True)
    except AttributeError:
        print("[error] sounddevice WasapiSettings unavailable. Update sounddevice.",
              flush=True)
        sys.exit(1)

    default_output_device_index = sound_device_module.default.device[1]
    device_info = sound_device_module.query_devices(default_output_device_index)
    print(f"[device] WASAPI loopback on: {device_info['name']}", flush=True)
    print(f"[output] {output_wav_path}", flush=True)
    print(f"[format] {CAPTURE_SAMPLE_RATE_HZ} Hz mono int16 WAV", flush=True)
    print("[ctrl-c] stops capture and finalizes the file.", flush=True)

    capture_should_stop_event = threading.Event()

    def handle_keyboard_interrupt_signal(*unused_args):
        capture_should_stop_event.set()

    signal.signal(signal.SIGINT, handle_keyboard_interrupt_signal)
    signal.signal(signal.SIGTERM, handle_keyboard_interrupt_signal)

    device_max_input_channels = device_info["max_input_channels"] or 2

    with soundfile_module.SoundFile(
        output_wav_path,
        mode="w",
        samplerate=CAPTURE_SAMPLE_RATE_HZ,
        channels=CAPTURE_CHANNEL_COUNT,
        subtype="PCM_16",
    ) as output_wav_writer:

        def audio_input_callback(indata, frames, time_info, status):
            if status:
                print(f"[audio] {status}", flush=True)
            mixed_to_mono = (
                indata.mean(axis=1, keepdims=True)
                if indata.shape[1] > CAPTURE_CHANNEL_COUNT
                else indata
            )
            mixed_to_mono_int16 = np.clip(
                mixed_to_mono * 32767.0, -32768, 32767
            ).astype(np.int16)
            output_wav_writer.write(mixed_to_mono_int16)

        with sound_device_module.InputStream(
            samplerate=CAPTURE_SAMPLE_RATE_HZ,
            channels=device_max_input_channels,
            dtype="float32",
            device=default_output_device_index,
            callback=audio_input_callback,
            extra_settings=wasapi_loopback_settings,
        ):
            print("[capture] started — play audio now.", flush=True)
            while not capture_should_stop_event.wait(0.2):
                pass

    print(f"\n[done] saved to {output_wav_path}", flush=True)


def main():
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument(
        "--output",
        default=None,
        help=("Output WAV path. Default: "
              "system_audio_capture_<timestamp>.wav in the current dir."),
    )
    parsed_arguments = argument_parser.parse_args()

    output_wav_path = parsed_arguments.output or (
        "system_audio_capture_"
        + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        + ".wav"
    )

    current_platform_system = platform.system()
    if current_platform_system == "Linux":
        capture_via_ffmpeg_pulse_on_linux(output_wav_path)
    elif current_platform_system == "Windows":
        capture_via_sounddevice_wasapi_loopback_on_windows(output_wav_path)
    else:
        print(f"[error] system audio loopback not supported on "
              f"{current_platform_system}. macOS needs BlackHole/Loopback.",
              flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
