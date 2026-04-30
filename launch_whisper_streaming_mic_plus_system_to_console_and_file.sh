#!/bin/bash
# Streams MIC + SYSTEM AUDIO mixed together to whisper_streaming, parses
# committed text lines, prints them to console AND appends to a file.
# Useful for transcribing a meeting where you want BOTH your own voice
# (mic) AND the other side's audio (system loopback) in one transcript.

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
SERVER_PORT="${SERVER_PORT:-43007}"

OUTPUT_TRANSCRIPT_FILE="${1:-mic_plus_system_transcript_$(date +%Y%m%d_%H%M%S).txt}"

DEFAULT_SINK_NAME="$(pactl get-default-sink)"
PULSE_MONITOR_SOURCE_NAME="${DEFAULT_SINK_NAME}.monitor"
PULSE_MIC_SOURCE_NAME="$(pactl get-default-source)"

echo "[mic source]    $PULSE_MIC_SOURCE_NAME"
echo "[system source] $PULSE_MONITOR_SOURCE_NAME"
echo "[transcript]    $OUTPUT_TRANSCRIPT_FILE"
echo "[server]        $SERVER_HOST:$SERVER_PORT"
echo "[ctrl-c] stops the pipeline."

# Two ffmpeg pulse inputs → amix to a single mono 16 kHz s16le PCM stream.
# `dropout_transition=0` keeps the level flat when one source is silent.
# `duration=longest` makes amix run as long as either source is producing.
ffmpeg -loglevel quiet \
    -f pulse -i "$PULSE_MIC_SOURCE_NAME" \
    -f pulse -i "$PULSE_MONITOR_SOURCE_NAME" \
    -filter_complex "[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0[mixed_audio]" \
    -map "[mixed_audio]" \
    -ac 1 -ar 16000 -f s16le - 2>/dev/null \
    | nc "$SERVER_HOST" "$SERVER_PORT" 2>/dev/null \
    | awk '{ if (NF >= 3 && $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/) { sub(/^[0-9]+ +[0-9]+ +/, ""); print; fflush() } }' \
    | tee -a "$OUTPUT_TRANSCRIPT_FILE"
