#!/bin/bash
cd "$(dirname "$0")"

# Reinstall Playwright system deps — these don't persist across reboots
PACKAGES_DIR="$(pwd)/screenshot_module/.local-packages"
echo "[*] Reinstalling Playwright system dependencies..."
sudo PYTHONPATH="$PACKAGES_DIR" python3 -m playwright install-deps chromium 2>/dev/null && sudo ldconfig
echo "[*] Done"

nohup python3 slack_bot/poller.py > poller.log 2>&1 &
echo "[+] poller.py PID: $!"
nohup python3 slack_bot/github_bridge.py > github_bridge.log 2>&1 &
echo "[+] github_bridge.py PID: $!"
sleep 2 && ps aux | grep -E "poller|github_bridge" | grep -v grep
