#!/bin/bash
# Double-click to start RenderHub on macOS.
cd "$(dirname "$0")" || exit 1
exec python3 run.py "$@"
