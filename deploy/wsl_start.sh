#!/usr/bin/env bash
# 在 WSL / Ubuntu 中启动（或重启）服务，使用系统 Python
APP=${APP_DIR:-/mnt/e/autonews}
HOST_ADDR=${HOST:-0.0.0.0}
PORT_NUM=${PORT:-8000}
export TZ=${TZ:-Asia/Shanghai}

cd "$APP" || exit 1
mkdir -p data

pkill -f "python3 run.py" 2>/dev/null || true
sleep 1

HOST=$HOST_ADDR PORT=$PORT_NUM TZ=$TZ \
  setsid nohup python3 run.py > data/server.log 2>&1 &

sleep 3
echo "[start] http://$HOST_ADDR:$PORT_NUM"
tail -n 8 data/server.log
