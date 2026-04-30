#!/bin/bash
# Streams system audio (default sink monitor) to the whisper_streaming server,
# parses committed text lines, prints them to the console AND appends them
# to a text file (path defaults to system_audio_transcript_<timestamp>.txt
# in the current directory; override with the first argument).

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
SERVER_PORT="${SERVER_PORT:-43007}"

OUTPUT_TRANSCRIPT_FILE="${1:-system_audio_transcript_$(date +%Y%m%d_%H%M%S).txt}"

DEFAULT_SINK_NAME="$(pactl get-default-sink)"
PULSE_MONITOR_SOURCE_NAME="${DEFAULT_SINK_NAME}.monitor"
echo "[source]    $PULSE_MONITOR_SOURCE_NAME"
echo "[transcript] $OUTPUT_TRANSCRIPT_FILE"
echo "[server]    $SERVER_HOST:$SERVER_PORT"
echo "[ctrl-c] stops the pipeline."

# nc reads server response (committed text lines) → awk strips leading
# "<begin_ms> <end_ms>" timestamps → tee writes to console AND file.
ffmpeg -loglevel quiet -f pulse -i "$PULSE_MONITOR_SOURCE_NAME" \
    -ac 1 -ar 16000 -f s16le - 2>/dev/null \
    | nc "$SERVER_HOST" "$SERVER_PORT" 2>/dev/null \
    | awk '{ if (NF >= 3 && $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/) { sub(/^[0-9]+ +[0-9]+ +/, ""); print; fflush() } }' \
    | tee -a "$OUTPUT_TRANSCRIPT_FILE"
