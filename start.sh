#!/usr/bin/env bash
# Launcher for Parakeet Dictation (uses the local uv venv).
cd "$(dirname "$(readlink -f "$0")")" || exit 1
exec ./.venv/bin/python -m parakeet_dictation "$@"
