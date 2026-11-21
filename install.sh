#!/bin/sh
# Claude Relay one-line installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/CodeWidKrish/claude-relay/main/install.sh | sh
#
# Downloads relay.py (or updates a git clone), then runs `relay.py setup`, which installs Relay into
# Claude Code and, when it's present, Codex CLI. Nothing else is downloaded. Set RELAY_DIR to choose
# the folder (default ~/.claude-relay/app).
set -eu

SOURCE="https://raw.githubusercontent.com/CodeWidKrish/claude-relay/main/relay.py"
DIR="${RELAY_DIR:-$HOME/.claude-relay/app}"

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "Claude Relay needs Python 3.8 or newer: https://www.python.org/downloads/" >&2
    exit 1
fi

mkdir -p "$DIR"
if [ -d "$DIR/.git" ] && command -v git >/dev/null 2>&1; then
    echo "Updating the git clone in $DIR ..."
    git -C "$DIR" pull --ff-only
elif command -v curl >/dev/null 2>&1; then
    echo "Downloading relay.py to $DIR ..."
    curl -fsSL "$SOURCE" -o "$DIR/relay.py"
else
    echo "Downloading relay.py to $DIR ..."
    wget -qO "$DIR/relay.py" "$SOURCE"
fi

exec "$PYTHON" "$DIR/relay.py" setup
