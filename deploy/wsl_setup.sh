#!/usr/bin/env bash
# WSL / Ubuntu 环境准备：直接用系统 Python 全局安装依赖（不使用虚拟环境，幂等）
set -e

APP=${APP_DIR:-/mnt/e/autonews}
PIP_INDEX=${PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}

command -v python3 >/dev/null || { apt-get update -qq; apt-get install -y -qq python3; }

# 确保 pip 存在
python3 -m pip --version >/dev/null 2>&1 || {
  apt-get update -qq
  apt-get install -y -qq python3-pip
}

# Ubuntu 23.04+ / Debian 12+ 的 PEP 668 限制，需要 --break-system-packages
PIP_FLAGS=""
python3 -m pip install --help 2>/dev/null | grep -q "break-system-packages" && PIP_FLAGS="--break-system-packages"

python3 -m pip install -q $PIP_FLAGS --upgrade pip 2>/dev/null || true
python3 -m pip install -q $PIP_FLAGS -r "$APP/requirements.txt" -i "$PIP_INDEX"

# 视频二次加工（截取 10~15s / 转码 / 抽帧）依赖 ffmpeg
command -v ffmpeg >/dev/null || { apt-get update -qq; apt-get install -y -qq ffmpeg; }

echo "[setup] done, python: $(command -v python3)"
python3 -c "import fastapi, uvicorn, httpx, openpyxl, PIL; print('[setup] deps ok')"
