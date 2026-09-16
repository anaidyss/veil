#!/usr/bin/env bash
# installs veil into ~/.local/share/veil, launcher into ~/.local/bin
set -euo pipefail
cd "$(dirname "$0")"

DIR=~/.local/share/veil
mkdir -p "$DIR/xml" ~/.local/bin

cp -f veil_lock.py "$DIR/"
cp -f xml/*.xml "$DIR/xml/"
cp -f veil-lock ~/.local/bin/
chmod +x ~/.local/bin/veil-lock

if [ ! -d "$DIR/venv" ]; then
    python3 -m venv "$DIR/venv"
    "$DIR/venv/bin/pip" install -q -r requirements.txt
fi

# protocol bindings from xml
cd "$DIR"
venv/bin/python -m pywayland.scanner -i xml/wayland.xml xml/ext-session-lock-v1.xml -o protocols

echo "done, run: veil-lock"
