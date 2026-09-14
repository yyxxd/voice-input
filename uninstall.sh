#!/usr/bin/env bash
set -e

# =========================================================
# Voice Input Bridge - 一键卸载脚本
# =========================================================

SERVICE_NAME="voice-input.service"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo "============================================================"
echo " 🗑️  正在卸载 Voice Input Bridge 服务..."
echo "============================================================"

# 1. 停止并禁用服务
if systemctl --user list-unit-files | grep -q "${SERVICE_NAME}"; then
    echo "[1/3] 停止并禁用后台服务..."
    systemctl --user stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl --user disable "${SERVICE_NAME}" 2>/dev/null || true
fi

# 2. 删除服务定义文件
echo "[2/3] 清理 Systemd 配置文件..."
rm -f "${USER_SYSTEMD_DIR}/${SERVICE_NAME}"
systemctl --user daemon-reload

# 3. 完成提示
echo "[3/3] 卸载完成！"
echo "============================================================"
echo " ✅ 服务已彻底停止并解除开机自启。"
echo " • 项目代码文件仍保留在当前目录。"
echo "============================================================"
