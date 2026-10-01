#!/usr/bin/env python3
"""
Voice Input Bridge - Windows 交互式控制中心 (Python 驱动)
避免 Windows CMD/BAT 文件在不同区域编码下的中文乱码问题。
"""
import os
import sys
import time
import urllib.request
import urllib.error
import json
import subprocess
if sys.platform == "win32":
    import winreg

# 确保控制台支持 UTF-8 与 ANSI 颜色
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        h_stdout = kernel32.GetStdHandle(-11)
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(h_stdout, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h_stdout, mode.value | 0x0004)
    except Exception:
        pass

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
VOICE_SCRIPT = os.path.join(PROJECT_DIR, "voice_input.py")

# 获取匹配的 pythonw.exe
PYTHON_DIR = os.path.dirname(sys.executable)
PYTHONW_BIN = os.path.join(PYTHON_DIR, "pythonw.exe")
if not os.path.exists(PYTHONW_BIN):
    PYTHONW_BIN = sys.executable

REG_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_RUN_NAME = "VoiceInputBridge"

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

def check_service_status():
    """检查 58002 与备用端口 53317 的运行状态"""
    for port in (58002, 53317):
        try:
            url = f"http://127.0.0.1:{port}/api/status"
            with urllib.request.urlopen(url, timeout=0.3) as r:
                if r.status == 200:
                    data = json.loads(r.read().decode("utf-8"))
                    if data.get("status") == "ok":
                        return True, port
        except Exception:
            pass
    return False, 58002

