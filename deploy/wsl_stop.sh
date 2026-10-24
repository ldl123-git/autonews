#!/usr/bin/env bash
# 停止 WSL 中的服务
pkill -f "python3 run.py" 2>/dev/null || true
sleep 1
echo "[stop] service stopped"
