#!/usr/bin/env bash
set -e

# =========================================================
# Voice Input Bridge - 一键自动化安装与自启配置脚本
# =========================================================

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="voice-input.service"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo "============================================================"
echo " 🎙️  Voice Input Bridge 一键安装与配置"
echo "============================================================"

# 1. 检查必备依赖
echo "[1/4] 检查系统环境与依赖..."
MISSING_PKGS=()
for cmd in wl-copy wtype qrencode python3; do
    if ! command -v "$cmd" &>/dev/null; then
        MISSING_PKGS+=("$cmd")
    fi
done

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo "  ⚠️ 检测到缺少必要依赖: ${MISSING_PKGS[*]}"
    echo "  正在尝试通过系统包管理器安装..."
    if command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm wl-clipboard wtype qrencode python
    elif command -v apt-get &>/dev/null; then
        sudo apt-get update && sudo apt-get install -y wl-clipboard wtype qrencode python3
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y wl-clipboard wtype qrencode python3
    else
        echo "  ❌ 未识别的包管理器，请手动安装: ${MISSING_PKGS[*]}"
        exit 1
    fi
else
    echo "  ✅ 所有依赖 (wl-clipboard, wtype, qrencode, python3) 已就绪！"
fi

# 2. 生成并链接 Systemd User Service
echo "[2/4] 配置开机自启动服务..."
mkdir -p "${USER_SYSTEMD_DIR}"

cat << EOF > "${USER_SYSTEMD_DIR}/${SERVICE_NAME}"
[Unit]
Description=Voice Input Bridge for Linux (Wayland)
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${PROJECT_DIR}/voice_input.py --display-ip 100.66.1.4
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

echo "  ✅ 服务配置已写入: ${USER_SYSTEMD_DIR}/${SERVICE_NAME}"

# 3. 启动并启用服务
echo "[3/4] 启动后台服务并配置开机自启..."
systemctl --user daemon-reload
systemctl --user enable --now "${SERVICE_NAME}"

sleep 1
if systemctl --user is-active --quiet "${SERVICE_NAME}"; then
    echo "  ✅ 服务已成功运行在后台！"
else
    echo "  ⚠️ 服务启动中，请通过 'systemctl --user status ${SERVICE_NAME}' 查看详情"
fi

# 4. 显示连接二维码与说明
echo "[4/4] 生成手机连接二维码..."
echo "============================================================"
LAN_IP=$(ip -4 addr show | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | grep -E '^192\.168\.|^10\.' | head -n 1 || echo "127.0.0.1")
DISPLAY_IP="100.66.1.4"
if ! ping -c 1 -W 1 "${DISPLAY_IP}" &>/dev/null; then
    DISPLAY_IP="${LAN_IP}"
fi

ACCESS_URL="http://${DISPLAY_IP}:58002"
echo " • 手机直连地址: ${ACCESS_URL}"
echo " • 备用局域网地址: http://${LAN_IP}:58002"
echo ""
qrencode -t ANSIUTF8 "${ACCESS_URL}" || true

echo "============================================================"
echo " 🎉 安装完成！"
echo " • 常用管理命令:"
echo "   - 查看状态: systemctl --user status voice-input"
echo "   - 停止服务: systemctl --user stop voice-input"
echo "   - 重启服务: systemctl --user restart voice-input"
echo "   - 查看日志: journalctl --user -u voice-input -f"
echo "============================================================"
