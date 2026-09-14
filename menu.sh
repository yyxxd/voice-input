#!/usr/bin/env bash

# =========================================================
# Voice Input Bridge - 一体化 TUI 控制中心
# =========================================================

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="58002"
FALLBACK_PORT="53317"
SERVICE_NAME="voice-input.service"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
PYTHON_BIN="$(which python3 || echo "/usr/bin/python3")"

# ANSI 颜色定义
C_RESET="\033[0m"
C_BOLD="\033[1m"
C_DIM="\033[2m"
C_GREEN="\033[1;32m"
C_BLUE="\033[1;34m"
C_CYAN="\033[1;36m"
C_YELLOW="\033[1;33m"
C_RED="\033[1;31m"
C_GRAY="\033[90m"

# ---------------------------------------------------------
# 1. 状态与环境探测
# ---------------------------------------------------------

get_connection_ip() {
    # 优先使用节点小宝虚拟 IP (若连通)
    if ping -c 1 -W 1 100.66.1.4 &>/dev/null; then
        echo "100.66.1.4"
        return
    fi
    # 其次取物理局域网 IP (192.168.x 或 10.x)
    local lan_ip
    lan_ip=$(ip -4 addr show 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | grep -E '^192\.168\.|^10\.' | head -n 1 || true)
    if [ -n "$lan_ip" ]; then
        echo "$lan_ip"
        return
    fi
    echo "127.0.0.1"
}

is_service_running() {
    # 1. 检查 HTTP 接口健康状态
    if curl -s --max-time 0.8 "http://127.0.0.1:${PORT}/api/status" 2>/dev/null | grep -q '"status":\s*"ok"'; then
        return 0
    fi
    if curl -s --max-time 0.8 "http://127.0.0.1:${FALLBACK_PORT}/api/status" 2>/dev/null | grep -q '"status":\s*"ok"'; then
        return 0
    fi
    # 2. 检查进程
    if pgrep -f "voice_input.py" &>/dev/null; then
        return 0
    fi
    # 3. 检查 systemd 用户单元
    if systemctl --user is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        return 0
    fi
    return 1
}

get_active_port() {
    if curl -s --max-time 0.5 "http://127.0.0.1:${PORT}/api/status" 2>/dev/null | grep -q '"status"'; then
        echo "${PORT}"
    elif curl -s --max-time 0.5 "http://127.0.0.1:${FALLBACK_PORT}/api/status" 2>/dev/null | grep -q '"status"'; then
        echo "${FALLBACK_PORT}"
    else
        echo "${PORT}"
    fi
}

is_autostart_enabled() {
    if systemctl --user is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
        return 0
    fi
    return 1
}

# ---------------------------------------------------------
# 2. 依赖自检与静默安装
# ---------------------------------------------------------

