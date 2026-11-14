# 开发日志

## 2026-04-17
- 扩展 `ncm.py`：新增用户歌单分页拉取（`user_playlists`）、歌单详情及超长歌单自动补齐（`playlist_tracks` / `song_details_batch`）、歌词解析与双语翻译合并（`song_lyric`）、CDN 可播直链探测（`probe_playable_url`）、`chafa` 真彩封面渲染（`fetch_cover_ansi`），并新增 `tui` 子命令。
- 新增 `player.py`：封装 `MpvPlayer`，通过 `mpv --input-ipc-server` 实现无损音频流式播放、暂停/继续、绝对/相对跳转进度、音量调节以及自然播完（`eof`）事件回调。
- 新增 `tui.py`：在 `.venv` 安装 `textual`，实现三栏 TUI 播放器（歌单栏、带搜索过滤的歌曲列表、封面+同步滚动双语歌词栏+可点击进度条/底部控制栏），默认随机洗牌播放（带历史回退栈），无版权歌曲自动跳过。
- 验证状态：已完成 Textual 无头集成测试（加载 20 个歌单 / 394 首歌、随机播放 LOSSLESS FLAC、封面渲染、歌词同步、进度 seek、暂停、上一首/下一首及退出清理均正常）。
- 初始化 git 仓库并配置 `.gitignore`（排除 `cookie.txt`、`output/`、`.venv/`、`__pycache__/`、`*.pyc`、`.dsh/`），完成初次提交。
- 修复 `tui.py` 音量加快捷键绑定：Textual 中 `=` 键名为 `equals_sign`（原误写为 `equal`），同时绑定 `plus`（`+`）并显示 `key_display="="`；将 `player.py` 音量上限统一为 `100.0` 与 mpv 默认一致。已通过无头按键测试（`100 -> 90 -> 95 -> 100`）。
- 全面美化 `tui.py` 前端显示效果：引入 Sakura Twilight Pastel 柔和粉彩渐变调色板（`#131521` 深暮底色 + 薄荷/薰衣草/樱粉/杏桃柔光过渡）、顶部极光品牌条 `AmbientBrand`、待机/无封面时的 ASCII 黑胶唱片机慢动画 `VinylCoverStage` 与流光指示带、3 行高细腻度 Braille 慢速谐波声浪 `AmbientWaveVisualizer`、粉彩渐变可点击进度条 `SeekBar`、圆角聚焦边框、双色调歌单/曲目排版，以及 `out_cubic` 丝滑歌词滚动与景深层次渐隐。已通过 Textual 无头集成测试（含播放、封面渲染、歌词同步、快进、暂停与退出）。
- 优化 `tui.py` 与 `player.py`：精简各栏 padding 与高度（`#cover-box` 压矮、ASCII 唱片机缩至 6 行、自适应封面缩至 6–10 行、单行无边框搜索栏）；新增面板开关快捷键（`1` 歌单栏、`2` 封面区、`3` 声波、`0` 一键还原）；新增 `PeakPrecomputer`/`PeakTimeline`（后台 `ffmpeg` 流式 4kHz 抽样，25Hz 提取 `peak/rms/bass/mid/high` 并缓存）与 `player.py` 单调时间戳 `time_pos_at` 平滑内插，驱动 `AmbientWaveVisualizer` 20 FPS 贴合节拍的高实时性声波可视化。已通过无头集成测试验证。
- 升级 `tui.py` 预计算可视化引擎：改用 `12kHz` 单声道流式采样 + 256 点基-2 FFT 映射至 24 个对数感知频段（25Hz 帧率）并提取真实峰谷包络；将 `AmbientWaveVisualizer` 扩至 5 行高度，默认展示 **真 FFT 频谱柱状图 + 独立悬停缓落 Peak 峰值帽**，支持按 `v` 键切换为 **实时对称卷轴波形图**（左侧已播高亮、中央游标、右侧预计算波形预览）。已通过无头集成测试验证。

## 2026-11-14
- 重写 `README.md`：移除本机路径与本机预置环境假设，补充面向新用户的仓库克隆（`https://github.com/Eganchiyu/ncm-tui-lite`）、虚拟环境创建、Python/系统依赖安装、启动命令、最新 TUI 快捷键（含 `v` 切换 FFT 频谱/波形与 `1/2/3/0` 面板开关）及各平台兼容性说明。
- 新增 `LICENSE`（MIT License，版权署名 `Eganchiyu`），并在 `README.md` 末尾补充开源协议与版权/使用范围免责声明。