def is_autostart_enabled():
    """检查是否配置了当前用户开机自启 (免管理员权限)"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, REG_RUN_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except Exception:
        return False

def toggle_autostart():
    """切换开机自启动"""
    enabled = is_autostart_enabled()
    if enabled:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, REG_RUN_NAME)
            print("\n\033[1;32m[✅ 成功] 已取消开机自启动配置。\033[0m")
        except Exception as e:
            print(f"\n\033[1;31m[❌ 失败] 取消自启失败: {e}\033[0m")
    else:
        try:
            cmd_val = f'"{PYTHONW_BIN}" "{VOICE_SCRIPT}"'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, REG_RUN_NAME, 0, winreg.REG_SZ, cmd_val)
            print("\n\033[1;32m[✅ 成功] 已开启开机自启！计算机开机时将在后台静默运行。\033[0m")
        except Exception as e:
            print(f"\n\033[1;31m[❌ 失败] 配置自启失败: {e}\033[0m")
    time.sleep(1.2)

def start_background():
    """后台静默启动服务"""
    is_running, port = check_service_status()
    if is_running:
        print(f"\n\033[1;33m[⚠️ 提示] 服务已在运行中 (端口: {port})，无需重复启动。\033[0m")
        time.sleep(1)
        return

    print("\n\033[1;36m[🚀 正在后台静默启动 Voice Input Bridge...]\033[0m")
    try:
        # DETACHED_PROCESS + CREATE_NO_WINDOW
        flags = 0x00000008 | 0x08000000
        subprocess.Popen(
            [PYTHONW_BIN, VOICE_SCRIPT],
            cwd=PROJECT_DIR,
            creationflags=flags,
            close_fds=True
        )
        time.sleep(1)
        is_running_now, p = check_service_status()
        if is_running_now:
            print(f"\033[1;32m[✅ 成功] 服务已在后台静默运行 (端口: {p})！\033[0m")
        else:
            print("\033[1;33m[ℹ️ 提示] 服务已启动，正在初始化网络接口...\033[0m")
    except Exception as e:
        print(f"\033[1;31m[❌ 失败] 启动异常: {e}\033[0m")
    time.sleep(1)

def stop_service():
    """停止服务"""
    print("\n\033[1;33m[🛑 正在停止 Voice Input Bridge 服务...]\033[0m")
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'voice_input\\.py' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(0.8)
        running, _ = check_service_status()
        if not running:
            print("\033[1;32m[✅ 成功] 服务已停止。\033[0m")
        else:
            print("\033[1;33m[⚠️ 提示] 服务可能仍在清理退出...\033[0m")
    except Exception as e:
        print(f"\033[1;31m[❌ 失败] 停止服务出错: {e}\033[0m")
    time.sleep(1)

def restart_service():
    stop_service()
    start_background()

def install_qrcode():
    clear_screen()
    print("=" * 60)
    print("  📦 安装 / 修复终端二维码库 (qrcode)")
    print("=" * 60)
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "qrcode"], check=True)
        print("\n\033[1;32m[✅ 完成] 二维码库准备就绪！\033[0m")
    except Exception as e:
        print(f"\n\033[1;31m[❌ 错误] 安装失败: {e}\033[0m")
    input("\n按回车键返回菜单...")

def show_firewall_tips():
    clear_screen()
    print("=" * 60)
    print("  🛡️  局域网防火墙放行指引")
    print("=" * 60)
    print("如果手机连接提示“无法访问此网站”：\n")
    print("1. 确保手机和电脑连接的是同一个 Wi-Fi (或手机热点)；")
    print("2. 检查电脑网络连接是否为“专用网络”(不是公用网络)；")
    print("3. 若 Windows Defender 防火墙拦截，可以管理员身份在 CMD 运行：\n")
    print('   netsh advfirewall firewall add rule name="VoiceInputBridge" dir=in action=allow protocol=TCP localport=58002,53317\n')
    print("=" * 60)
    input("\n按回车键返回菜单...")

def read_menu_key():
    import msvcrt
    key = msvcrt.getwch()
    if key in ("\x00", "\xe0"):
        return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
    return {"\r": "enter", "q": "quit", "Q": "quit", "\x03": "quit",
            "k": "up", "j": "down"}.get(key, "")


def run_foreground():
    clear_screen()
    print("服务运行中，按 Ctrl+C 返回控制中心。\n")
    try:
        subprocess.run([sys.executable, VOICE_SCRIPT])
    except KeyboardInterrupt:
        pass


def main_loop():
    if sys.platform != "win32":
        os.execvp("bash", ["bash", os.path.join(PROJECT_DIR, "menu.sh")])
    sys.path.insert(0, PROJECT_DIR)
    import voice_input

    actions = [
        ("前台启动", "查看实时日志与扫码连接", run_foreground),
        ("后台启动", "静默接收手机文字", start_background),
        ("停止服务", "关闭正在运行的接收服务", stop_service),
        ("重启服务", "载入最新代码并重新启动", restart_service),
        ("切换开机自启", "管理当前用户开机启动配置", toggle_autostart),
        ("安装二维码支持", "安装或修复 qrcode", install_qrcode),
        ("防火墙指引", "排查手机无法连接的问题", show_firewall_tips),
        ("退出控制台", "后台服务继续运行", None),
    ]
    selected = 0
    while True:
        running, port = check_service_status()
        ips = voice_input.get_lan_ips()
        ip = ips[0] if ips else "127.0.0.1"
        auto = "已启用" if is_autostart_enabled() else "未启用"
        lines = []
        lines.append("\033[1;36m  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m")
        lines.append("\033[1m  VOICE INPUT\033[0m  \033[90m手机语音 · 直达电脑\033[0m")
        lines.append("\033[1;36m  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m")
        status = "\033[32m● 运行中" if running else "\033[33m○ 已停止"
        lines.append(f"  {status}\033[0m  端口 {port} · 自启 {auto}")
        lines.append(f"  \033[36mhttp://{ip}:{port}\033[0m\n")
        for index, (label, _, _) in enumerate(actions):
            if index == selected:
                lines.append(f"  \033[1;36m❯ {label}\033[0m")
            else:
                lines.append(f"    {label}")
        lines.append(f"\n  \033[90m{actions[selected][1]}\033[0m")
        lines.append("\033[36m  ──────────────────────────────────────────────\033[0m")
        lines.append("  ↑ ↓ 选择   Enter 执行   Q 退出")
        sys.stdout.write("\033[?25l\033[H" + "\033[K\n".join(lines) + "\033[K\033[J\033[s")
        sys.stdout.flush()
        while True:
            previous = selected
            key = read_menu_key()
            if key == "up":
                selected = (selected - 1) % len(actions)
            elif key == "down":
                selected = (selected + 1) % len(actions)
            elif key == "quit":
                return
            elif key == "enter":
                action = actions[selected][2]
                if action is None:
                    return
                sys.stdout.write("\033[?25h\n")
                sys.stdout.flush()
                action()
                break
            if selected != previous:
                changes = []
                for index in (previous, selected):
                    label = actions[index][0]
                    line = f"  \033[1;36m❯ {label}\033[0m" if index == selected else f"    {label}"
                    changes.append(f"\033[{7 + index};1H{line}\033[K")
                changes.append(f"\033[{8 + len(actions)};1H  \033[90m{actions[selected][1]}\033[0m\033[K")
                sys.stdout.write("".join(changes) + "\033[u")
                sys.stdout.flush()


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[0m\033[?25h\n")
        sys.stdout.flush()