ensure_dependencies() {
    local missing=()
    for cmd in wl-copy wtype qrencode python3; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done

    if [ ${#missing[@]} -eq 0 ]; then
        return 0
    fi

    echo ""
    echo -e "${C_CYAN}┌─────────────────────────────────────────────────────────────┐${C_RESET}"
    echo -e "${C_CYAN}│ 🔍 正在进行系统依赖自检                                     │${C_RESET}"
    echo -e "${C_CYAN}├─────────────────────────────────────────────────────────────┤${C_RESET}"
    echo -e "  检测到当前系统缺少必要组件，即将进行自动安装:"
    for m in "${missing[@]}"; do
        echo -e "    ${C_YELLOW}○ ${m}${C_RESET} (系统必备支持工具)"
    done
    echo ""
    echo -e "  ${C_DIM}💡 组件总大小 < 200 KB，秒级完成，安装后将自动继续${C_RESET}"
    echo -e "${C_CYAN}└─────────────────────────────────────────────────────────────┘${C_RESET}"

    if command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm --needed wl-clipboard wtype qrencode python
    elif command -v apt-get &>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y wl-clipboard wtype qrencode python3
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y wl-clipboard wtype qrencode python3
    else
        echo -e "${C_RED}❌ 未能识别系统包管理器，请手动安装: ${missing[*]}${C_RESET}"
        read -n 1 -s -r -p "按任意键返回..."
        return 1
    fi

    echo -e "${C_GREEN}✅ 依赖组件准备完毕！${C_RESET}"
    sleep 0.8
}

# ---------------------------------------------------------
# 3. 核心控制动作
# ---------------------------------------------------------

do_start_service() {
    ensure_dependencies || return

    echo -e "\n${C_CYAN}🚀 正在启动 Voice Input Bridge 服务...${C_RESET}"

    # 若存在 systemd 单元且文件有效，优先通过 systemd 启动
    if [ -f "${USER_SYSTEMD_DIR}/${SERVICE_NAME}" ]; then
        systemctl --user start "${SERVICE_NAME}"
    else
        # 否则启动独立后台守护进程
        local ip
        ip=$(get_connection_ip)
        nohup "${PYTHON_BIN}" "${PROJECT_DIR}/voice_input.py" --display-ip "$ip" > /tmp/voice_input.log 2>&1 &
    fi

    sleep 1.2
    if is_service_running; then
        echo -e "${C_GREEN}✅ 服务已成功运行在后台！${C_RESET}"
    else
        echo -e "${C_RED}❌ 服务启动失败，请检查端口冲突或日志。${C_RESET}"
    fi
    sleep 0.8
}

do_stop_service() {
    echo -e "\n${C_YELLOW}🛑 正在停止 Voice Input Bridge 服务...${C_RESET}"

    # 停止 systemd
    if systemctl --user is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        systemctl --user stop "${SERVICE_NAME}" 2>/dev/null || true
    fi

    # 停止独立运行的 python 进程
    pkill -f "voice_input.py" 2>/dev/null || true

    sleep 0.8
    if ! is_service_running; then
        echo -e "${C_GREEN}✅ 服务已停止。${C_RESET}"
    else
        echo -e "${C_RED}⚠️ 服务可能仍在清理中...${C_RESET}"
    fi
    sleep 0.8
}
do_restart_service() {
    do_stop_service
    do_start_service
}

do_toggle_autostart() {
    if is_autostart_enabled; then
        echo -e "\n${C_YELLOW}🔄 正在关闭开机自启动...${C_RESET}"
        systemctl --user disable "${SERVICE_NAME}" 2>/dev/null || true
        echo -e "${C_GREEN}✅ 已取消开机自启动配置。${C_RESET}"
    else
        ensure_dependencies || return
        echo -e "\n${C_CYAN}🔄 正在配置系统开机自启服务...${C_RESET}"
        mkdir -p "${USER_SYSTEMD_DIR}"
        local ip
        ip=$(get_connection_ip)

        cat << EOF > "${USER_SYSTEMD_DIR}/${SERVICE_NAME}"
[Unit]
Description=Voice Input Bridge for Linux (Wayland)
After=graphical-session.target

[Service]
Type=simple
ExecStart=${PYTHON_BIN} ${PROJECT_DIR}/voice_input.py --display-ip ${ip}
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now "${SERVICE_NAME}"
        echo -e "${C_GREEN}✅ 已成功注册并开启开机自启服务！${C_RESET}"
    fi
    sleep 1
}

do_view_logs() {
    echo -e "\n${C_CYAN}📋 实时日志跟踪 (按 Ctrl+C 退出日志回到控制面板):${C_RESET}\n"
    if [ -f "${USER_SYSTEMD_DIR}/${SERVICE_NAME}" ] && systemctl --user is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        journalctl --user -u "${SERVICE_NAME}" -n 30 -f || true
    elif [ -f "/tmp/voice_input.log" ]; then
        tail -n 30 -f /tmp/voice_input.log || true
    else
        echo -e "${C_GRAY}暂无独立日志文件，建议通过 systemd 查看。${C_RESET}"
        read -n 1 -s -r -p "按任意键返回..."
    fi
}

do_uninstall() {
    echo ""
    echo -e "${C_RED}⚠️  注意：即将停止服务并彻底清理开机自启配置！${C_RESET}"
    read -r -p "确认清理并卸载吗？[y/N]: " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        echo -e "\n${C_YELLOW}正在卸载中...${C_RESET}"
        systemctl --user stop "${SERVICE_NAME}" 2>/dev/null || true
        systemctl --user disable "${SERVICE_NAME}" 2>/dev/null || true
        rm -f "${USER_SYSTEMD_DIR}/${SERVICE_NAME}"
        systemctl --user daemon-reload
        pkill -f "voice_input.py" 2>/dev/null || true
        echo -e "${C_GREEN}✅ 已完全清理开机服务与自启配置（本地项目代码保留）。${C_RESET}"
        sleep 1.2
    else
        echo -e "${C_GRAY}已取消操作。${C_RESET}"
        sleep 0.6
    fi
}

# ---------------------------------------------------------
# 4. TUI 仪表盘动态渲染
# ---------------------------------------------------------

draw_dashboard() {
    clear 2>/dev/null || true
    local running=false
    if is_service_running; then
        running=true
    fi

    local autostart=false
    if is_autostart_enabled; then
        autostart=true
    fi

    local conn_ip
    conn_ip=$(get_connection_ip)
    local active_port
    active_port=$(get_active_port)
    local access_url="http://${conn_ip}:${active_port}"

    # 顶部卡片
    echo -e "${C_CYAN}┌─────────────────────────────────────────────────────────────┐${C_RESET}"
    echo -e "${C_CYAN}│${C_BOLD} 🎙️  Voice Input Bridge 控制中心                           ${C_RESET}${C_CYAN}│${C_RESET}"
    echo -e "${C_CYAN}├─────────────────────────────────────────────────────────────┤${C_RESET}"

    # 状态指示看板
    local status_line=""
    if [ "$running" = true ]; then
        status_line="${C_GREEN}● 运行状态: 运行中 (端口: ${active_port})${C_RESET}"
    else
        status_line="${C_YELLOW}○ 运行状态: 未运行${C_RESET}               "
    fi

    local auto_line=""
    if [ "$autostart" = true ]; then
        auto_line="${C_GREEN}● 开机自启: 已启用${C_RESET}"
    else
        auto_line="${C_GRAY}○ 开机自启: 未配置${C_RESET}"
    fi

    echo -e "  ${status_line}     ${auto_line}"

    if [ "$running" = true ]; then
        echo -e "  ${C_BOLD}📱 手机直连:${C_RESET} ${C_CYAN}${access_url}${C_RESET}"
        echo -e "${C_CYAN}├─────────────────────────────────────────────────────────────┤${C_RESET}"
        echo -e "  ${C_DIM}手机扫码直达输入页:${C_RESET}"
        # 居中缩进打印二维码
        qrencode -t ANSIUTF8 "${access_url}" 2>/dev/null | sed 's/^/    /' || true
    else
        echo -e "${C_CYAN}├─────────────────────────────────────────────────────────────┤${C_RESET}"
        echo -e "  ${C_YELLOW}💡 服务尚未启动，请按 [1] 启动服务以自动生成连接二维码${C_RESET}"
    fi

    echo -e "${C_CYAN}├─────────────────────────────────────────────────────────────┤${C_RESET}"

    # 动态自适应菜单
    if [ "$running" = true ]; then
        echo -e "  ${C_BOLD}[1] 停止服务${C_RESET}"
        echo -e "      ${C_GRAY}└─ 关闭当前正在运行的后台服务${C_RESET}"
        echo -e "  ${C_BOLD}[2] 重启服务${C_RESET}"
        echo -e "      ${C_GRAY}└─ 重新载入最新代码并重启服务${C_RESET}"
        if [ "$autostart" = true ]; then
            echo -e "  ${C_BOLD}[3] 关闭开机自启${C_RESET}"
            echo -e "      ${C_GRAY}└─ 移除 systemd 开机自启配置${C_RESET}"
        else
            echo -e "  ${C_BOLD}[3] 开启开机自启${C_RESET}"
            echo -e "      ${C_GRAY}└─ 注册为系统服务，开机即静默后台运行${C_RESET}"
        fi
        echo -e "  ${C_BOLD}[4] 查看实时日志${C_RESET}"
        echo -e "      ${C_GRAY}└─ 实时跟踪手机端发来的打字请求与按键记录${C_RESET}"
        echo -e "  ${C_BOLD}[5] 清理与卸载${C_RESET}"
        echo -e "      ${C_GRAY}└─ 停止服务并清理系统自启配置文件${C_RESET}"
        echo -e "  ${C_BOLD}[0] 退出控制台${C_RESET} ${C_DIM}(后台服务继续运行)${C_RESET}"
    else
        echo -e "  ${C_BOLD}[1] 启动后台服务${C_RESET}"
        echo -e "      ${C_GRAY}└─ 在后台运行语音桥接服务，监听 ${PORT} 端口${C_RESET}"
        echo -e "  ${C_BOLD}[2] 注册并开启开机自启 (推荐)${C_RESET}"
        echo -e "      ${C_GRAY}└─ 配置为系统服务，开机即静默后台常驻${C_RESET}"
        echo -e "  ${C_BOLD}[3] 查看历史日志${C_RESET}"
        echo -e "      ${C_GRAY}└─ 查看最近的服务输出与连接记录${C_RESET}"
        echo -e "  ${C_BOLD}[4] 清理与卸载${C_RESET}"
        echo -e "      ${C_GRAY}└─ 清理系统服务与残留配置${C_RESET}"
        echo -e "  ${C_BOLD}[0] 退出控制台${C_RESET}"
    fi

    echo -e "${C_CYAN}└─────────────────────────────────────────────────────────────┘${C_RESET}"
}

# ---------------------------------------------------------
# 5. 主循环
# ---------------------------------------------------------

main_loop() {
    # 捕获 Ctrl+C 安全退出
    trap 'echo -e "\n\n${C_GRAY}[已退出控制台] 后台服务保持运行状态。${C_RESET}"; exit 0' INT

    while true; do
        draw_dashboard

        local running=false
        if is_service_running; then
            running=true
        fi

        echo -n -e "  ${C_BOLD}请输入选项 [0-5]: ${C_RESET}"
        read -r choice || break

        if [ "$running" = true ]; then
            case "$choice" in
                1) do_stop_service ;;
                2) do_restart_service ;;
                3) do_toggle_autostart ;;
                4) do_view_logs ;;
                5) do_uninstall ;;
                0|q|Q)
                    echo -e "\n${C_GREEN}👋 已退出控制台，后台服务继续静默运行。${C_RESET}"
                    exit 0
                    ;;
                *)
                    echo -e "${C_RED}无效选项，请重新输入${C_RESET}"
                    sleep 0.6
                    ;;
            esac
        else
            case "$choice" in
                1) do_start_service ;;
                2) do_toggle_autostart ;;
                3) do_view_logs ;;
                4) do_uninstall ;;
                0|q|Q)
                    echo -e "\n${C_GREEN}👋 已退出控制台。${C_RESET}"
                    exit 0
                    ;;
                *)
                    echo -e "${C_RED}无效选项，请重新输入${C_RESET}"
                    sleep 0.6
                    ;;
            esac
        fi
    done
}

main_loop
