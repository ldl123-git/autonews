#!/bin/bash
cd /mnt/e/autonews
pkill -f "run.py" 2>/dev/null || true
pkill -f "uvicorn app.main" 2>/dev/null || true
sleep 1
nohup python3 run.py > /tmp/autonews.log 2>&1 &
echo "spawned pid=$!"
sleep 4
curl -s http://127.0.0.1:8000/api/stats | head -c 200
echo
tail -5 /tmp/autonews.log
