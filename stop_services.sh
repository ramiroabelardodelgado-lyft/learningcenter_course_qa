#!/bin/bash
pkill -f poller.py && echo "[-] poller.py stopped" || echo "[!] poller.py not running"
pkill -f github_bridge.py && echo "[-] github_bridge.py stopped" || echo "[!] github_bridge.py not running"
