#!/usr/bin/env python3
"""
Voice Input Bridge for Linux (Wayland / Hyprland)
手机语音输入直达 Linux 电脑：双端互联，利用手机成熟语音输入法，同步至 Linux 焦点输入框与系统剪贴板。
"""
import os
import time
import sys
import json
import signal
import socket
import argparse
import subprocess
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ---------------------------------------------------------
# 1. Wayland 与系统环境智能探测
# ---------------------------------------------------------

def setup_wayland_env():
    """确保在 SSH、systemd 用户服务或无图形子 shell 下也能正确定位 Wayland 与 D-Bus 会话"""
    env = os.environ.copy()
    uid = os.getuid()

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
                # 优先选择数字最小的可用 socket
                sockets.sort()
                env["WAYLAND_DISPLAY"] = sockets[0]

        # 补充 Hyprland 实例环境变量，以便 hyprctl 在后台服务中正常调用
        if "HYPRLAND_INSTANCE_SIGNATURE" not in env:
            hypr_dir = runtime_path / "hypr"
            if hypr_dir.exists():
                hypr_instances = [d for d in hypr_dir.glob("*") if d.is_dir()]
                if hypr_instances:
                    hypr_instances.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    env["HYPRLAND_INSTANCE_SIGNATURE"] = hypr_instances[0].name

        # 补充 D-Bus 会话总线地址，以便 notify-send 正常投递桌面通知
        if "DBUS_SESSION_BUS_ADDRESS" not in env:
            bus_sock = runtime_path / "bus"
            if bus_sock.exists():
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_sock}"

    return env

APP_ENV = setup_wayland_env()

def get_lan_ips():
    """获取本机有效局域网 IP（优先排除 docker/tun 等虚拟网卡）"""
    ips = []
    try:
        output = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show"], text=True
        )
        for line in output.strip().splitlines():
            parts = line.split()
            if len(parts) >= 4:
                dev = parts[1]
                ip_cidr = parts[3]
                ip = ip_cidr.split("/")[0]
                if ip.startswith("127."):
                    continue
                # 记录网卡和 IP
                ips.append((dev, ip))
    except Exception:
        # 回退通用探测方式
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("223.5.5.5", 80))
            fallback_ip = s.getsockname()[0]
            s.close()
            return [fallback_ip]
        except Exception:
            return ["127.0.0.1"]

    # 排序：优先选择常见的物理内网段 (192.168.x, 10.x, 172.16-31.x)
    sorted_ips = []
    for dev, ip in ips:
        if any(dev.startswith(prefix) for prefix in ["docker", "veth", "br-", "virbr", "tun", "tap", "meta"]):
            continue
        if ip.startswith("192.168."):
            sorted_ips.insert(0, ip)
        elif ip.startswith("10.") and not dev.startswith("tun"):
            sorted_ips.append(ip)
        else:
            sorted_ips.append(ip)
    if not sorted_ips:
        sorted_ips = [ip for _, ip in ips] or ["127.0.0.1"]

    # 去重
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

# 常见主流 Linux 终端模拟器的 class / app_id
KNOWN_TERMINAL_CLASSES = {
    "alacritty", "kitty", "foot", "footclient", "ghostty", "wezterm", "wezterm-gui",
    "gnome-terminal", "gnome-terminal-server", "org.gnome.terminal", "konsole",
    "xterm", "uxterm", "rxvt", "urxvt", "terminator", "tilix", "xfce4-terminal",
    "lxterminal", "mate-terminal", "sakura", "termite", "rio", "contour",
    "hyper", "tabby", "warp", "blackbox", "ptyxis", "deepin-terminal"
}

def get_active_window_class() -> str:
    """探测当前处于焦点的活动窗口 class / app_id"""
    # 1. 优先尝试 Hyprland (hyprctl)
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

    # 2. 尝试 Sway (swaymsg)
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

