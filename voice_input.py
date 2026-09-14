#!/usr/bin/env python3
"""
Voice Input Bridge for Linux (Wayland / Hyprland)
手机语音输入直达 Linux 电脑：双端互联，利用手机成熟语音输入法，同步至 Linux 焦点输入框与系统剪贴板。
"""

import os
import sys
import json
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
    """确保在 SSH 或无图形子 shell 下也能正确定位 Wayland 会话"""
    env = os.environ.copy()
    uid = os.getuid()

    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

    if "WAYLAND_DISPLAY" not in env:
        runtime_path = Path(env["XDG_RUNTIME_DIR"])
        if runtime_path.exists():
            sockets = [
                s.name for s in runtime_path.glob("wayland-*")
                if not s.name.endswith(".lock")
            ]
            if sockets:
                # 优先选择数字最小的可用 socket
                sockets.sort()
                env["WAYLAND_DISPLAY"] = sockets[0]

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
        if ip.startswith("100.66.") or ip == "100.66.1.4":
            sorted_ips.insert(0, ip)
        elif ip.startswith("192.168."):
            sorted_ips.append(ip)
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

def inject_text(text: str, mode: str = "auto"):
    """
    双通道无损注入逻辑：
    1. 无条件写入 Wayland 剪贴板 (wl-copy) 确保内容不丢失
    2. 如果 mode == 'auto'，模拟触发 Ctrl+V 粘贴按键 (wtype)
    """
    result = {
        "ok": False,
        "copied": False,
        "typed": False,
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
    # 模拟按键 Ctrl+V
    if mode == "auto":
        try:
            proc_type = subprocess.run(
                ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"],
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

# ---------------------------------------------------------
# 3. 手机端 Web 界面 HTML (纯单文件嵌入)
# ---------------------------------------------------------

MOBILE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <meta name="theme-color" content="#121316" />
  <title>语音直达电脑</title>
  <style>
    :root {
      --bg: #0f1012;
      --card-bg: #181a1f;
      --card-border: #262930;
      --text: #f0f2f5;
      --text-muted: #88909d;
      --primary: #3b82f6;
      --primary-hover: #2563eb;
      --primary-active: #1d4ed8;
      --success: #10b981;
      --danger: #ef4444;
      --radius: 14px;
    }
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
    }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      padding: 16px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 0 14px 0;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 600;
      font-size: 1.15rem;
      letter-spacing: -0.01em;
    }
    .status-badge {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.8rem;
      color: var(--text-muted);
      background: var(--card-bg);
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid var(--card-border);
    }
    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--success);
      box-shadow: 0 0 8px rgba(16, 185, 129, 0.6);
    }
    .status-dot.offline {
      background: var(--danger);
      box-shadow: 0 0 8px rgba(239, 68, 68, 0.6);
    }
    .main-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: var(--radius);
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.3);
    }
    .input-wrapper {
      position: relative;
    }
    textarea {
      width: 100%;
      height: 130px;
      background: #111215;
      color: var(--text);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 12px;
      font-size: 16px;
      line-height: 1.5;
      resize: none;
      outline: none;
      transition: border-color 0.2s, box-shadow 0.2s;
    }
    textarea:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
    }
    .char-count {
      position: absolute;
      right: 10px;
      bottom: 10px;
      font-size: 0.75rem;
      color: var(--text-muted);
      pointer-events: none;
    }
    .controls {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }
    .mode-toggle {
      display: flex;
      background: #111215;
      padding: 3px;
      border-radius: 8px;
      border: 1px solid var(--card-border);
      font-size: 0.85rem;
    }
    .mode-btn {
      padding: 6px 12px;
      border-radius: 6px;
      border: none;
      background: transparent;
      color: var(--text-muted);
      font-size: 0.82rem;
      cursor: pointer;
      transition: all 0.2s;
    }
    .mode-btn.active {
      background: var(--card-bg);
      color: var(--text);
      font-weight: 500;
      box-shadow: 0 1px 4px rgba(0,0,0,0.4);
    }
    .action-btn {
      width: 100%;
      padding: 14px;
      background: var(--primary);
      color: #fff;
      font-size: 1.05rem;
      font-weight: 600;
      border: none;
      border-radius: 12px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      transition: background-color 0.15s, transform 0.1s;
      box-shadow: 0 4px 14px rgba(59, 130, 246, 0.35);
    }
    .action-btn:active {
      background: var(--primary-active);
      transform: scale(0.98);
    }
    .action-btn:disabled {
      background: #2a2d34;
      color: #555c68;
      box-shadow: none;
      cursor: not-allowed;
    }
    .quick-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 0 4px;
    }
    .checkbox-label {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.85rem;
      color: var(--text-muted);
      user-select: none;
      cursor: pointer;
    }
    .clear-link {
      background: none;
      border: none;
      color: var(--text-muted);
      font-size: 0.85rem;
      cursor: pointer;
      padding: 4px;
    }
    .clear-link:active {
      color: var(--danger);
    }
    .history-card {
      margin-top: 16px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: var(--radius);
      padding: 14px;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .history-title {
      font-size: 0.85rem;
      color: var(--text-muted);
      font-weight: 500;
      display: flex;
      justify-content: space-between;
    }
    .history-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
      overflow-y: auto;
      max-height: 220px;
    }
    .history-item {
      background: #111215;
      padding: 8px 12px;
      border-radius: 8px;
      font-size: 0.88rem;
      line-height: 1.4;
      border-left: 3px solid var(--primary);
      cursor: pointer;
      word-break: break-all;
    }
    .history-item:active {
      background: #1e2127;
    }
    .toast {
      position: fixed;
      bottom: 24px;
      left: 50%;
      transform: translateX(-50%) translateY(100px);
      background: #1e2025;
      border: 1px solid var(--card-border);
      color: #fff;
      padding: 10px 18px;
      border-radius: 999px;
      font-size: 0.9rem;
      box-shadow: 0 6px 24px rgba(0,0,0,0.5);
      pointer-events: none;
      opacity: 0;
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      z-index: 100;
    }
    .toast.show {
      transform: translateX(-50%) translateY(0);
      opacity: 1;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span>🎙️ 语音直达电脑</span>
    </div>
    <div class="status-badge">
      <span class="status-dot" id="statusDot"></span>
      <span id="statusText">已连接</span>
    </div>
  </header>

  <main class="main-card">
    <div class="input-wrapper">
      <textarea
        id="textInput"
        placeholder="点击调出手机输入法，按住语音键说话..."
        autofocus
      ></textarea>
      <div class="char-count" id="charCount">0 字</div>
    </div>

    <div class="controls">
      <div class="mode-toggle">
        <button class="mode-btn active" id="btnModeAuto" onclick="setMode('auto')">直接上屏</button>
        <button class="mode-btn" id="btnModeClip" onclick="setMode('clipboard_only')">仅剪贴板</button>
      </div>
      <div class="quick-bar">
        <button class="clear-link" onclick="clearInput()">清空</button>
      </div>
    </div>

    <button class="action-btn" id="sendBtn" onclick="handleSend()">
      <span>发送至电脑</span>
    </button>

    <div class="quick-bar">
      <label class="checkbox-label">
        <input type="checkbox" id="autoClearCheck" checked />
        <span>发送后自动清空输入框</span>
      </label>
    </div>
  </main>

  <section class="history-card">
    <div class="history-title">
      <span>最近发送历史 (点击可再次填入)</span>
      <span id="historyCount">0 条</span>
    </div>
    <div class="history-list" id="historyList"></div>
  </section>

  <div class="toast" id="toast"></div>

  <script>
    let currentMode = 'auto';
    const textInput = document.getElementById('textInput');
    const charCount = document.getElementById('charCount');
    const sendBtn = document.getElementById('sendBtn');
    const toast = document.getElementById('toast');
    const historyList = document.getElementById('historyList');
    const historyCount = document.getElementById('historyCount');
    const autoClearCheck = document.getElementById('autoClearCheck');
    const statusDot = document.getElementById('statusDot');
    const statusText = document.getElementById('statusText');

    let history = [];

    // 模式切换
    function setMode(mode) {
      currentMode = mode;
      document.getElementById('btnModeAuto').classList.toggle('active', mode === 'auto');
      document.getElementById('btnModeClip').classList.toggle('active', mode === 'clipboard_only');
      showToast(mode === 'auto' ? '模式：自动触发上屏' : '模式：仅同步剪贴板');
    }

    // 监听输入
    textInput.addEventListener('input', () => {
      charCount.textContent = `${textInput.value.length} 字`;
    });

    // 清空输入
    function clearInput() {
      textInput.value = '';
      charCount.textContent = '0 字';
      textInput.focus();
    }

    // 提示通知
    let toastTimer = null;
    function showToast(msg) {
      toast.textContent = msg;
      toast.classList.add('show');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => {
        toast.classList.remove('show');
      }, 1800);
    }

    // 发送逻辑
    async function handleSend() {
      const text = textInput.value.trim();
      if (!text) {
        showToast('请先说话或输入文字');
        textInput.focus();
        return;
      }

      // 轻微震动反馈
      if (navigator.vibrate) {
        navigator.vibrate(30);
      }

      sendBtn.disabled = true;
      sendBtn.innerHTML = '<span>发送中...</span>';

      try {
        const resp = await fetch('/api/type', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: text, mode: currentMode })
        });
        const data = await resp.json();

        if (data.ok) {
          showToast(currentMode === 'auto' ? '已上屏并存入剪贴板 ✨' : '已存入电脑剪贴板 📋');
          addHistory(text);
          if (autoClearCheck.checked) {
            clearInput();
          }
        } else {
          showToast('错误: ' + (data.error || '发送失败'));
        }
      } catch (err) {
        showToast('网络连接失败，请检查局域网');
        statusDot.classList.add('offline');
        statusText.textContent = '网络断开';
      } finally {
        sendBtn.disabled = false;
        sendBtn.innerHTML = '<span>发送至电脑</span>';
      }
    }

    // 历史记录
    function addHistory(text) {
      history.unshift(text);
      if (history.length > 20) history.pop();
      renderHistory();
    }

    function renderHistory() {
      historyCount.textContent = `${history.length} 条`;
      historyList.innerHTML = history.map((item, idx) => `
        <div class="history-item" onclick="reuseText(${idx})">${escapeHtml(item)}</div>
      `).join('');
    }

    function reuseText(idx) {
      textInput.value = history[idx];
      charCount.textContent = `${textInput.value.length} 字`;
      textInput.focus();
      showToast('已重载历史文本');
    }

    function escapeHtml(str) {
      return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    // 心跳状态检查
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
    setInterval(checkHealth, 5000);
  </script>
</body>
</html>
"""

# ---------------------------------------------------------
# 4. HTTP 请求处理派发
# ---------------------------------------------------------

class VoiceRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 简化日志输出
        sys.stdout.write(f"[{self.log_date_time_string()}] {self.address_string()} - {format % args}\n")
        sys.stdout.flush()

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

                if not text:
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": False, "error": "文本不能为空"}).encode("utf-8"))
                    return

                # 执行注入
                inject_res = inject_text(text, mode)
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
    parser.add_argument("--display-ip", type=str, default="100.66.1.4", help="优先展示的连接 IP (如节点小宝地址)")
    args = parser.parse_args()
    port = args.port
    server = None

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

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[👋 服务已停止]")
        server.server_close()

if __name__ == "__main__":
    main()
