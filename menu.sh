#!/usr/bin/env bash

# =========================================================
# Voice Input Bridge - 一体化 TUI 控制中心
# =========================================================

# 0. 图形界面直接双击自适应唤起终端窗口
if [ ! -t 0 ] || [ ! -t 1 ]; then
    for term in alacritty foot kitty ghostty gnome-terminal xfce4-terminal konsole xterm; do
        if command -v "$term" &>/dev/null; then
            exec "$term" -e "$0" "$@"
        fi
    done
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="58002"
FALLBACK_PORT="53317"
SERVICE_NAME="voice-input.service"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
PYTHON_BIN="$(command -v python3 || echo "/usr/bin/python3")"
cd "${PROJECT_DIR}"

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
    if [ -n "$VOICE_DISPLAY_IP" ]; then
        echo "$VOICE_DISPLAY_IP"
        return
    fi
    local lan_ip
    lan_ip=$("${PYTHON_BIN}" -c 'import voice_input; print(next(iter(voice_input.get_lan_ips()), "127.0.0.1"))' 2>/dev/null)
    if [ -n "$lan_ip" ]; then
        echo "$lan_ip"
        return
    fi
    lan_ip=$(ip -4 addr show 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | grep -E '^192\.168\.|^10\.' | head -n 1 || true)
    if [ -z "$lan_ip" ]; then
        lan_ip=$(ip -4 addr show 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | grep -v '127.0.0.1' | head -n 1 || true)
    fi
    echo "${lan_ip:-127.0.0.1}"
}

