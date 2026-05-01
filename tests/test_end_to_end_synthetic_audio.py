"""End-to-end smoke test:

1. Start the whisper_streaming server in CPU mode using the bundled
   `tiny.en` model.
2. Open a TCP socket to it.
3. Stream the bundled `tests/test_audio/short_clip.wav` as raw 16 kHz
   mono int16 PCM through ffmpeg.
4. Read transcript lines off the socket.
5. Assert at least one non-empty transcribed token came back.

Skipped gracefully when ffmpeg or faster-whisper isn't available, or
when the bundled audio + tiny.en model aren't both present locally.
"""

import importlib.util
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


REPO_ROOT_DIRECTORY = Path(__file__).resolve().parent.parent
TEST_AUDIO_FILE_PATH = REPO_ROOT_DIRECTORY / "tests" / "test_audio" / "short_clip.wav"
LOCAL_TINY_EN_MODEL_DIR = REPO_ROOT_DIRECTORY / "models" / "tiny.en"
SERVER_RUNNER_SCRIPT_PATH = (
    REPO_ROOT_DIRECTORY
    / "whisper_streaming_server_runner_with_device_choice.py"
)
END_TO_END_SERVER_PORT = 43099  # avoid colliding with default 43007


def _faster_whisper_is_available():
    return importlib.util.find_spec("faster_whisper") is not None


@pytest.mark.skipif(
    not _faster_whisper_is_available(),
    reason="faster-whisper not installed",
)
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH"
)
@pytest.mark.skipif(
    not (LOCAL_TINY_EN_MODEL_DIR / "model.bin").is_file(),
    reason=f"local tiny.en model.bin missing at {LOCAL_TINY_EN_MODEL_DIR}",
)
@pytest.mark.skipif(
    not TEST_AUDIO_FILE_PATH.is_file(),
    reason=f"bundled test audio missing at {TEST_AUDIO_FILE_PATH}",
)
def test_end_to_end_pipeline_produces_non_empty_transcript():
    # Force CPU + reasonably small chunk so this finishes within ~60s.
    server_environment = os.environ.copy()
    server_environment["WHISPER_DEVICE"] = "cpu"
    server_environment["CUDA_VISIBLE_DEVICES"] = ""

    server_argv = [
        sys.executable,
        str(SERVER_RUNNER_SCRIPT_PATH),
        "--host", "127.0.0.1",
        "--port", str(END_TO_END_SERVER_PORT),
        "--backend", "faster-whisper",
        "--model_dir", str(LOCAL_TINY_EN_MODEL_DIR),
        "--lan", "en",
        "--min-chunk-size", "1.0",
        "--vad",
        "-l", "WARNING",
    ]
    server_subprocess = subprocess.Popen(
        server_argv,
        env=server_environment,
        cwd=str(REPO_ROOT_DIRECTORY),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    try:
        # Wait for the server to bind the port (model load can take a
        # few seconds on CPU).
        wait_deadline_seconds = time.time() + 90.0
        while time.time() < wait_deadline_seconds:
            try:
                with socket.create_connection(
                    ("127.0.0.1", END_TO_END_SERVER_PORT), timeout=1.0
                ):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            stderr_tail = b""
            try:
                stderr_tail = server_subprocess.stderr.read() or b""
            except Exception:
                pass
            pytest.fail(
                "server didn't open port "
                f"{END_TO_END_SERVER_PORT} within 90s.\n"
                f"stderr: {stderr_tail.decode('utf-8', 'replace')[-2000:]}"
            )

        # Connect and stream the wav as raw PCM via ffmpeg.
        client_socket = socket.create_connection(
            ("127.0.0.1", END_TO_END_SERVER_PORT), timeout=10.0
        )
        try:
            ffmpeg_argv = [
                "ffmpeg",
                "-loglevel", "error",
                "-i", str(TEST_AUDIO_FILE_PATH),
                "-ac", "1",
                "-ar", "16000",
                "-f", "s16le",
                "-",
            ]
            ffmpeg_subprocess = subprocess.Popen(
                ffmpeg_argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )

            def pump_audio_pcm_to_socket():
                try:
                    while True:
                        audio_chunk_bytes = ffmpeg_subprocess.stdout.read(8192)
                        if not audio_chunk_bytes:
                            break
                        client_socket.sendall(audio_chunk_bytes)
                except (OSError, ValueError):
                    pass
                finally:
                    try:
                        client_socket.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            pump_thread = threading.Thread(
                target=pump_audio_pcm_to_socket, daemon=True
            )
            pump_thread.start()

            # Read transcript lines until the server closes the connection
            # or we've been reading for too long.
            received_text_segments = []
            client_socket.settimeout(60.0)
            line_buffer = b""
            read_deadline = time.time() + 90.0
            while time.time() < read_deadline:
                try:
                    chunk_bytes = client_socket.recv(4096)
                except socket.timeout:
                    break
                if not chunk_bytes:
                    break
                line_buffer += chunk_bytes
                while b"\n" in line_buffer:
                    one_line, line_buffer = line_buffer.split(b"\n", 1)
                    decoded_line = one_line.decode("utf-8", "replace").strip()
                    if not decoded_line:
                        continue
                    parts = decoded_line.split(" ", 2)
                    if (
                        len(parts) == 3
                        and parts[0].lstrip("-").isdigit()
                        and parts[1].lstrip("-").isdigit()
                    ):
                        received_text_segments.append(parts[2])
                if pump_thread is not None and not pump_thread.is_alive():
                    # Audio fully sent; give the server a moment to flush.
                    pass

            assert received_text_segments, (
                "no transcript text received from server end-to-end"
            )
            joined_text = " ".join(received_text_segments).strip().lower()
            assert joined_text, "transcript was empty after joining"
        finally:
            try:
                client_socket.close()
            except OSError:
                pass
            try:
                ffmpeg_subprocess.terminate()
            except Exception:
                pass
    finally:
        try:
            server_subprocess.terminate()
            server_subprocess.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_subprocess.kill()
        except Exception:
            pass
