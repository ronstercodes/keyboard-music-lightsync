#!/bin/zsh
# One-time setup: Python venv + deps, and (macOS) the audio-routing helper.
set -e
cd "${0:A:h}"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
if [[ "$(uname)" == "Darwin" ]]; then
  if command -v swiftc >/dev/null; then
    swiftc -O macos/setup_multi_output.swift -o macos/setup_multi_output && echo "built macos/setup_multi_output"
  else
    echo "swiftc not found (install Xcode Command Line Tools); musicsync.sh will fall back to 'swift' each run"
  fi
  ls /Library/Audio/Plug-Ins/HAL 2>/dev/null | grep -qi blackhole || \
    echo "BlackHole not installed. For system-audio capture:  brew install --cask blackhole-2ch  (then: sudo killall coreaudiod)"
fi
echo "done. try:  ./musicsync.sh --probe   or   .venv/bin/python music_sync.py --dry-run"
