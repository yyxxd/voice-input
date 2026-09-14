#!/usr/bin/env python3
"""
Voice Input Bridge (Linux / Windows 双平台通用)
手机语音输入直达电脑：双端互联，利用手机成熟语音输入法，同步至当前焦点输入框与系统剪贴板。
"""
import os
import time
import sys
import json
import signal
import socket
import argparse
import platform
import subprocess
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# 在无控制台的后台模式 (如 Windows pythonw.exe) 下，将 None 重定向至 devnull 防止写入崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

# ---------------------------------------------------------
# Windows Win32 API 辅助定义 (纯 Python 标准库 ctypes)
# ---------------------------------------------------------
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    import winsound

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    # 函数签名精确定义（保证 64 位系统指针安全，杜绝截断崩溃）
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.argtypes = []
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.argtypes = []
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    _user32.keybd_event.restype = None

    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.restype = wintypes.BOOL
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    # 尝试开启 Windows 控制台虚拟终端支持（VT100 ANSI 颜色高亮）
    try:
        h_stdout = _kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if _kernel32.GetConsoleMode(h_stdout, ctypes.byref(mode)):
            _kernel32.SetConsoleMode(h_stdout, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass

def win32_set_clipboard(text: str) -> bool:
    """Windows 原生 Unicode (UTF-16LE) 剪贴板写入，杜绝中文乱码与字符截断"""
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    # 短暂重试，避免其他应用短暂独占剪贴板导致写入失败
    opened = False
    for _ in range(5):
        if _user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.02)
    if not opened:
        return False

    try:
        _user32.EmptyClipboard()
        data = text.encode("utf-16le") + b"\x00\x00"
        h_mem = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_mem:
            return False
        p_mem = _kernel32.GlobalLock(h_mem)
        if not p_mem:
            _kernel32.GlobalFree(h_mem)
            return False
        ctypes.memmove(p_mem, data, len(data))
        _kernel32.GlobalUnlock(h_mem)
        if not _user32.SetClipboardData(CF_UNICODETEXT, h_mem):
            _kernel32.GlobalFree(h_mem)
            return False
        return True
    finally:
        _user32.CloseClipboard()

def win32_get_active_window_info():
    """获取 Windows 当前激活焦点窗口的 (类名, 进程名)"""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return "", ""

    cls_buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, cls_buf, 256)
    cls_name = cls_buf.value

    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc_name = ""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h_proc = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if h_proc:
        try:
            path_buf = ctypes.create_unicode_buffer(1024)
            path_len = wintypes.DWORD(1024)
            if _kernel32.QueryFullProcessImageNameW(h_proc, 0, path_buf, ctypes.byref(path_len)):
                proc_name = os.path.basename(path_buf.value).lower()
        finally:
            _kernel32.CloseHandle(h_proc)

    return cls_name, proc_name

def win32_paste(is_terminal: bool):
    """自适应模拟粘贴：终端模拟 Ctrl+Shift+V，普通窗口模拟 Ctrl+V"""
    VK_CONTROL = 0x11
    VK_SHIFT = 0x10
    VK_V = 0x56
    KEYEVENTF_KEYUP = 0x0002

    if is_terminal:
        # 终端 / SSH 环境：模拟 Ctrl + Shift + V
        _user32.keybd_event(VK_CONTROL, 0, 0, 0)
        _user32.keybd_event(VK_SHIFT, 0, 0, 0)
        _user32.keybd_event(VK_V, 0, 0, 0)
        time.sleep(0.01)
        _user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        _user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
        _user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    else:
        # 普通日常软件：模拟 Ctrl + V
        _user32.keybd_event(VK_CONTROL, 0, 0, 0)
        _user32.keybd_event(VK_V, 0, 0, 0)
        time.sleep(0.01)
        _user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        _user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

def win32_press_key(key: str = "Return"):
    """模拟单次键盘敲击 (Return / Enter 等)"""
    KEYEVENTF_KEYUP = 0x0002
    vk_map = {
        "return": 0x0D,
        "enter": 0x0D,
        "backspace": 0x08,
        "space": 0x20,
        "tab": 0x09,
        "escape": 0x1B
    }
    vk = vk_map.get(key.lower(), 0x0D)
    _user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.01)
    _user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    return {"ok": True}

# ---------------------------------------------------------
# 1. 系统环境与局域网 IP 探测
# ---------------------------------------------------------

def setup_wayland_env():
    """确保在 SSH、systemd 用户服务或无图形子 shell 下也能正确定位 Wayland 与 D-Bus 会话 (仅 Linux)"""
    if not IS_LINUX:
        return os.environ.copy()
    env = os.environ.copy()
    try:
        uid = os.getuid()
    except AttributeError:
        return env

    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

    runtime_path = Path(env["XDG_RUNTIME_DIR"])
    if runtime_path.exists():
        if "WAYLAND_DISPLAY" not in env:
            sockets = [
                s.name for s in runtime_path.glob("wayland-*")
                if not s.name.endswith(".lock")
            ]
            if sockets:
                sockets.sort()
                env["WAYLAND_DISPLAY"] = sockets[0]

        if "HYPRLAND_INSTANCE_SIGNATURE" not in env:
            hypr_dir = runtime_path / "hypr"
            if hypr_dir.exists():
                hypr_instances = [d for d in hypr_dir.glob("*") if d.is_dir()]
                if hypr_instances:
                    hypr_instances.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    env["HYPRLAND_INSTANCE_SIGNATURE"] = hypr_instances[0].name

        if "DBUS_SESSION_BUS_ADDRESS" not in env:
            bus_sock = runtime_path / "bus"
            if bus_sock.exists():
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_sock}"

    return env

