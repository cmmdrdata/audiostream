#!/usr/bin/env bash
# Installs AudioStream as a systemd service that starts at boot.
# Usage:  sudo ./deploy/install.sh [user]      (default user: whoever ran sudo)
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Run with sudo: sudo $0"; exit 1; fi

RUN_USER="${1:-${SUDO_USER:-}}"
if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
  echo "Tell me which account should run it: sudo $0 <username>"; exit 1
fi
RUN_GROUP="$(id -gn "$RUN_USER")"
USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
APPDIR="$(cd "$(dirname "$0")/.." && pwd)"

sed -e "s|__USER__|$RUN_USER|g" -e "s|__GROUP__|$RUN_GROUP|g" -e "s|__APPDIR__|$APPDIR|g" \
    "$APPDIR/deploy/audiostream.service" > /etc/systemd/system/audiostream.service

if [[ ! -f /etc/audiostream.env ]]; then
  sed "s|/home/you|$USER_HOME|g" "$APPDIR/deploy/audiostream.env.example" > /etc/audiostream.env
  chmod 600 /etc/audiostream.env
  echo "Created /etc/audiostream.env - edit it to set your folders and password."
fi

systemctl daemon-reload
systemctl enable --now audiostream
sleep 1
systemctl --no-pager --lines=5 status audiostream || true
echo
echo "Installed. Open http://$(hostname -I 2>/dev/null | awk '{print $1}'):\$PORT  (port is in /etc/audiostream.env)"
echo "After editing /etc/audiostream.env:  sudo systemctl restart audiostream"