is_service_running() {
    # 1. 检查 HTTP 接口健康状态
    if curl -s --max-time 0.8 "http://127.0.0.1:${PORT}/api/status" 2>/dev/null | grep -q '"status":\s*"ok"'; then
        return 0
    fi
    if curl -s --max-time 0.8 "http://127.0.0.1:${FALLBACK_PORT}/api/status" 2>/dev/null | grep -q '"status":\s*"ok"'; then
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
    for cmd in wl-copy wtype qrencode python3 notify-send; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done

    if [[ "${XDG_CURRENT_DESKTOP,,}" == *gnome* ]] && ! /usr/bin/python3 -c 'import dbus' 2>/dev/null; then
        missing+=("python3-dbus")
    fi

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
        sudo pacman -S --noconfirm --needed wl-clipboard wtype qrencode python python-dbus libnotify
    elif command -v apt-get &>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y wl-clipboard wtype qrencode python3 python3-dbus libnotify-bin
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y wl-clipboard wtype qrencode python3 python3-dbus libnotify
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

    systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE DBUS_SESSION_BUS_ADDRESS 2>/dev/null || true

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
        systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE DBUS_SESSION_BUS_ADDRESS 2>/dev/null || true
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

# Compact dashboard: connection QR is a separate view so menus fit small screens.
SELECTED=0
MENU_ACTIONS=()
MENU_LABELS=()
MENU_HINTS=()

build_menu() {
    MENU_ACTIONS=()
    MENU_LABELS=()
    MENU_HINTS=()
    if is_service_running; then
        RUNNING=true
        MENU_ACTIONS+=(do_stop_service do_restart_service)
        MENU_LABELS+=("停止服务" "重启服务")
        MENU_HINTS+=("关闭后台接收服务" "载入最新代码并重新启动")
    else
        RUNNING=false
        MENU_ACTIONS+=(do_start_service)
        MENU_LABELS+=("启动后台服务")
        MENU_HINTS+=("开始接收手机文字")
    fi
    local auto_label="开启开机自启"
    AUTO_STATUS="未启用"
    if is_autostart_enabled; then
        auto_label="关闭开机自启"
        AUTO_STATUS="已启用"
    fi
    MENU_ACTIONS+=(do_connection do_toggle_autostart do_view_logs do_uninstall do_exit)
    MENU_LABELS+=("手机连接 / 二维码" "$auto_label" "查看日志" "清理与卸载" "退出控制台")
    MENU_HINTS+=("显示所有连接地址与扫码入口" "管理 systemd 用户服务" "检查连接和发送记录" "清理服务配置，保留项目文件" "后台服务继续运行")
    ACTIVE_PORT=$(get_active_port)
    ACCESS_URL="http://$(get_connection_ip):${ACTIVE_PORT}"
    (( SELECTED < ${#MENU_ACTIONS[@]} )) || SELECTED=0
}

do_connection() {
    clear
    echo -e "${C_CYAN}${C_BOLD}  手机连接${C_RESET}"
    echo -e "\n  ${C_CYAN}${ACCESS_URL}${C_RESET}\n"
    if [ "$RUNNING" = true ]; then
        qrencode -t ANSIUTF8 "$ACCESS_URL" 2>/dev/null || true
    else
        echo "  请先启动后台服务。"
    fi
    echo "  手机与电脑需处于同一网络。"
    echo "  GNOME 终端请选择手机页面的「终端模式」。"
    read -r -p "  按回车返回..."
}

do_exit() {
    echo -e "\n${C_GRAY}已退出控制台，后台服务保持当前状态。${C_RESET}"
    exit 0
}

dashboard_content() {
    echo -e "${C_CYAN}  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RESET}"
    echo -e "${C_BOLD}  VOICE INPUT${C_RESET}  ${C_GRAY}手机语音 · 直达电脑${C_RESET}"
    echo -e "${C_CYAN}  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RESET}"
    if [ "$RUNNING" = true ]; then
        echo -e "  ${C_GREEN}● 运行中${C_RESET}  ${C_GRAY}端口 ${ACTIVE_PORT} · 自启 ${AUTO_STATUS}${C_RESET}"
    else
        echo -e "  ${C_YELLOW}○ 已停止${C_RESET}  ${C_GRAY}自启 ${AUTO_STATUS}${C_RESET}"
    fi
    echo -e "  ${C_CYAN}${ACCESS_URL}${C_RESET}\n"
    local i
    for i in "${!MENU_LABELS[@]}"; do
        if [ "$i" -eq "$SELECTED" ]; then
            echo -e "  ${C_CYAN}${C_BOLD}❯ ${MENU_LABELS[$i]}${C_RESET}"
        else
            echo -e "    ${MENU_LABELS[$i]}"
        fi
    done
    echo -e "\n  ${C_GRAY}${MENU_HINTS[$SELECTED]}${C_RESET}"
    echo -e "${C_CYAN}  ──────────────────────────────────────────────${C_RESET}"
    echo -e "  ${C_DIM}↑ ↓ 选择   Enter 执行   Q 退出${C_RESET}"
}

# Paint complete screens in one write; arrows only touch the changed rows.
draw_dashboard() {
    local frame
    frame=$(dashboard_content)
    frame=${frame//$'\n'/$'\033[K\n'}
    printf '\033[?25l\033[H%b\033[K\033[J\033[s' "$frame"
}

update_selection() {
    local previous="$1" frame="" line index
    for index in "$previous" "$SELECTED"; do
        if [ "$index" -eq "$SELECTED" ]; then
            line="  ${C_CYAN}${C_BOLD}❯ ${MENU_LABELS[$index]}${C_RESET}"
        else
            line="    ${MENU_LABELS[$index]}"
        fi
        printf -v line '\033[%d;1H%b\033[K' "$((7 + index))" "$line"
        frame+="$line"
    done
    printf -v line '\033[%d;1H  %b%s%b\033[K' "$((8 + ${#MENU_LABELS[@]}))" "$C_GRAY" "${MENU_HINTS[$SELECTED]}" "$C_RESET"
    frame+="$line"
    printf '%b\033[u' "$frame"
}

main_loop() {
    trap 'printf "\033[0m\033[?25h"' EXIT
    trap 'do_exit' INT
    trap 'draw_dashboard' WINCH
    build_menu
    draw_dashboard
    while true; do
        local key tail previous="$SELECTED"
        local read_status
        IFS= read -rsn1 -t 0.5 key
        read_status=$?
        if [ "$read_status" -ne 0 ]; then
            [ "$read_status" -gt 128 ] && continue
            break
        fi
        if [[ "$key" == $'\e' ]]; then
            IFS= read -rsn1 -t 0.15 tail || tail=""
            if [[ "$tail" == '[' || "$tail" == 'O' ]]; then
                IFS= read -rsn1 -t 0.15 key || key=""
                case "$key" in
                    A) SELECTED=$(( (SELECTED + ${#MENU_ACTIONS[@]} - 1) % ${#MENU_ACTIONS[@]} )) ;;
                    B) SELECTED=$(( (SELECTED + 1) % ${#MENU_ACTIONS[@]} )) ;;
                esac
            fi
        else
            case "$key" in
                k) SELECTED=$(( (SELECTED + ${#MENU_ACTIONS[@]} - 1) % ${#MENU_ACTIONS[@]} )) ;;
                j) SELECTED=$(( (SELECTED + 1) % ${#MENU_ACTIONS[@]} )) ;;
                "")
                    printf '\033[?25h\n'
                    "${MENU_ACTIONS[$SELECTED]}"
                    build_menu
                    draw_dashboard
                    ;;
                q|Q) do_exit ;;
            esac
        fi
        if [ "$previous" -ne "$SELECTED" ]; then
            update_selection "$previous"
        fi
    done
}

main_loop