def is_terminal_window(window_class: str) -> bool:
    """判定指定窗口是否为终端模拟器"""
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
    1. 无条件写入 Wayland 剪贴板 (wl-copy) 确保内容不丢失
    2. 如果 mode == 'auto'：
       - 智能检测活动窗口是否为终端
       - 终端触发 Ctrl+Shift+V
       - 普通应用触发 Ctrl+V
    """
    result = {
        "ok": False,
        "copied": False,
        "typed": False,
        "target_is_terminal": False,
        "error": None
    }

    # 检查 wl-copy
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
        # wl-copy 正常 fork 至后台提供剪贴板服务
        result["copied"] = True
    except subprocess.CalledProcessError:
        result["error"] = "wl-copy 写入剪贴板失败"
        return result
    except FileNotFoundError:
        result["error"] = "系统中未安装 wl-copy，请确认已安装 wl-clipboard"
        return result

    # 模拟按键上屏
    if mode == "auto":
        active_cls = get_active_window_class()
        is_term = is_terminal_window(active_cls)
        result["target_is_terminal"] = is_term

        # 快捷键自适应：终端默认使用 Ctrl+Shift+V，普通软件使用 Ctrl+V
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
    """模拟单次物理按键敲击 (如 Return / BackSpace 等)"""
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
# 2.1 电脑端即时音频反馈
# ---------------------------------------------------------

ENABLE_SOUND = True
SOUND_FILE = "/usr/share/sounds/freedesktop/stereo/audio-volume-change.oga"

def play_feedback_sound():
    """异步非阻塞播放轻微清脆的键盘上屏音效"""
    global ENABLE_SOUND
    if not ENABLE_SOUND or not os.path.exists(SOUND_FILE):
        return
    # 优先使用 pw-play (PipeWire)，回退使用 paplay
    player = "pw-play" if os.path.exists("/usr/bin/pw-play") else ("paplay" if os.path.exists("/usr/bin/paplay") else None)
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
    """异步非阻塞发送系统桌面通知 (notify-send)"""
    # 截断过长内容，保持通知卡片紧凑优雅
    display_body = body if len(body) <= 120 else f"{body[:117]}..."
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
# ---------------------------------------------------------
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

          // 按钮原地轻量反馈 (不弹视线遮挡的 Toast)
          sendBtn.classList.add('success');
          const successLabel = currentMode === 'auto' ? '已上屏' : '已存剪贴板';
          sendBtn.innerHTML = `
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="20 6 9 17 4 12"/>
            </svg>
            <span>${successLabel}</span>
          `;

          setTimeout(() => {
            resetSendBtn();
          }, 650);
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
    def log_message(self, format, *args):
        sys.stdout.write(f"[{self.log_date_time_string()}] {self.address_string()} - {format % args}\n")
        sys.stdout.flush()

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as e:
            sys.stderr.write(f"[HTTP Error] {e}\n")

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
                    # 模式为仅剪贴板时，电脑屏幕弹出通知提醒
                    if mode == "clipboard_only":
                        send_desktop_notification("已复制到剪贴板", text)
                    elif mode == "auto" and auto_enter:
                        time.sleep(0.06)  # 间隔 60ms 确保模拟粘贴按键完全释放与文本完成上屏
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

    # 尝试绑定端口
    try:
        server = HTTPServer((args.host, port), VoiceRequestHandler)
    except OSError as e:
        if e.errno == 98: # Address already in use
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

    print("=" * 60)
    print(" 🎙️  Voice Input Bridge (Linux / Wayland 已就绪)")
    print("=" * 60)
    print(f" • 监听地址: {args.host}:{port}")
    print(f" • 桌面会话: WAYLAND_DISPLAY={APP_ENV.get('WAYLAND_DISPLAY')}")
    print(f" • 手机直连: \033[1;36m{access_url}\033[0m")
    if len(lan_ips) > 1:
        print(f" • 备用地址: {', '.join([f'http://{ip}:{port}' for ip in lan_ips[1:]])}")

    # 打印二维码
    try:
        qr_output = subprocess.check_output(
            ["qrencode", "-t", "ANSIUTF8", access_url],
            stderr=subprocess.DEVNULL,
            text=True
        )
        print("\n 📱 手机扫码直达输入界面:")
        print(qr_output)
    except Exception:
        pass

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
