#!/bin/zsh
# One-command music sync (macOS): routes system audio through BlackHole via a
# Multi-Output Device, runs the lighting sync, restores the output on Ctrl-C.
#
#   ./musicsync.sh                 # music mode
#   ./musicsync.sh --mode meter    # any music_sync.py flags pass through
#   ./musicsync.sh off             # just restore audio output
#   ./musicsync.sh --probe         # check device links (no audio routing)
set -u
DIR="${0:A:h}"
PY="$DIR/.venv/bin/python"
[[ -x "$PY" ]] || { echo "run ./setup.sh first"; exit 1; }
if [[ -x "$DIR/macos/setup_multi_output" ]]; then AUDIO=("$DIR/macos/setup_multi_output")
else AUDIO=(swift "$DIR/macos/setup_multi_output.swift"); fi

reverted=0
revert() { (( reverted )) && return; reverted=1; "${AUDIO[@]}" --revert; }

case "${1:-}" in
  off)     revert; exit 0 ;;
  --probe|--list-audio|--dry-run) exec "$PY" -u "$DIR/music_sync.py" "$@" ;;
esac

"${AUDIO[@]}" || exit 1
ARGS=("$@")
(( ${#ARGS} == 0 )) && ARGS=(--mode music)
"$PY" -u "$DIR/music_sync.py" "${ARGS[@]}" &
child=$!
trap 'kill -INT $child 2>/dev/null' INT TERM
wait $child
trap - INT TERM
revert
