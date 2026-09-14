#!/usr/bin/env bash
# 快速在终端展示手机连接二维码
DISPLAY_IP="100.66.1.4"
PORT="58002"
ACCESS_URL="http://${DISPLAY_IP}:${PORT}"

echo "============================================================"
echo " 📱 手机扫码直达输入界面: ${ACCESS_URL}"
echo "============================================================"
qrencode -t ANSIUTF8 "${ACCESS_URL}" || echo "请访问: ${ACCESS_URL}"