APP_ENV = setup_wayland_env()

def get_lan_ips():
    """获取本机有效局域网 IP（跨平台通用，优先排除虚拟网卡并优先选取常用物理内网段）"""
    ips = []

    # 1. 通用 UDP socket 出口探测（双平台秒级生效，优先获取默认网卡 IP）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        primary_ip = s.getsockname()[0]
        s.close()
        if primary_ip and not primary_ip.startswith("127."):
            ips.append(primary_ip)
    except Exception:
        pass

    # 2. 主机名接口枚举（双平台通用）
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    # 3. Linux 专属补充探测
    if IS_LINUX:
        try:
            output = subprocess.check_output(
                ["ip", "-4", "-o", "addr", "show"], text=True
            )
            for line in output.strip().splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    dev = parts[1]
                    if any(dev.startswith(prefix) for prefix in ["docker", "veth", "br-", "virbr", "tun", "tap", "meta"]):
                        continue
                    ip = parts[3].split("/")[0]
                    if not ip.startswith("127.") and ip not in ips:
                        ips.append(ip)
        except Exception:
            pass

    # 过滤 APIPA (169.254.x.x) 和回环
    filtered = [ip for ip in ips if not ip.startswith("169.254.") and not ip.startswith("127.")]
    if not filtered:
        filtered = ["127.0.0.1"]

    # 优先级排序：192.168.x 优先 > 10.x 局域网 > 172.16-31.x > 其它
    def ip_sort_key(ip: str):
        if ip.startswith("192.168."):
            return (0, ip)
        if ip.startswith("10.") and not ip.startswith("10.222."):
            return (1, ip)
        if ip.startswith("172."):
            return (2, ip)
        return (3, ip)

    sorted_ips = sorted(filtered, key=ip_sort_key)

    # 去重保留顺序
    seen = set()
    result = []
    for ip in sorted_ips:
        if ip not in seen:
            seen.add(ip)
            result.append(ip)
    return result

# ---------------------------------------------------------
# 2. 文本注入与剪贴板控制
# ---------------------------------------------------------

# Linux 常见主流终端模拟器的 class / app_id
KNOWN_TERMINAL_CLASSES = {
    "alacritty", "kitty", "foot", "footclient", "ghostty", "wezterm", "wezterm-gui",
    "gnome-terminal", "gnome-terminal-server", "org.gnome.terminal", "konsole",
    "xterm", "uxterm", "rxvt", "urxvt", "terminator", "tilix", "xfce4-terminal",
    "lxterminal", "mate-terminal", "sakura", "termite", "rio", "contour",
    "hyper", "tabby", "warp", "blackbox", "ptyxis", "deepin-terminal"
}

# Windows 常见终端进程与窗口类名
KNOWN_WINDOWS_TERMINALS = {
    "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe",
    "alacritty.exe", "kitty.exe", "wezterm-gui.exe", "ghostty.exe", "putty.exe",
    "xshell.exe", "mobaxterm.exe", "mintty.exe", "hyper.exe", "tabby.exe", "warp.exe",
    "git-bash.exe", "bash.exe", "wsl.exe"
}
KNOWN_WINDOWS_TERMINAL_CLASSES = {
    "ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PuTTY", "VirtualConsoleClass"
}

