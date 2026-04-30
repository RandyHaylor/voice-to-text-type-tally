#!/bin/bash
# Streams the SYSTEM AUDIO (default sink monitor) to the whisper_streaming
# server, parses committed-text lines, and types them into the focused
# window via xdotool. Same pipeline as the mic client, just a different
# audio source — useful for transcribing meetings/videos/whatever is
# playing through your speakers.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
SERVER_PORT="${SERVER_PORT:-43007}"

# Resolve default sink and append .monitor → PulseAudio source name that
# captures whatever is playing on the default output.
DEFAULT_SINK_NAME="$(pactl get-default-sink)"
PULSE_MONITOR_SOURCE_NAME="${DEFAULT_SINK_NAME}.monitor"
echo "[source] $PULSE_MONITOR_SOURCE_NAME"

ffmpeg -loglevel quiet -f pulse -i "$PULSE_MONITOR_SOURCE_NAME" \
    -ac 1 -ar 16000 -f s16le - 2>/dev/null \
    | nc "$SERVER_HOST" "$SERVER_PORT" 2>/dev/null \
    | python3 -u "$SCRIPT_DIR/whisper_streaming_text_emitter.py"
