#!/bin/bash
# 双击运行即可启动 sendfiledrop
cd "$(dirname "$0")"

PORT=8000

echo "正在启动 sendfiledrop..."

PIDS=$(lsof -ti tcp:$PORT)
if [ -n "$PIDS" ]; then
  echo "检测到端口 $PORT 已被占用，正在停止旧的服务 (PID: $PIDS)..."
  kill $PIDS
  sleep 1
  PIDS=$(lsof -ti tcp:$PORT)
  if [ -n "$PIDS" ]; then
    kill -9 $PIDS
    sleep 1
  fi
  echo "旧服务已停止，重新启动..."
fi

python3 server.py "$PORT"