def get_active_window_class() -> str:
    """探测当前处于焦点的活动窗口 class / app_id (Linux)"""
    if IS_WINDOWS:
        cls_name, _ = win32_get_active_window_info()
        return cls_name

    # Linux 优先尝试 Hyprland (hyprctl)
    try:
        proc = subprocess.run(
            ["hyprctl", "activewindow", "-j"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=APP_ENV,
            timeout=0.3
        )
        if proc.returncode == 0 and proc.stdout:
            data = json.loads(proc.stdout.decode("utf-8", errors="ignore"))
            cls = data.get("class") or data.get("initialClass") or ""
            if cls:
                return str(cls).strip()
    except Exception:
        pass

    # Linux 尝试 Sway (swaymsg)
    try:
        proc = subprocess.run(
            ["swaymsg", "-t", "get_tree"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=APP_ENV,
            timeout=0.4
        )
        if proc.returncode == 0 and proc.stdout:
            tree = json.loads(proc.stdout.decode("utf-8", errors="ignore"))
            def find_focused(node):
                if node.get("focused"):
                    return node.get("app_id") or (node.get("window_properties") or {}).get("class") or ""
                for child in node.get("nodes", []) + node.get("floating_nodes", []):
                    res = find_focused(child)
                    if res:
                        return res
                return ""
            focused_cls = find_focused(tree)
            if focused_cls:
                return str(focused_cls).strip()
    except Exception:
        pass

    return ""

def is_terminal_window(window_class: str = "", proc_name: str = "") -> bool:
    """判定指定窗口是否为终端模拟器"""
    if IS_WINDOWS:
        if proc_name and proc_name.lower() in KNOWN_WINDOWS_TERMINALS:
            return True
        if window_class and window_class in KNOWN_WINDOWS_TERMINAL_CLASSES:
            return True
        if "terminal" in proc_name.lower() or "term" in proc_name.lower():
            return True
        return False
    else:
        if not window_class:
            return False
        cls_lower = window_class.lower()
        if cls_lower in KNOWN_TERMINAL_CLASSES:
            return True
        if "terminal" in cls_lower:
            return True
        if any(cls_lower.startswith(p) for p in ["alacritty", "kitty", "foot", "ghostty", "wezterm", "term-"]):
            return True
        if any(cls_lower.endswith(s) for s in ["-terminal", "-term", ".terminal"]):
            return True
        return False

def inject_text(text: str, mode: str = "auto"):
    """
    双通道无损注入逻辑：
    1. 无条件写入系统剪贴板确保内容物理不丢失
    2. 如果 mode == 'auto'：
       - 智能检测活动窗口是否为终端
       - 终端触发 Ctrl+Shift+V (完美兼容 Linux 终端与 Windows SSH/CMD)
       - 普通应用触发 Ctrl+V (兼容微信、记事本、浏览器等)
    """
    result = {
        "ok": False,
        "copied": False,
        "typed": False,
        "target_is_terminal": False,
        "error": None
    }

    if IS_WINDOWS:
        # Windows 分支
        if not win32_set_clipboard(text):
            result["error"] = "写入 Windows 剪贴板失败"
            return result
        result["copied"] = True

        if mode == "auto":
            cls_name, proc_name = win32_get_active_window_info()
            is_term = is_terminal_window(cls_name, proc_name)
            result["target_is_terminal"] = is_term
            try:
                win32_paste(is_terminal=is_term)
                result["typed"] = True
            except Exception as e:
                result["error"] = f"Windows 模拟按键失败: {e}"
                return result
        result["ok"] = True
        return result

    # Linux 分支 (Wayland wl-copy & wtype)
    try:
        proc_copy = subprocess.run(
            ["wl-copy"],
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=APP_ENV,
            check=True,
            timeout=2
        )
        result["copied"] = True
    except subprocess.TimeoutExpired:
        result["copied"] = True
    except subprocess.CalledProcessError:
        result["error"] = "wl-copy 写入剪贴板失败"
        return result
    except FileNotFoundError:
        result["error"] = "系统中未安装 wl-copy，请确认已安装 wl-clipboard"
        return result

    if mode == "auto":
        active_cls = get_active_window_class()
        if not active_cls:
            result["typed"] = False
            result["ok"] = True
            return result
        is_term = is_terminal_window(active_cls)
        result["target_is_terminal"] = is_term

        if is_term:
            key_cmd = ["wtype", "-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"]
        else:
            key_cmd = ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"]

        try:
            proc_type = subprocess.run(
                key_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=APP_ENV,
                check=True,
                timeout=2
            )
            result["typed"] = True
        except subprocess.CalledProcessError as e:
            result["error"] = f"wtype 错误: {e.stderr.decode('utf-8', errors='ignore')}"
            return result
        except FileNotFoundError:
            result["error"] = "系统中未安装 wtype"
            return result

    result["ok"] = True
    return result

def press_key(key: str = "Return"):
    """模拟单次物理按键敲击 (如 Return / Enter / BackSpace 等)"""
    if IS_WINDOWS:
        return win32_press_key(key)

    try:
        subprocess.run(
            ["wtype", "-k", key],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=APP_ENV,
            check=True,
            timeout=2
        )
        return {"ok": True}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": f"wtype 错误: {e.stderr.decode('utf-8', errors='ignore')}"}
    except FileNotFoundError:
        return {"ok": False, "error": "系统中未安装 wtype"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------------------------------------------------------
# 2.1 电脑端即时音频反馈 (统一专属轻微机械轴微敲击音)
# ---------------------------------------------------------

ENABLE_SOUND = True
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SOUND_FILE = os.path.join(PROJECT_DIR, "pop.wav")

def ensure_sound_file():
    """若声音文件缺失，使用标准库 wave 自动合成 45ms 轻微柔和的按键音"""
    if not os.path.exists(SOUND_FILE):
        try:
            import wave, struct, math
            sample_rate = 44100
            duration = 0.045
            num_samples = int(sample_rate * duration)
            samples = []
            for i in range(num_samples):
                t = i / sample_rate
                freq = 350 + 650 * math.exp(-t * 90)
                env = min(1.0, t / 0.003) * math.exp(-t * 65)
                val = (math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t)) * env * 0.25
                sample_val = int(max(-32767, min(32767, val * 32767)))
                samples.append(struct.pack('<h', sample_val))
            with wave.open(SOUND_FILE, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(b''.join(samples))
        except Exception:
            pass

ensure_sound_file()

def play_feedback_sound():
    """跨平台统一播放轻柔的按键上屏音效 (杜绝 Windows 系统刺耳的叮咚通知声)"""
    global ENABLE_SOUND
    if not ENABLE_SOUND or not os.path.exists(SOUND_FILE):
        return

    if IS_WINDOWS:
        try:
            winsound.PlaySound(SOUND_FILE, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            pass
        return

    # Linux 分支: 优先使用 pw-play (PipeWire)，回退 paplay 或 aplay
    player = "pw-play" if os.path.exists("/usr/bin/pw-play") else ("paplay" if os.path.exists("/usr/bin/paplay") else ("aplay" if os.path.exists("/usr/bin/aplay") else None))
    if not player:
        return
    try:
        subprocess.Popen(
            [player, SOUND_FILE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=APP_ENV
        )
    except Exception:
        pass

# ---------------------------------------------------------
# 2.2 电脑端桌面通知反馈 (仅在存入剪贴板时提示)
# ---------------------------------------------------------

def send_desktop_notification(title: str, body: str):
    """异步非阻塞发送系统桌面通知"""
    display_body = body if len(body) <= 120 else f"{body[:117]}..."

    if IS_WINDOWS:
        # Windows 10/11 原生 Toast 弹窗通知 (PowerShell 后台异步调用)
        safe_title = title.replace('"', '`"').replace("'", "''")
        safe_body = display_body.replace('"', '`"').replace("'", "''")
        ps_cmd = (
            f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
            f"$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            f"$nodes = $t.GetElementsByTagName('text'); "
            f"$nodes.Item(0).AppendChild($t.CreateTextNode('{safe_title}')) > $null; "
            f"$nodes.Item(1).AppendChild($t.CreateTextNode('{safe_body}')) > $null; "
            f"$toast = [Windows.UI.Notifications.ToastNotification]::new($t); "
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Voice Input Bridge').Show($toast);"
        )
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            pass
        return

    try:
        subprocess.Popen(
            [
                "notify-send",
                "-a", "Voice Input",
                "-i", "edit-copy",
                "-t", "2500",
                title,
                display_body
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=APP_ENV
        )
    except Exception:
        pass
# 3. 手机端 Web 界面 HTML (纯单文件嵌入)
# ---------------------------------------------------------

MOBILE_HTML = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover, interactive-widget=resizes-content" />
  <meta name="theme-color" content="#F9F8F6" id="themeColorMeta" />
  <title>语音直达电脑</title>
  <style>
    /* =========================================================
       Design Tokens & Dual Theme System (UI/UX Pro Max 规范)
       ========================================================= */
    :root[data-theme="light"] {
      --canvas: #F9F8F6;
      --surface: #FFFFFF;
      --surface-subtle: #F2EFE9;
      --border: rgba(0, 0, 0, 0.08);
      --border-strong: rgba(0, 0, 0, 0.16);
      --text-primary: #21201D;
      --text-secondary: #57534D;
      --text-muted: #8C877D;
      --accent: #21201D;
      --accent-text: #FFFFFF;
      --accent-hover: #3A3834;
      --success: #16A34A;
      --danger: #DC2626;
      --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.04);
      --shadow-md: 0 6px 20px -2px rgba(0, 0, 0, 0.08);
      --glass-bg: rgba(249, 248, 246, 0.88);
    }

    :root[data-theme="dark"] {
      --canvas: #0F1117;
      --surface: #181B24;
      --surface-subtle: #12141C;
      --border: rgba(255, 255, 255, 0.08);
      --border-strong: rgba(255, 255, 255, 0.18);
      --text-primary: #F0F3F8;
      --text-secondary: #9AA5B8;
      --text-muted: #647087;
      --accent: #3B82F6;
      --accent-text: #FFFFFF;
      --accent-hover: #2563EB;
      --success: #10B981;
      --danger: #EF4444;
      --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
      --shadow-md: 0 8px 24px -4px rgba(0, 0, 0, 0.5);
      --glass-bg: rgba(15, 17, 23, 0.88);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
    }

    html, body {
      height: 100%;
      height: 100dvh;
      overflow: hidden;
      background-color: var(--canvas);
      color: var(--text-primary);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      transition: background-color 0.25s ease, color 0.25s ease;
    }

    .app-container {
      display: flex;
      flex-direction: column;
      height: 100%;
      padding-top: env(safe-area-inset-top, 0px);
      position: relative;
    }

    /* 顶部毛玻璃导航 */
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 12px 18px;
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      background: var(--glass-bg);
      border-bottom: 1px solid var(--border);
      z-index: 20;
      flex-shrink: 0;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .brand-icon {
      width: 32px;
      height: 32px;
      border-radius: 9px;
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--accent);
    }
    .brand-text {
      font-size: 15px;
      font-weight: 600;
      letter-spacing: -0.02em;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .theme-toggle-btn {
      width: 34px;
      height: 34px;
      border-radius: 9px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      transition: all 0.2s;
    }
    .theme-toggle-btn:active {
      transform: scale(0.92);
    }

    .status-pill {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 5px 10px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 999px;
      font-size: 12px;
      color: var(--text-secondary);
      font-weight: 500;
    }
    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--success);
      box-shadow: 0 0 6px var(--success);
    }
    .status-dot.offline {
      background: var(--danger);
      box-shadow: 0 0 6px var(--danger);
    }

    /* 中部滚动内容区 */
    main {
      flex: 1;
      overflow-y: auto;
      padding: 16px 18px;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }

    .input-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 16px;
      box-shadow: var(--shadow-sm);
      display: flex;
      flex-direction: column;
      position: relative;
      transition: border-color 0.2s, box-shadow 0.2s;
    }
    .input-card:focus-within {
      border-color: var(--border-strong);
      box-shadow: var(--shadow-md);
    }

    .textarea-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 12px;
      color: var(--text-muted);
    }

    textarea {
      width: 100%;
      min-height: 130px;
      background: transparent;
      border: none;
      outline: none;
      color: var(--text-primary);
      font-size: 17px;
      line-height: 1.7;
      letter-spacing: -0.01em;
      resize: none;
      font-family: inherit;
    }
    textarea::placeholder {
      color: var(--text-muted);
      opacity: 0.7;
    }

    .textarea-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 6px;
      font-size: 11px;
      color: var(--text-muted);
    }

    /* 历史记录 */
    .history-section {
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin-bottom: 8px;
    }
    .history-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 13px;
      font-weight: 600;
      color: var(--text-secondary);
      padding: 0 4px;
    }
    .history-header-actions {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .clear-history-link {
      background: none;
      border: none;
      color: var(--text-muted);
      font-size: 12px;
      cursor: pointer;
      padding: 2px 4px;
    }
    .clear-history-link:active {
      color: var(--danger);
    }
    .history-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .history-item {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 14px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      cursor: pointer;
      transition: background-color 0.15s, transform 0.1s;
    }
    .history-item:active {
      background: var(--surface-subtle);
      transform: scale(0.99);
    }
    .history-item-body {
      font-size: 14px;
      color: var(--text-primary);
      line-height: 1.5;
      word-break: break-all;
    }
    .history-item-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 11px;
      color: var(--text-muted);
    }
    .history-actions {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .history-action-btn {
      background: none;
      border: none;
      color: var(--accent);
      font-size: 11px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 3px;
      padding: 2px;
    }

    /* 底部悬浮吸附操作区 (Docked Action Bar) - 贴合软键盘与大拇指黄金区 */
    .docked-footer {
      background: var(--glass-bg);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border-top: 1px solid var(--border);
      padding: 12px 18px calc(12px + env(safe-area-inset-bottom, 0px)) 18px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      z-index: 30;
      box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.03);
      flex-shrink: 0;
    }

    .footer-tools {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }

    /* 分段模式控制器 */
    .segmented-control {
      display: flex;
      background: var(--surface-subtle);
      padding: 3px;
      border-radius: 10px;
      border: 1px solid var(--border);
      flex: 1;
    }
    .segment-btn {
      flex: 1;
      padding: 7px 10px;
      border: none;
      background: transparent;
      color: var(--text-secondary);
      font-size: 12px;
      font-weight: 500;
      border-radius: 7px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 5px;
      transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .segment-btn.active {
      background: var(--surface);
      color: var(--text-primary);
      font-weight: 600;
      box-shadow: var(--shadow-sm);
    }

    .clear-btn {
      padding: 8px 12px;
      border-radius: 10px;
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      font-size: 12px;
      display: flex;
      align-items: center;
      gap: 5px;
      cursor: pointer;
      flex-shrink: 0;
    }
    .clear-btn:active {
      background: var(--border);
    }

    /* 大尺寸高触达发送按钮 */
    .send-btn {
      width: 100%;
      height: 48px;
      background: var(--accent);
      color: var(--accent-text);
      border: none;
      border-radius: 12px;
      font-size: 16px;
      font-weight: 600;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      cursor: pointer;
      box-shadow: 0 3px 12px rgba(0, 0, 0, 0.15);
      transition: transform 0.1s ease, opacity 0.2s, background-color 0.2s, box-shadow 0.2s;
    }
    .send-btn:active {
      transform: scale(0.98);
      opacity: 0.92;
    }
    .send-btn.success {
      background: var(--success) !important;
      color: #FFFFFF !important;
      box-shadow: 0 3px 12px rgba(22, 163, 74, 0.35);
    }
    .send-btn:disabled {
      opacity: 0.4;
      cursor: not-allowed;
      transform: none;
    }

    .footer-hints {
      display: flex;
      justify-content: center;
      align-items: center;
      gap: 20px;
      padding: 2px 4px;
      font-size: 11px;
      color: var(--text-muted);
    }
    .hint-checkbox {
      display: flex;
      align-items: center;
      gap: 5px;
      cursor: pointer;
      user-select: none;
    }
    .hint-checkbox input[type="checkbox"] {
      cursor: pointer;
      accent-color: var(--accent);
    }

    /* 顶部微感悬浮 Toast */
    .toast-container {
      position: fixed;
      top: 50px;
      left: 50%;
      transform: translateX(-50%) translateY(-20px);
      background: var(--text-primary);
      color: var(--canvas);
      padding: 8px 16px;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 500;
      box-shadow: var(--shadow-md);
      opacity: 0;
      pointer-events: none;
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      z-index: 100;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .toast-container.show {
      transform: translateX(-50%) translateY(0);
      opacity: 1;
    }
  </style>
</head>
<body>
  <div class="app-container">
    <!-- 顶部毛玻璃导航 -->
    <header>
      <div class="brand">
        <div class="brand-icon">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
            <line x1="12" x2="12" y1="19" y2="22"/>
          </svg>
        </div>
        <div class="brand-text">语音直达电脑</div>
      </div>

      <div class="header-actions">
        <!-- 浅色/深色主题切换 -->
        <button class="theme-toggle-btn" id="themeBtn" title="切换浅色/深色模式" onclick="toggleTheme()">
          <svg id="themeIcon" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="4"/>
            <path d="M12 2v2"/><path d="M12 20v2"/>
            <path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/>
            <path d="M2 12h2"/><path d="M20 12h2"/>
            <path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>
          </svg>
        </button>

        <div class="status-pill">
          <span class="status-dot" id="statusDot"></span>
          <span id="statusText">已连接</span>
        </div>
      </div>
    </header>

    <!-- 中间滚动内容区 -->
    <main>
      <!-- 沉浸输入卡片 -->
      <div class="input-card">
        <div class="textarea-header">
          <span>输入区</span>
          <span id="charCount">0 字符</span>
        </div>
        <textarea
          id="textInput"
          placeholder="调出手机输入法，按住语音键说话..."
          autofocus
        ></textarea>
        <div class="textarea-footer">
          <span>建议长按输入法语音键连说</span>
        </div>
      </div>

      <!-- 历史记录区域 -->
      <div class="history-section">
        <div class="history-header">
          <span>最近发送历史</span>
          <div class="history-header-actions">
            <span id="historyCount">0 条</span>
            <button class="clear-history-link" onclick="clearAllHistory()" title="清空全部历史">清空</button>
          </div>
        </div>
        <div class="history-list" id="historyList"></div>
      </div>
    </main>

    <!-- 底部悬浮操作底栏 (位于单手拇指黄金区) -->
    <footer class="docked-footer">
      <div class="footer-tools">
        <!-- 模式分段控制器 -->
        <div class="segmented-control">
          <button class="segment-btn active" id="modeAutoBtn" onclick="setMode('auto')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg>
            直接上屏
          </button>
          <button class="segment-btn" id="modeClipBtn" onclick="setMode('clipboard_only')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/></svg>
            仅剪贴板
          </button>
        </div>

        <button class="clear-btn" onclick="clearInput()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
          清空
        </button>
      </div>

      <!-- 主发送按钮 -->
      <button class="send-btn" id="sendBtn" onclick="handleSend()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
        </svg>
        <span>发送至电脑</span>
      </button>

      <div class="footer-hints">
        <label class="hint-checkbox" title="发送成功后清空手机输入框">
          <input type="checkbox" id="autoClearCheck" checked />
          <span>自动清空</span>
        </label>
        <label class="hint-checkbox" title="发送成功后在电脑端自动按下回车键">
          <input type="checkbox" id="autoEnterCheck" />
          <span>自动回车</span>
        </label>
      </div>
    </footer>
  </div>

  <!-- 轻量悬浮 Toast -->
  <div class="toast-container" id="toast">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
    <span id="toastMsg">操作成功</span>
  </div>

  <script>
    let currentMode = 'auto';
    const textInput = document.getElementById('textInput');
    const charCount = document.getElementById('charCount');
    const sendBtn = document.getElementById('sendBtn');
    const toast = document.getElementById('toast');
    const toastMsg = document.getElementById('toastMsg');
    const historyList = document.getElementById('historyList');
    const historyCount = document.getElementById('historyCount');
    const autoClearCheck = document.getElementById('autoClearCheck');
    const autoEnterCheck = document.getElementById('autoEnterCheck');
    const statusDot = document.getElementById('statusDot');
    const statusText = document.getElementById('statusText');

    // 1. 主题初始化与切换 (支持持久化与系统匹配)
    function initTheme() {
      const saved = localStorage.getItem('voice_theme');
      const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
      const theme = saved || (prefersDark ? 'dark' : 'light');
      applyTheme(theme, false);
    }

    function applyTheme(theme) {
      document.documentElement.setAttribute('data-theme', theme);
      localStorage.setItem('voice_theme', theme);
      const metaColor = theme === 'light' ? '#F9F8F6' : '#0F1117';
      document.getElementById('themeColorMeta').setAttribute('content', metaColor);

      const icon = document.getElementById('themeIcon');
      if (theme === 'dark') {
        icon.innerHTML = '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>';
      } else {
        icon.innerHTML = '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>';
      }
    }

    function toggleTheme() {
      const current = document.documentElement.getAttribute('data-theme') || 'light';
      applyTheme(current === 'light' ? 'dark' : 'light');
    }

    // 初始化偏好设置 (自动清空 / 自动回车)
    function initPreferences() {
      const savedClear = localStorage.getItem('voice_auto_clear');
      if (savedClear !== null) {
        autoClearCheck.checked = savedClear === 'true';
      }
      const savedEnter = localStorage.getItem('voice_auto_enter');
      if (savedEnter !== null) {
        autoEnterCheck.checked = savedEnter === 'true';
      }
    }

    autoClearCheck.addEventListener('change', () => {
      localStorage.setItem('voice_auto_clear', autoClearCheck.checked);
    });
    autoEnterCheck.addEventListener('change', () => {
      localStorage.setItem('voice_auto_enter', autoEnterCheck.checked);
    });

    // 2. 模式切换
    function setMode(mode) {
      currentMode = mode;
      document.getElementById('modeAutoBtn').classList.toggle('active', mode === 'auto');
      document.getElementById('modeClipBtn').classList.toggle('active', mode === 'clipboard_only');
    }

    // 3. 文本输入与字数统计
    textInput.addEventListener('input', () => {
      charCount.textContent = `${textInput.value.length} 字符`;
    });

    function clearInput() {
      if (!textInput.value) return;
      textInput.value = '';
      charCount.textContent = '0 字符';
      if (document.activeElement !== textInput) {
        textInput.focus();
      }
    }

    // 4. Toast 轻量提示 (仅限异常或拦截)
    let toastTimer = null;
    function showToast(msg) {
      toastMsg.textContent = msg;
      toast.classList.add('show');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => {
        toast.classList.remove('show');
      }, 2000);
    }

    function resetSendBtn() {
      sendBtn.disabled = false;
      sendBtn.classList.remove('success');
      sendBtn.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
        </svg>
        <span>发送至电脑</span>
      `;
    }

    // 5. 真实数据发送
    async function handleSend() {
      const text = textInput.value.trim();
      if (!text) {
        showToast('请先长按输入法语音键说话');
        if (document.activeElement !== textInput) {
          textInput.focus();
        }
        return;
      }

      // Haptic 反馈
      if (navigator.vibrate) {
        navigator.vibrate(20);
      }

      sendBtn.disabled = true;
      sendBtn.innerHTML = '<span>正在发送...</span>';

      try {
        const resp = await fetch('/api/type', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            text: text,
            mode: currentMode,
            enter: autoEnterCheck.checked
          })
        });
        const data = await resp.json();

        if (data.ok) {
          // 静默添加历史与清空
          addHistory(text);
          if (autoClearCheck.checked) {
            textInput.value = '';
            charCount.textContent = '0 字符';
          }

          // 按钮原地轻量反馈：统一显示“已发送”
          sendBtn.classList.add('success');
          sendBtn.innerHTML = `
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="20 6 9 17 4 12"/>
            </svg>
            <span>已发送</span>
          `;
          setTimeout(() => {
            resetSendBtn();
          }, 750);
        } else {
          resetSendBtn();
          showToast('错误: ' + (data.error || '发送失败'));
        }
      } catch (err) {
        resetSendBtn();
        showToast('网络连接失败，请检查局域网');
        statusDot.classList.add('offline');
        statusText.textContent = '网络断开';
      }
    }

    // 6. 历史记录 (本地持久化 LocalStorage)
    function getSavedHistory() {
      try {
        return JSON.parse(localStorage.getItem('voice_history') || '[]');
      } catch {
        return [];
      }
    }

    function saveHistory(list) {
      try {
        localStorage.setItem('voice_history', JSON.stringify(list));
      } catch {}
    }

    let history = getSavedHistory();

    function addHistory(text) {
      // 去重置顶
      history = history.filter(item => item.text !== text);
      history.unshift({
        text: text,
        time: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
        count: text.length
      });
      if (history.length > 30) history.pop();
      saveHistory(history);
      renderHistory();
    }

    function renderHistory() {
      historyCount.textContent = `${history.length} 条`;
      if (history.length === 0) {
        historyList.innerHTML = '<div style="font-size:12px;color:var(--text-muted);text-align:center;padding:12px;">暂无历史记录</div>';
        return;
      }
      historyList.innerHTML = history.map((item, idx) => `
        <div class="history-item" onclick="reuseText(${idx})">
          <div class="history-item-body">${escapeHtml(item.text)}</div>
          <div class="history-item-footer">
            <span>${item.time || ''} · ${item.count || item.text.length}字</span>
            <div class="history-actions">
              <button class="history-action-btn" onclick="event.stopPropagation(); reSendText(${idx})">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 10 4 15 9 20"/><path d="M20 4v7a4 4 0 0 1-4 4H4"/></svg>
                重发
              </button>
            </div>
          </div>
        </div>
      `).join('');
    }

    function reuseText(idx) {
      if (!history[idx]) return;
      textInput.value = history[idx].text;
      charCount.textContent = `${textInput.value.length} 字符`;
      textInput.focus();
    }

    function reSendText(idx) {
      if (!history[idx]) return;
      textInput.value = history[idx].text;
      handleSend();
    }

    function clearAllHistory() {
      if (history.length === 0) return;
      history = [];
      saveHistory(history);
      renderHistory();
    }
    function escapeHtml(str) {
      return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    // 7. 心跳状态检查
    async function checkHealth() {
      try {
        const res = await fetch('/api/status', { cache: 'no-store' });
        if (res.ok) {
          statusDot.classList.remove('offline');
          statusText.textContent = '已连接';
        } else {
          statusDot.classList.add('offline');
          statusText.textContent = '服务异常';
        }
      } catch {
        statusDot.classList.add('offline');
        statusText.textContent = '无法连通';
      }
    }

    // 启动初始化
    initTheme();
    initPreferences();
    renderHistory();
    setInterval(checkHealth, 5000);

    // 8. 软键盘焦点保活拦截：点击按钮阻止失焦，防止软键盘闪退重启
    function preventFocusLoss(e) {
      e.preventDefault();
    }
    sendBtn.addEventListener('pointerdown', preventFocusLoss);
    sendBtn.addEventListener('mousedown', preventFocusLoss);
    document.querySelectorAll('.clear-btn, .segment-btn, .theme-toggle-btn, .hint-checkbox').forEach(el => {
      el.addEventListener('pointerdown', preventFocusLoss);
      el.addEventListener('mousedown', preventFocusLoss);
    });
  </script>
</body>
</html>
"""

# ---------------------------------------------------------
# 4. HTTP 请求处理派发
# ---------------------------------------------------------

class VoiceRequestHandler(BaseHTTPRequestHandler):
    def address_string(self):
        # 禁用反向 DNS 解析，直接返回 IP，避免 Windows 局域网下耗时阻塞
        return self.client_address[0]

    def log_message(self, format, *args):
        if sys.stdout is not None:
            try:
                sys.stdout.write(f"[{self.log_date_time_string()}] {self.address_string()} - {format % args}\n")
                sys.stdout.flush()
            except Exception:
                pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as e:
            if sys.stderr is not None:
                try:
                    sys.stderr.write(f"[HTTP Error] {e}\n")
                except Exception:
                    pass
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            content = MOBILE_HTML.encode("utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif parsed.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            payload = {
                "status": "ok",
                "platform": platform.system(),
                "wayland_display": APP_ENV.get("WAYLAND_DISPLAY"),
                "xdg_runtime_dir": APP_ENV.get("XDG_RUNTIME_DIR")
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/type":
            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len)
            try:
                data = json.loads(post_body.decode("utf-8"))
                text = data.get("text", "")
                mode = data.get("mode", "auto")
                auto_enter = bool(data.get("enter", False))

                if not text:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": False, "error": "文本不能为空"}).encode("utf-8"))
                    return

                # 执行注入
                inject_res = inject_text(text, mode)
                if inject_res["ok"]:
                    play_feedback_sound()
                    # 如果勾选了自动回车，间隔 60ms 敲下回车键
                    if auto_enter:
                        time.sleep(0.06)
                        press_key("Return")
                self.send_response(200 if inject_res["ok"] else 500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                resp_bytes = json.dumps(inject_res).encode("utf-8")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)
            except Exception as ex:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(ex)}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

# ---------------------------------------------------------
# 5. 启动入口与二维码展示
# ---------------------------------------------------------

def get_qr_ansi(text: str) -> str:
    """尝试获取终端字符二维码：优先 qrencode 命令，次选 python-qrcode 库"""
    try:
        proc = subprocess.run(
            ["qrencode", "-t", "ANSIUTF8", text],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout.strip("\n")
    except Exception:
        pass

    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        lines = []
        for r in range(0, len(matrix), 2):
            line = []
            for c in range(len(matrix[0])):
                top = matrix[r][c]
                bot = matrix[r+1][c] if r + 1 < len(matrix) else False
                if top and bot:
                    line.append(" ")
                elif top and not bot:
                    line.append("▄")
                elif not top and bot:
                    line.append("▀")
                else:
                    line.append("█")
            lines.append("".join(line))
        return "\n".join(lines)
    except Exception:
        pass

    return ""

def main():
    parser = argparse.ArgumentParser(description="Voice Input Bridge Server")
    parser.add_argument("--port", type=int, default=58002, help="监听端口 (默认 58002)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--fallback-port", type=int, default=53317, help="端口被占用时的自动回退端口")
    parser.add_argument("--display-ip", type=str, default=os.environ.get("VOICE_DISPLAY_IP"), help="优先展示的连接 IP (默认自动探测，支持环境变量 VOICE_DISPLAY_IP)")
    parser.add_argument("--sound", dest="sound", action="store_true", default=True, help="启用电脑端文字上屏提示音 (默认启用)")
    parser.add_argument("--no-sound", dest="sound", action="store_false", help="禁用电脑端文字上屏提示音")
    args = parser.parse_args()
    port = args.port
    server = None
    global ENABLE_SOUND
    ENABLE_SOUND = args.sound

    # 尝试绑定端口 (兼容 Linux errno 98 与 Windows WSAEADDRINUSE 10048)
    try:
        server = HTTPServer((args.host, port), VoiceRequestHandler)
    except OSError as e:
        if e.errno in (98, 10048):
            print(f"\n[⚠️ 端口提示] 端口 {port} 当前已被其他进程占用！")
            print(f"[🔄 自动回退] 尝试使用防火墙已放行的备用端口: {args.fallback_port} ...")
            try:
                port = args.fallback_port
                server = HTTPServer((args.host, port), VoiceRequestHandler)
            except OSError as e2:
                print(f"[❌ 错误] 备用端口 {port} 同样不可用: {e2}")
                sys.exit(1)
        else:
            print(f"[❌ 错误] 启动服务失败: {e}")
            sys.exit(1)

    lan_ips = get_lan_ips()
    if args.display_ip:
        primary_ip = args.display_ip
    else:
        primary_ip = lan_ips[0] if lan_ips else "127.0.0.1"
    access_url = f"http://{primary_ip}:{port}"

    sys_name = "Windows" if IS_WINDOWS else "Linux / Wayland"
    print("=" * 60)
    print(f" 🎙️  Voice Input Bridge ({sys_name} 已就绪)")
    print("=" * 60)
    print(f" • 监听地址: {args.host}:{port}")
    if IS_WINDOWS:
        print(f" • 操作系统: Windows ({platform.release()})")
    else:
        print(f" • 桌面会话: WAYLAND_DISPLAY={APP_ENV.get('WAYLAND_DISPLAY')}")
    print(f" • 手机直连: \033[1;36m{access_url}\033[0m")
    if len(lan_ips) > 1:
        print(f" • 备用地址: {', '.join([f'http://{ip}:{port}' for ip in lan_ips[1:]])}")

    # 打印二维码或直连说明
    qr_output = get_qr_ansi(access_url)
    if qr_output:
        print("\n 📱 手机扫码直达输入界面:")
        print(qr_output)
    else:
        print("\n" + "-" * 60)
        print(" 📱 手机扫码直达输入界面:")
        print(f"    👉 请在手机浏览器地址栏输入: \033[1;36m{access_url}\033[0m")
        if IS_WINDOWS:
            print("    💡 提示: 电脑终端运行 pip install qrcode 即可开启字符二维码扫码")
        else:
            print("    💡 提示: 安装 qrencode 即可开启字符二维码扫码")
        print("-" * 60)

    print("=" * 60)
    print(" 服务运行中 (按 Ctrl+C 停止)...")
    sys.stdout.flush()

    def handle_sigterm(signum, frame):
        print("\n[👋 收到终止信号，安全退出...]")
        server.server_close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
