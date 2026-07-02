#!/bin/bash
cd "$(dirname "$0")"
nohup python3 slack_bot/poller.py > poller.log 2>&1 &
echo "[+] poller.py PID: $!"
nohup python3 slack_bot/github_bridge.py > github_bridge.log 2>&1 &
echo "[+] github_bridge.py PID: $!"
sleep 2 && ps aux | grep -E "poller|github_bridge" | grep -v grep
