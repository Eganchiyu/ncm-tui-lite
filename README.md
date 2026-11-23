# ncm-tui-lite

基于本人账号 Cookie 直连网易云音乐 Web / EAPI 接口的轻量级 CLI 下载器与 TUI 无损流媒体播放器（最高支持 **lossless / SQ, FLAC**，不涉及 Hi-Res / 沉浸环绕等更高档位）。这是因为本作者是无损派，主要用监听耳机来听歌，不是很喜欢经过AI调音的音质选项，于是锁定在无损位置。如果需要更高的音质选项，可以通过修改代码来轻松修改。

仓库地址：<https://github.com/Eganchiyu/ncm-tui-lite>

## 功能特性

- **扫码登录**：通过终端生成的二维码图片或登录链接使用网易云 App 扫码登录，Cookie 本地保存在 `cookie.txt`（权限 `600`）。
- **无损直链与下载**：优先获取 `lossless`（FLAC），不可用时自动逐级降档（`lossless → exhigh → higher → standard`），并自动处理部分 CDN 节点的 `authSecret` 403 降级。
- **TUI 流媒体播放器**：基于 Textual + `mpv` IPC 的三栏终端播放器：
  - 歌单浏览、关键词即时过滤、随机洗牌（带历史回退栈）与顺序循环；
  - `chafa` 真彩终端专辑封面渲染（缺失时自动回退为内置 ASCII 黑胶动画）；
  - 双语滚动歌词（支持鼠标点击歌词行或进度条跳转）；
  - 基于 `ffmpeg` 流式预计算的 24 频段 FFT 频谱柱状图（含峰值帽）与实时对称波形图；
  - **常驻顶层桌面歌词窗口**（`512×64` 胶囊浮窗，GTK3 + Cairo VSync 高刷、单调锁相 PLL 毫秒同步、支持 YRC 逐字卡拉OK扫光与鼠标点击穿透）。

---

## 环境依赖

### 1. Python 环境
- **Python 3.9+**（推荐 3.10 及以上）
- Python 包：
  - `textual`（TUI 播放器必需，会一并安装 `rich` 等组件）
  - `qrcode`、`pillow`（仅 `login` 生成二维码 PNG 图片需要；未安装时会自动降级为仅输出登录 URL）

### 2. 系统外部工具

| 工具 | 作用 | 必需程度 |
|---|---|---|
| `openssl` | `login` 扫码登录时的 EAPI AES-128-ECB 加密 | `login` 必需（已有 `cookie.txt` 时可不装） |
| `mpv` | TUI 播放器音频流解码与播放控制 | TUI 必需（仅用 `dl`/`url` 可不装） |
| `ffmpeg` | TUI 后台预计算音频 FFT 频谱与波形 | TUI 可选（缺失时回退为待机呼吸动画，不影响播歌） |
| `chafa` | 在终端渲染真彩专辑封面 | TUI 可选（缺失时回退为 ASCII 黑胶唱片动画） |

---

## 安装与启动

### 1. 克隆仓库并创建虚拟环境

```bash
git clone https://github.com/Eganchiyu/ncm-tui-lite.git
cd ncm-tui-lite

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install textual qrcode pillow
```

> 若仅使用命令行下载（`dl` / `url` / `whoami`），甚至无需创建虚拟环境，直接用系统 `python3` 即可（`login` 若不需要生成二维码图片也可只靠终端链接登录）。

### 2. 安装系统依赖

- **Debian / Ubuntu**：
  ```bash
  sudo apt update
  sudo apt install -y openssl mpv ffmpeg chafa
  ```
- **Arch Linux**：
  ```bash
  sudo pacman -S --needed openssl mpv ffmpeg chafa
  ```
- **Fedora**：
  ```bash
  sudo dnf install -y openssl mpv ffmpeg chafa
  ```
- **macOS (Homebrew)**：
  ```bash
  brew install mpv ffmpeg chafa openssl
  ```

### 3. 登录与启动

在项目根目录下（确保已激活虚拟环境，或使用 `.venv/bin/python`）：

```bash
# 1) 扫码登录（生成 output/ncm_qr_login.png 并打印登录链接，Cookie 存至 cookie.txt）
python ncm.py login

# 2) 校验登录态与账号 VIP 信息
python ncm.py whoami

# 3) 启动 TUI 流媒体播放器（默认 lossless）
python ncm.py tui
# 如需指定最高音质档位（lossless / exhigh / higher / standard）
python ncm.py tui --level exhigh

# 3b) 单独启动常驻顶层桌面歌词窗口（也可在 TUI 内按 l 一键开关）
python ncm.py lyrics
python ncm.py lyrics --width 512 --height 64 --offset-ms 0

# 4) 按歌曲 ID 下载单曲到 output/（ID 即网页版 song?id=xxxxx 中的数字）
python ncm.py dl 1385116338
python ncm.py dl 1385116338 --level exhigh

# 5) 仅查询播放直链与音质信息，不下载
python ncm.py url 1385116338
```

---

## TUI 快捷键与操作

