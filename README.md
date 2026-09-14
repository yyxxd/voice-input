# Voice Input Bridge 🎙️

> **手机语音输入直达 Linux 电脑**：利用手机成熟高精度的输入法（豆包、微信输入法、讯飞、搜狗、Gboard 等），通过局域网/异地组网秒级同步至 Linux 当前输入框与剪贴板。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.8+](https://img.shields.io/badge/Python-3.8+-green.svg)]()
[![Platform: Linux Wayland](https://img.shields.io/badge/Platform-Linux%20Wayland-orange.svg)]()

---

## 💡 痛点与背景

1. **Linux 语音输入现状**：缺乏好用、开箱即用的系统级中文语音输入法；配置本地语音大模型（Whisper 等）既吃显卡内存又难以做到实时。
2. **电脑硬件限制**：台式机或特定工作站通常没有随身配备高质量外置麦克风。
3. **手机端体验领先**：手机上的现代 AI 语音输入法（如豆包、微信输入法等）识别率极高、抗噪强、口语自动润色、中英文混杂完美兼容。

**本项目通过极简架构打通两端**：手机只负责最擅长的“语音转文本”，电脑只负责“文本毫秒级上屏”，无需复杂的音频流转或笨重客户端。

---

## ✨ 核心特性

- **极简单文件设计**：仅单个 `voice_input.py` 文件，**零第三方包依赖**（纯 Python 3 标准库），开箱即用。
- **手机端零安装**：无需在手机上安装任何 App，局域网扫码或打开浏览器即可使用，兼容 iOS、Android 与各类平板。
- **双通道无损上屏策略**：
  - **第一通道**：无条件快速写入 Wayland 系统剪贴板（`wl-copy`），保证数据 100% 不丢失。
  - **第二通道**：瞬时触发虚拟按键粘贴（`wtype` 模拟 `Ctrl+V`），光标所在输入框自动上屏；若未选中任何输入框，文字静默留在剪贴板中供随时粘贴。
- **现代化触控界面**：深色自适应 UI，大号多行录入区，触控震动反馈，支持「直接上屏 / 仅存剪贴板」双模式与「历史记录」快速重发。
- **网络自适应与组网友好**：支持物理局域网 Wi-Fi 直连，完美兼容节点小宝、Tailscale 等虚拟组网异地直连。
- **终端二维码**：启动时自动在终端生成高对比度 UTF-8 字符二维码，手机摄像头扫码秒开。

---

## 🛠️ 系统要求

- **操作系统**：Linux（推荐 Arch Linux、Ubuntu 22.04+、Fedora 等使用 Wayland 的桌面，如 Hyprland、Sway、GNOME、KDE）
- **Python 版本**：Python 3.8+
- **系统依赖包**：
  - `wl-clipboard`（提供 `wl-copy` 与 `wl-paste`）
  - `wtype`（提供 Wayland 虚拟键盘按键模拟）
  - `qrencode`（可选，用于在终端打印二维码）

### 安装依赖命令

- **Arch Linux**：
  ```bash
  sudo pacman -S wl-clipboard wtype qrencode
  ```
- **Ubuntu / Debian**：
  ```bash
  sudo apt install wl-clipboard wtype qrencode
  ```
- **Fedora**：
  ```bash
  sudo dnf install wl-clipboard wtype qrencode
  ```

---

## 🚀 快速上手

### 1. 运行服务

```bash
# 进入项目目录
cd ~/Projects/voice-input

# 启动服务
python3 voice_input.py
```

### 2. 手机连接并输入

1. 确保手机和电脑连入同一 Wi-Fi，或开启了相同的虚拟组网（如节点小宝、Tailscale）。
2. 手机扫描终端中输出的**二维码**，或在手机浏览器中打开显示的 IP 地址（例如 `http://192.168.31.58:58002` 或节点小宝 `http://100.66.1.4:58002`）。
3. 电脑上将鼠标光标点进任意输入框（浏览器搜索框、聊天窗口、VS Code 或终端）。
4. 在手机页面里点击输入框，唤出手机键盘并**长按语音键说话**，说完点击**「发送至电脑」**即可实时上屏！

---

## ⚙️ 进阶启动参数

```bash
python3 voice_input.py [选项]
```

| 参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `--port` | `58002` | 服务监听端口（默认 58002，若被占用会自动平滑切至 `--fallback-port`） |
| `--fallback-port` | `53317` | 当首选端口被占用时的自动回退端口 |
| `--display-ip` | 自动探测 | 终端二维码与首选展示的 IP 地址（如节点小宝虚拟 IP：`100.66.1.4`） |
| `--host` | `0.0.0.0` | 监听的主机地址（`0.0.0.0` 表示监听所有网卡） |

**示例（指定节点小宝虚拟组网地址）：**
```bash
python3 voice_input.py --display-ip 100.66.1.4
```

---

## 🔄 配置开机自启动 (Systemd User Service)

测试满意后，可将其配置为当前用户的开机自启后台服务：

### 1. 复制服务配置文件
```bash
mkdir -p ~/.config/systemd/user/
cp ~/Projects/voice-input/voice-input.service ~/.config/systemd/user/
```

### 2. （可选）核对服务参数
查看或编辑 `~/.config/systemd/user/voice-input.service`，确认端口与 IP 参数符合个人网络环境：
```ini
[Unit]
Description=Voice Input Bridge for Linux (Wayland)
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/yy/Projects/voice-input/voice_input.py --display-ip 100.66.1.4
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

### 3. 启用并立即启动
```bash
# 重新加载 systemd 用户配置
systemctl --user daemon-reload

# 设置开机自启并立即运行
systemctl --user enable --now voice-input

# 查看运行状态
systemctl --user status voice-input
```

---

## 📄 开源许可证

本项目采用 [MIT License](LICENSE) 授权许可。
