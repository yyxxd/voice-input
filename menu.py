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

def main_loop():
    # 导入主脚本获取 IP 探测方法
    sys.path.insert(0, PROJECT_DIR)
    import voice_input

    while True:
        clear_screen()
        is_running, active_port = check_service_status()
        lan_ips = voice_input.get_lan_ips()
        primary_ip = lan_ips[0] if lan_ips else "127.0.0.1"
        autostart_str = "\033[1;32m已启用\033[0m" if is_autostart_enabled() else "\033[90m未启用\033[0m"

        print("=" * 60)
        print("  🎙️  Voice Input Bridge 控制中心 (Windows)")
        print("=" * 60)
        if is_running:
            print(f"  ● 运行状态: \033[1;32m运行中\033[0m (端口: {active_port})    ● 开机自启: {autostart_str}")
            print(f"  📱 手机直连: \033[1;36mhttp://{primary_ip}:{active_port}\033[0m")
        else:
            print(f"  ○ 运行状态: \033[1;31m已停止\033[0m                  ● 开机自启: {autostart_str}")
            print(f"  📱 手机直连: \033[90mhttp://{primary_ip}:58002 (服务未启动)\033[0m")

        print("-" * 60)
        print("  [1] 前台启动 (查看实时日志与扫码连接)")
        print("  [2] 后台静默启动 (无黑框窗口，后台常驻)")
        print("  [3] 停止运行服务")
        print("  [4] 重启服务")
        print(f"  [5] 切换开机自启动")
        print("  [6] 安装/修复二维码支持库 (qrcode)")
        print("  [7] 查看局域网防火墙放行指引")
        print("  [0] 退出")
        print("=" * 60)

        choice = input("请输入选项 [0-7]: ").strip()
        if choice == "1":
            clear_screen()
            print("\033[1;36m[🚀 正在前台启动 Voice Input Bridge...]\033[0m")
            print("💡 提示: 按 Ctrl+C 可停止运行并返回控制中心\n")
            try:
                subprocess.run([sys.executable, VOICE_SCRIPT])
            except KeyboardInterrupt:
                pass
            print("\n服务已退出。")
            time.sleep(1)
        elif choice == "2":
            start_background()
        elif choice == "3":
            stop_service()
        elif choice == "4":
            restart_service()
        elif choice == "5":
            toggle_autostart()
        elif choice == "6":
            install_qrcode()
        elif choice == "7":
            show_firewall_tips()
        elif choice == "0":
            print("\n👋 祝你使用愉快，再见！")
            sys.exit(0)

if __name__ == "__main__":
    main_loop()