- `Enter` / 鼠标点击歌单或歌曲：切换歌单 / 播放指定曲目
- `x`：从当前歌单直接随机开播
- `Space` / `p`：播放 / 暂停
- `n` / `b`：下一首 / 上一首（保留历史回退栈）
- `←` / `→`：后退 / 快进 5 秒（`,` / `.` 后退 / 快进 15 秒）
- `s`：切换随机播放 / 列表顺序循环
- `v`：切换可视化模式（24 频段 FFT 频谱 ↔ 实时对称波形）
- `1` / `2` / `3`：分别显示/隐藏 歌单栏 / 专辑封面 / 音频可视化
- `0`：还原全部面板显示
- `l` / `Shift+L`：开启或显示/隐藏 512×64 桌面歌词浮窗 / 切换鼠标穿透锁定
- `/` / `Esc`：聚焦搜索过滤当前歌单 / 清除搜索条件
- `-` / `=`（或 `+`）：音量 -5% / +5%
- 鼠标点击进度条或任意歌词行：直接跳转到对应时间点
- `r` / `q`：刷新歌单 / 退出

---

## 平台兼容性说明

| 平台 | CLI（`login`/`dl`/`url`） | TUI 播放器（`tui`） | 说明与注意事项 |
|---|---|---|---|
| **Linux** | 完全支持 | 完全支持 | 主力运行环境。音频依赖 ALSA / PulseAudio / PipeWire；SSH 远程运行时音频在远端宿主机播放。 |
| **macOS** | 完全支持 | 支持 | 建议配合 **iTerm2 / Kitty / WezTerm / Ghostty / Alacritty** 等支持 24-bit TrueColor 的终端使用；系统自带 `Terminal.app` 仅支持 256 色，粉彩渐变会有色带失真。 |
| **Windows（原生）** | 部分支持 | **暂不支持** | `player.py` 使用 Unix Domain Socket（`AF_UNIX`）连接 `mpv --input-ipc-server`，而 Windows 版 `mpv` 的 IPC 走命名管道（`\\.\pipe\...`），无法互通，直接运行 `tui` 会超时报错。CLI 的 `login` 需自行将 `openssl.exe` 加入 `PATH`。 |
| **Windows（WSL2）** | 完全支持 | 支持 | 在 WSL2（如 Ubuntu）中按 Linux 方式安装依赖即可完整使用；Windows 11 自带 WSLg，可将 PulseAudio 音频直接输出到宿主机。 |
| **Termux (Android)** | 支持 | 基本支持 | 可通过 `pkg` 安装 `python`、`openssl`、`mpv`、`ffmpeg`、`chafa`；手机竖屏较窄时建议按 `1` 隐藏歌单栏或横屏使用。 |

### 终端显示建议
- TUI 大量使用了 24-bit TrueColor（`#rrggbb`）与 Unicode 字符（方块字符 `▁▂▃▄▅▆▇█`、圆角边框与符号），请使用支持 **TrueColor** 与完整 **UTF-8** 渲染的现代终端。

---

## 输出与已知边界

- 下载文件名格式：`output/<歌名> - <歌手>.flac`（或 `.mp3`），文件名中的 `\ / : * ? " < > |` 等非法字符会被替换为 `_`。
- 无版权或版权受限歌曲接口返回 `code: 404` / 空直链：CLI 会打印 `plLevel` / `dlLevel` 权限信息，TUI 会自动跳过。
- Cookie 有效期通常为数月，失效后重新运行 `python ncm.py login` 即可续期。
- 本工具直接调用非公开 Web / EAPI 接口，仅供个人学习与本地备份用途，账号风险自担。

## 仓库文件说明

| 路径 | 说明 |
|---|---|
| `ncm.py` | 网易云登录、歌单/详情/歌词/直链接口与 CLI 入口 |
| `player.py` | 基于 `mpv --input-ipc-server` 的流媒体音频控制器 |
| `tui.py` | Textual TUI 播放器（封面、FFT 频谱/波形、滚动双语歌词与控制栏） |
| `lyric_ipc.py` | 播放器与桌面歌词之间的低延迟 Unix Socket 双向同步模块 |
| `desktop_lyrics.py` | 常驻顶层桌面歌词浮窗（GTK3 + Cairo VSync 高刷、PLL 同步与逐字扫光） |
| `cookie.txt` | 运行 `login` 后在本地生成（已在 `.gitignore` 中排除，切勿提交或外传） |
| `output/` | 运行后在本地生成，存放下载音频与登录二维码临时图片（已在 `.gitignore` 中排除） |
| `.venv/` | 建议创建的 Python 虚拟环境目录（已在 `.gitignore` 中排除） |

## 开源协议与免责声明

- 本项目源代码采用 [MIT License](LICENSE) 开源。
- **仅供学习参考**：本项目仅用于终端 UI、音频流处理及网络协议相关的技术学习与个人本地播放用途。
- **禁止违法与侵权用途**：严禁将本项目用于任何违反当地法律法规、侵犯他人合法权益（包括但不限于绕过付费限制、批量爬取、商业分发或传播受版权保护的音频内容）的行为。
- **版权归属**：本项目不提供、不托管也不分发任何音频资源；所有音频流及元数据版权均归原权利人及对应音乐平台所有。
- **风险自担**：本项目调用非公开 Web / EAPI 接口，请仅用于本人已合法获得授权的账号登录；因不当或违法使用本项目所引发的一切账号风险、纠纷或法律责任均由使用者自行承担，与作者无关。

