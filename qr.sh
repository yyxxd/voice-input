#!/usr/bin/env bash
# 快速在终端展示手机连接二维码
LAN_IP=$(ip -4 addr show 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | grep -E '^192\.168\.|^10\.' | head -n 1 || true)
DISPLAY_IP="${VOICE_DISPLAY_IP:-${LAN_IP:-127.0.0.1}}"
PORT="58002"
ACCESS_URL="http://${DISPLAY_IP}:${PORT}"

echo "============================================================"
echo " 📱 手机扫码直达输入界面: ${ACCESS_URL}"
echo "============================================================"
qrencode -t ANSIUTF8 "${ACCESS_URL}" || echo "请访问: ${ACCESS_URL}"
