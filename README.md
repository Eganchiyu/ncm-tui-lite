# ncm-lite

用**本人账号 cookie** 直接调网易云 web / eapi 接口，按歌曲 id 下载**无损（lossless / SQ, FLAC）**到 `output/`。
不碰 hires、jymaster、沉浸环绕等更高档位。

只用标准库（AES 走系统 `openssl`）；只有 `login` 需要 venv 里的 `qrcode` + `pillow` 来画二维码。

## 用法

```bash
cd ~/ncm-lite

# 1) 登录（扫码，cookie 存到 cookie.txt，权限 600）
./.venv/bin/python ncm.py login

# 2) 校验登录态
python3 ncm.py whoami

# 3) 启动 TUI 无损流媒体播放器（默认 lossless）
./.venv/bin/python ncm.py tui

# 4) 按歌曲 id 下载无损到 output/
python3 ncm.py dl 1385116338

# 只取直链看信息，不下载
python3 ncm.py url 1385116338

# 指定档位（默认 lossless；可选 standard/higher/exhigh）
python3 ncm.py dl 1385116338 --level exhigh
```

歌曲 id 就是网页版 `music.163.com/#/song?id=xxxxx` 里的数字。

## 输出

- 文件：`output/<歌名> - <歌手>.flac`，文件名里的 `/ : * ? " < > |` 会被替换成 `_`。
- `dl` 打印 JSON：`actual_level` / `type` / `br` / `size_mb`，可确认真的是 lossless。
- 若请求档位拿不到，会自动逐级降档（lossless → exhigh → higher → standard），`actual_level` 会显示实际拿到的档位。

## 已知边界

- 无版权 / 版权受限的歌返回 `code: 404`，脚本报「拿不到直链」并打印 plLevel/dlLevel 权限信息。
- 直链带 `authSecret`，个别 CDN 节点见到它会 403，脚本会自动降级成去 authSecret / 去查询串重试（`cdn_fallback: true` 表示走了降级）。
- cookie 会过期（MUSIC_U 有效期数月），失效后重跑 `login`。
- 走的是非公开接口，账号风险自担；本目录内容属于个人备份用途。

## 文件

| 文件 | 说明 |
|---|---|
| `ncm.py` | 网易云登录、歌单/详情/歌词/直链接口及 CLI 入口 |
| `player.py` | 基于 `mpv --input-ipc-server` 的流媒体音频控制器 |
| `tui.py` | Textual TUI 播放器（歌单浏览、随机播放、chafa 封面、滚动歌词、进度与音量控制） |
| `cookie.txt` | 登录 cookie（不提交、不外传） |
| `output/` | 下载的 flac 与登录二维码临时图片 |
| `.venv/` | 含 `qrcode` / `pillow` / `textual` |

## TUI 快捷键

- `Enter` / 鼠标点击歌单或歌曲：切换歌单 / 播放指定曲目
- `x`：从当前歌单直接随机开播
- `Space` / `p`：播放 / 暂停
- `n` / `b`：下一首 / 上一首（保留历史栈）
- `←` / `→`（或 `,` / `.`）：后退 / 快进 5s（15s）
- `s`：切换随机播放 / 列表顺序循环
- `/` / `Esc`：搜索过滤歌单歌曲 / 清除搜索
- `-` / `=`：音量 -5% / +5%
- 点击进度条或任意歌词行：直接跳转到对应位置
- `r` / `q`：刷新歌单 / 退出
