# 开发日志

## 2026-04-17
- 扩展 `ncm.py`：新增用户歌单分页拉取（`user_playlists`）、歌单详情及超长歌单自动补齐（`playlist_tracks` / `song_details_batch`）、歌词解析与双语翻译合并（`song_lyric`）、CDN 可播直链探测（`probe_playable_url`）、`chafa` 真彩封面渲染（`fetch_cover_ansi`），并新增 `tui` 子命令。
- 新增 `player.py`：封装 `MpvPlayer`，通过 `mpv --input-ipc-server` 实现无损音频流式播放、暂停/继续、绝对/相对跳转进度、音量调节以及自然播完（`eof`）事件回调。
- 新增 `tui.py`：在 `.venv` 安装 `textual`，实现三栏 TUI 播放器（歌单栏、带搜索过滤的歌曲列表、封面+同步滚动双语歌词栏+可点击进度条/底部控制栏），默认随机洗牌播放（带历史回退栈），无版权歌曲自动跳过。
- 验证状态：已完成 Textual 无头集成测试（加载 20 个歌单 / 394 首歌、随机播放 LOSSLESS FLAC、封面渲染、歌词同步、进度 seek、暂停、上一首/下一首及退出清理均正常）。
- 初始化 git 仓库并配置 `.gitignore`（排除 `cookie.txt`、`output/`、`.venv/`、`__pycache__/`、`*.pyc`、`.dsh/`），完成初次提交。
- 修复 `tui.py` 音量加快捷键绑定：Textual 中 `=` 键名为 `equals_sign`（原误写为 `equal`），同时绑定 `plus`（`+`）并显示 `key_display="="`；将 `player.py` 音量上限统一为 `100.0` 与 mpv 默认一致。已通过无头按键测试（`100 -> 90 -> 95 -> 100`）。
