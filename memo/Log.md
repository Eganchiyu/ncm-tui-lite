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
- 强化 `README.md` 免责声明条款，明确“仅供学习参考”“严禁用于违法违规及侵权行为”与“风险自担”声明。

## 2026-11-18
- 新增常驻顶层桌面歌词浮窗系统：
  - 扩展 `ncm.py`：新增 `parse_yrc`、`song_lyric_rich`（优先通过 `eapi/song/lyric/v1` 拉取逐字 YRC 时间轴并兼容回退标准 LRC 与双语译文对齐），新增 `lyrics` CLI 子命令。
  - 扩展 `player.py`：观测 `speed` 与 `seek` 事件、维护状态跳变序号 `seq`，新增 `on_state_change` 实时回调钩子，在收到 mpv `time-pos` 事件（~50ms 周期）瞬间即时推送状态快照。
  - 新增 `lyric_ipc.py`：仅标准库的 Unix Domain Socket 低延迟双向广播模块，支持迟到客户端快照同步与控制指令回传。
  - 新增 `desktop_lyrics.py`：基于系统 PyGObject (`Gtk 3.0` + `PangoCairo` + `Gdk.FrameClock` VSync 60Hz) 的 `512×64` 无边框置顶胶囊歌词窗（`ABOVE` + `STICKY` + `SKIP_TASKBAR` + `SKIP_PAGER`）；内置 `SyncedClock` 单调锁相 PLL 消除 IPC/缓冲抖动实现毫秒级时间对齐；支持 YRC 逐字粉彩扫光、超长句平滑滚动、鼠标左键拖动保存位置、滚轮微调时间偏移（`±50ms`/`±10ms`）、右键播放控制菜单与点击穿透锁定。
  - 集成进 `tui.py`：接入 `LyricIpcServer` 实时广播与反控，新增快捷键 `l`（启动/显示隐藏桌面歌词）与 `Shift+L`（切换鼠标穿透锁定），并在退出时清理子进程与 socket；更新 `README.md` 文档。
- 验证状态：已完成 `py_compile` 语法检查、YRC/PLL 时钟/IPC 双向通信单元测试、X11 真窗口属性与 `512×64` 几何验证，以及 Textual 无头集成测试。
- 精简 `desktop_lyrics.py` 视觉细节：移除切行缓入淡入/上浮动画改为直接闪现切换，并移除底部彩色进度发丝线。


## 2026-11-19
- 新增实验性全屏流质 Shader 视觉叠加层 `screen_shader.py` 并集成至 `ncm.py`（`shader` 子命令）与 `tui.py`（`Shift+V` 快捷键）：
  - 基于 `Gtk.GLArea` + `ctypes` (`libGL.so.1`) 运行 OpenGL 3.3 Core GLSL，实现全屏置顶透明与鼠标完全穿透（`input_shape_combine_region` + `set_pass_through`）。
  - 通过 `X11ScreenGrabber`（`XCompositeNameWindowPixmap` + `XRenderComposite` + `MIT-SHM`）在排除自身窗口的前提下实时合成底层桌面画面，使 Shader 直接以“下方屏幕内容透射扭曲程度 + RGB 三通道色散程度”为因变量，彻底移除刺眼白光与亮色贴纸感，维持画面原亮度。
  - 采用慢速粘稠颜料流场（3 层低频 `viscousFbm` + 粘滞 `pigmentField`）与非线性张力包络（`AudioBeatAnalyzer` 通过 `parec` 实时抓取 PipeWire `.monitor` 系统音频，无音频时 `presence -> 0` 趋近于无，重拍驱动色散与大尺度流体张力，边缘漏色沿用歌词同款粉彩渐变 `#8fd8c2 -> #b4a7e5 -> #e89eb8 -> #f0b7a4`）。
  - 支持 X11 全局热键 `Ctrl+Alt+V`（`X11GlobalHotkey`）与 TUI `Shift+V` 触发，入场/退场呈现从左下角扫向右上角的液态玻璃凸透镜水波。
- 验证状态：已通过 `py_compile` 语法检查、`X11ScreenGrabber` 1920×1080 非零 BGRA 抓屏验证、`AudioBeatAnalyzer` 静默/重拍脉冲单元测试，以及 `screen_shader.py --active` 与 `ncm.py shader --active` 实机运行验证（零崩溃/零 GLSL 报错）。
- 修复 `screen_shader.py` 递归采样自身叠加层的问题：在 `xfwm4` 合成器下 `XQueryTree(root)` 返回的是窗口管理器包裹的 Frame 父窗口而非客户端 `self_xid`，导致原 `win == self_xid` 未命中自身；现通过 `resolve_toplevel_xid` 沿 `XQueryTree` 父链定位并跳过自身顶层 Frame 窗口，并在 `self_xid` 就绪前禁止抓屏。
- 修复 `screen_shader.py` 采样包含顶栏导致画面下移偏移的问题：在 `_sync_grabber_config` 中改用 `gdk_win.get_origin()` 与 `gdk_win.get_width()/get_height()` 实时同步叠加窗口在屏幕上的真实坐标与尺寸，并监听 `configure-event`，使 `X11ScreenGrabber` 严格截取叠加窗口正下方 1:1 对齐的屏幕区域。
- 精校 `screen_shader.py`、`tui.py` 与 `lyric_ipc.py` 的鼓点动画判定与前瞻对齐引擎：
  - `lyric_ipc.py` 新增 `publish_peaks`（支持增量推送与迟到客户端全量补发），`tui.py` 复用 `PeakPrecomputer` 后台 25Hz FFT 预计算流，实时提取 `[kick, punch, mid, rms]` 鼓点频段时间轴并广播给 Shader。
  - `screen_shader.py` 新增 `SyncedClock`（毫秒锁相 PLL）与 `LookaheadPeakTimeline`：在 `pos + lookahead_ms`（默认 `75ms`）处做零相位对称三角窗采样，结合前后 `±1.25s` 局部相对峰值基准算出鼓点 FFT 相对强度 $x \in [0, 1]$，采用 $(1 - \cos(\pi x)) / 2$ 非线性曲线分别驱动变形程度与重拍色散程度，实现鼓点重心与视觉形变 100% 对齐。
  - 无 TUI 连接时的实时 `AudioBeatAnalyzer` 同步升级为 256 点基-2 FFT 鼓点频段提取 + 同款局部相对归一化与余弦非线性曲线；`ncm.py shader` 与 `screen_shader.py` 支持 `--lookahead-ms` 微调前瞻补偿。
- 全面升级 `screen_shader.py` 与 `tui.py` 的多频段细粒度音频驱动与液态光学渲染质感：
  - **多维音频特征（前瞻 + 实时统一）**：`tui.py` 将 24 频段 FFT 聚合为完整 8 频段与高频分量一并推送（向后兼容旧 4 维格式）；`screen_shader.py` 的 `LookaheadPeakTimeline` 与 `AudioBeatAnalyzer` 均提取 8 频段平滑频谱（`u_bands[8]`）、军鼓/中高频正向通量瞬态（`u_snare`）、高频泛音闪烁（`u_sparkle`）、频谱质心（`u_centroid`）及旋涡相位（`u_swirl`）。
  - **三尺度流体场与环绕频谱塑形**：在宏观低频粘稠流场中加入旋涡卷曲，叠加中频旋律驱动的脊线流场（`ridgedFlow`）与高频驱动的微涟漪法线扰动（`microGrad`）；屏幕四周边缘液态厚度与起伏按空间映射由对应 `u_bands[8]` 实时塑形（底低频、侧中频、顶高频）。
  - **4 路并发液态玻璃冲击涟漪环**：重拍上升沿发射厚重广域棱镜波环，军鼓/中高频瞬态发射纤细清脆的高光涟漪环，黄金角分布交错干涉。
  - **高级光学质感**：升级为 5-tap 光谱棱镜色散（红/琥珀/绿/青/蓝平滑过渡）、薄膜干涉虹彩（随频谱质心冷暖流转）与脊线/波环/高频微闪处的克制物理焦散亮丝。已通过 `py_compile`、音频分析单测及真实 OpenGL 3.3 Core GLSL 编译链接验证。
- 强化 `screen_shader.py` 高频高噪率动画、人声反馈，并新增可开关全屏大歌词显示（同步接入 `tui.py` 与 `ncm.py`）：
  - **人声与高频独立局部动态归一化**：`LookaheadPeakTimeline` 与 `AudioBeatAnalyzer` 新增局部 90%/88% 分位数动态归一化的 `u_vocal`（人声核心频段 `bands[2..4]`）及高灵敏度 `u_high` / `u_sparkle`，彻底解决低频主导下人声与高频响应偏弱的问题。
  - **高频高噪率微晶动画（`hfGrainField`）**：新增高空间频率（17.5/31.0）+ 高时间频率双层交错细粒噪点与离散微米星尘闪点；高频活跃时在边缘、脊线与波环处激起细密声波霜纹折射、碎钻级微粒棱镜色散与高频星尘闪烁，高频回落后回归镜面质感。
  - **人声丝绸焦散光带（`vocalRibbon`）**：强化人声驱动的 `ridgedFlow` 丝绸筋络折射、两侧向内延展幅度与粉彩流光丝带，人声开口/副歌拉升即刻清晰呼应。
  - **可开关全屏大歌词（`BigLyricsRenderer`）**：基于 Cairo + PangoCairo 离屏渲染 Premultiplied BGRA 大号粗体双语歌词（支持 YRC 逐字粉彩扫光、自适应缩字、柔和椭圆暗雾衬底与按需重绘），通过 `GL_TEXTURE1` 融合进 GLSL（带克制流场呼吸微晃与重拍边缘微色散），支持 TUI `Shift+K`、全局热键 `Ctrl+Alt+L`、CLI `--big-lyrics`/`--no-big-lyrics` 开关并持久化到 `~/.config/ncm-lite/shader_overlay.json`。已通过语法、人声/高频动态单测及 OpenGL 3.3 Core 编译链接验证。
- 将全屏流质 Shader 模块化拆分为 `shader_overlay/` 包（`shaders.py`、`x11_utils.py`、`audio_analyzer.py`、`big_lyrics.py`、`overlay_window.py`，`screen_shader.py` 保留为轻量兼容入口），并按实机视觉反馈重构音频视觉映射：
  - **扭曲强度回退至干净克制**：移除多叠层 `ridgedFlow` 与噪点扭曲，回归单层顺滑粘稠 `pigmentField`（中心文字区保持清晰锐利，边缘渗透与色散大幅收敛）。
  - **重拍随机灵动推力**：每次重拍上升沿生成独立随机强度倍率（`u_beat_rand: 0.58~1.42`）、随机推动方向（`u_beat_dir`）与弹性冲击包络（`u_beat_kick`），告别单调同线抖动。
  - **超低频（`u_sub`）克制声压**：20~70Hz 超低频驱动屏幕最外圈轻微向心呼吸缩放与克制暗角声压阴影，沉稳有力不花哨。
  - **高频四周彩漏与干净粒子（`highFreqParticles`）**：高频驱动屏幕边缘柔和粉彩漏出与两层网格散列星尘微粒闪烁飘散，纯光效叠加、零扭曲污染。已通过全部模块编译、音频分析单测及 OpenGL 3.3 Core 编译链接验证。

## 2026-11-20
- 进一步精调 `shader_overlay/` 的随机性、底鼓冲击与宽光线流动光影：
  - 修复 `overlay_window.py` 漏导入 `GL_BLEND` 导致的绘制异常。
  - 提升边缘变化随机性：在 `audio_analyzer.py` 中扩大重拍随机强度范围（`0.45 ~ 1.65`），并新增随机边缘聚光重心 `u_beat_focus` 与随机边缘调制相位 `u_edge_seed`；配合角度空间噪声让四周边缘伸展高度不均匀、每拍灵动切换。
  - 强化底鼓并与超低频明确区分：适当提高底鼓（`kickImpact`）的局部聚光折射与色散峰值，保留超低频（`u_sub`）稳重向心的全周微呼吸与暗角声压。
  - 周围漏光升级为不均匀的宽光线流动光影：`flowingAuroraColor` 替代固定粉色，在青绿/冰蓝/靛紫/珊瑚金/柔粉之间随角度与时间流动，叠加两层交错宽光束 `broadBeam`，形成有明有暗、宽窄错落的边缘流光。已通过 OpenGL 3.3 Core 编译链接与实机无报错启动验证。

## 2026-11-21
- 重构 `shader_overlay/` 与 `tui.py` 的低频音频分频与视觉映射，彻底解决超低频（808）与底鼓（Kick）区分度不足及持续 808 下低频消失/底鼓无反应的问题：
  - 新增零依赖时域低频分频模块 `shader_overlay/dsp.py`（`Biquad` + `LowEndFilterBank`：4× 盒式抗混叠降采样 + 16Hz DC 阻隔 + 4 阶 Butterworth 62Hz 低通 Sub + 4 阶 74Hz 高通 / 4 阶 175Hz 低通 Kick Body），在 `tui.py`（17 维向后兼容 IPC 帧）与 `shader_overlay/audio_analyzer.py` 实时路径统一接入。
  - 重构 `LookaheadPeakTimeline` 与 `AudioBeatAnalyzer`：持续超低频改用长时慢 AGC + 绝对声压保底（输出 `sub_sustain` / `sub_phase` 防止持续 808 消失）；底鼓改用削顶前 dB 域快慢双包络差瞬态通量（带鼓槌 `click` 协同与军鼓/中高频排异）+ 自适应中位数/MAD 局部峰值拾取（移除固定 `0.45` 回落锁死，新增线程安全峰值锁存与 `kick_ring`）。
  - 升级 `shader_overlay/shaders.py` 与 `overlay_window.py`：剥离 `u_sub` 对底鼓色散与张力的污染；超低频呈现全周沉稳厚玻璃向心呼吸、深邃冷靛/深紫声压暗角与慢速低频驻波涌动，底鼓呈现随机聚光点定向折射与脆利 RGB 棱镜色散闪击（并依实机观感移除了偏花的局部冲击波圆环 `u_kick_ring`）。
  - 验证状态：已通过 `py_compile`、45Hz/105Hz/400Hz 频域隔离度测试（17~30 dB）、持续 808 叠加 8 脚底鼓双路径合成回归测试（实时与前瞻均 8/8 精准触发且持续 808 `sub > 0.98`）、真实 FLAC 全曲预计算耗时基准、以及 OpenGL 3.3 Core GLSL 编译链接与实机启动验证。
- 在保持现有流质 Shader（Pass 1）100% 原汁原味的前提下，全面重构全屏歌词系统为 **GPU 动态排印液态水晶巨型歌词引擎（Pass 2）**：
  - **动态字形图集与独立 GPU 渲染管线**（`shader_overlay/big_lyrics.py`、`shader_overlay/shaders.py`、`shader_overlay/overlay_window.py`）：放弃每帧 CPU 全屏重绘，改为 2048×2048 按需 Row-Shelf 增量上传字形图集（思源宋体 `Noto Serif CJK SC/JP` Bold 主字 + `LXGW WenKai` 霞鹜文楷译文浮标），配合 OpenGL 硬件 `glGenerateMipmap` + `textureLod` 实现零卡顿（实测 ~0.32 ms/帧 CPU 开销）电影级镜头拉焦与柔和光晕。
  - **铺满 80% 屏幕的动态字号错落排印**：修正原 YRC `words` 结束时间戳解析；CJK 逐字独立切分、英文按视觉宽高比自适应换行；依据词性（汉字实词 vs 日中助词虚词）、YRC 拖长音 Hero 加权与交错韵律波赋予每字差异化字号（`0.62× ~ 1.56×`）；内置 5 套轮换电影构图流派（`MONUMENTAL` 巨幅海报 / `DIAGONAL_CASCADE` 斜瀑 / `TWIN_WAVE` 双峰潮汐 / `HERO_EDITORIAL` 杂志焦点 / `POETIC_TERRACE` 诗意梯田）+ 软斥力防重叠松弛 + 80% 主舞台自动等比撑满。
  - **每字独立流体运动学与液态水晶光学材质**：每字拥有独立双谐波持续漂浮、入场交错拉焦滑入、唱到瞬间二阶阻尼弹簧膨胀激发与斜向极光波前扫光、底鼓视差弹性与脆利 RGB 棱镜色散、以及切句多句交织风散流体溶解退场；`LYRIC_FRAG_SHADER` 实时采样 `u_screen` 实现巨字液态玻璃折射、菲涅尔刀锋亮边、同源全光谱流动极光与桌面明暗自适应对比度。
  - **验证状态**：已通过 `py_compile` 语法检查、多语言/短长句 80% 屏幕覆盖率与 120 帧耗时单测（0.321 ms/帧）、OpenGL 3.3 Core 双 Shader 真编译链接与 `glGetError == 0` 绘制验证，以及带真实 IPC 逐字流的 `screen_shader.py --active --big-lyrics` 端到端实机冒烟测试（`EXIT: 0`）。

## 2026-11-22
- 修复全屏动态歌词间奏后重复闪出上一句的时序 Bug，并新增当前字符位置浅浅辉光效果：
  - **修复间奏死区复活与对齐优化**（`shader_overlay/big_lyrics.py`）：重排 `_resolve_target` 判定优先级——先检查下一句预浮入窗口（间奏后提前 `0.36s`、连唱切句提前 `0.30s`），再无条件执行 `pos > end + hold_tail` 留白判断（移除原 `(next_start - pos) > 1.2` 导致的 0.96s 旧句复现死区）；同时优化前奏歌名留白过渡、提升 `lit_rate` 响应并校准 `sweepEdge` 扫光起止点以强化逐字对齐精度。
  - **当前字符位置浅辉光（Soft Current-Char Aura Halo）**（`shader_overlay/big_lyrics.py`、`shader_overlay/shaders.py`）：为每个字形新增 `active_glow` 丝滑包络（精准跟随当前正在唱的字符亮起并在切字时柔缓交棒），在字形背后先绘制宽幅零硬边椭圆 Halo Quad（`a_cell_uv.x = -1.0` 标记），由 `LYRIC_FRAG_SHADER` 以双层高斯柔雾衰减 + 同源流动极光色 + 流场微起伏 + 桌面明暗自适应收敛渲染出通透浅辉光，并微强当前字笔画贴体微光呼应。
  - **验证状态**：已通过 `py_compile` 语法检查、0~40s 20ms 步进间奏状态机全周期回归测试（零旧句复现）、Halo Quad 绘制顺序校验及 OpenGL 3.3 Core 真编译渲染验证（`glGetError == 0`）。
- 在桌面新增一键启动脚本 `/home/asumi/Desktop/ncm-lite.sh`（已设可执行权限并通过 `bash -n` 校验）：终端内直接启动 TUI，桌面双击时自动通过 `kitty`（回退 `xfce4-terminal`）拉起终端运行，并支持透传 `ncm.py` 子命令参数。
- 按实机观感彻底移除当前字符背底浅辉光（清除 `big_lyrics.py` 的 `active_glow` / Halo Quad 缓冲与 `shaders.py` 的 `isHalo` 分支），保持巨型歌词清爽通透，保留间奏防复现修复与逐字扫光对齐优化；已通过语法检查、间奏回归与 OpenGL 3.3 Core 编译验证。
- 全面优化网络请求提速/去重并大幅降低全栈 CPU 与内存占用（解决机身发热）：
  - **网络层连接池、多级缓存与单流 Tee 代理**（`ncm.py`、`tui.py`）：引入标准库线程安全 HTTP/1.1 Keep-Alive 连接池 `_KeepAlivePool` + Gzip 自动压缩（消除重复 TCP/TLS 握手 RTT）；用预展开轮密钥纯 Python `AES-128-ECB` 与 Pillow 内存半块真彩渲染取代每次 fork `openssl` / `chafa` 子进程；新增 `_MetadataCache`（`~/.cache/ncm-lite/` 持久化歌曲元数据与歌词 + 内存 LRU 缓存直链与封面）与大歌单多分片并发拉取；新增本地流式缓存代理 `AudioStreamProxy`（`resolve_cached_stream`），将直链头部校验、`mpv` 播放与 `ffmpeg` 频谱分析合并为单条 CDN 下载流（消除原三重重复拉流）；在 `tui.py` 实现切歌三路并发加载（直链/封面/歌词）与下一首后台静默预取（`_prefetch_next_track`）。
  - **CPU 与内存降温优化**（`tui.py`、`desktop_lyrics.py`、`shader_overlay/`）：
    - `PeakPrecomputer` 改为按当前播放进度自适应前瞻节流（保持超前播放位置 ~18s 并限制 `ffmpeg -threads 1`），彻底消除切歌瞬间 100% 单核 CPU 满载解码整曲的发热；
    - `tui.py` 为所有固定几何尺寸的动态高频组件（`AmbientBrand`、`VinylCoverStage`、`AmbientWaveVisualizer`、`LyricLine`、`SeekBar`、`time-cur`、`time-dur`、`cover-aura`、`status-label`）显式传入 `Static.update(..., layout=False)`，根治 Textual 默认 `layout=True` 导致每帧（20 FPS）对上千个歌单 `TrackItem` 做全屏 `_compositor.reflow()` 重排（占 ~58% 单核 CPU）的核心发热源；同时引入 256×50 粉彩取色查找表 `_PASTEL_LUT`、进度条/时间标签 DOM 脏检查、暂停/待机态自动降频（20 FPS -> 2.5 FPS）、大歌单 `lv.extend` 批量挂载与 `O(1)` 播放标记更新；
    - `shader_overlay/`：还原 `x11_utils.py`、`overlay_window.py` 与 `audio_analyzer.py` 的抓屏/每帧 `resolve_toplevel_xid` 父链解析与音频管线至 100% 原版等价（修复跳过 `resolve_toplevel_xid` 刷新后在 `xfwm4` 延迟 reparent / 显隐切换时未命中新 Frame 窗口导致的递归自采样扭曲叠加问题），仅保留 `GlyphAtlas` 2048×2048 (16MB) 画布按需延迟分配；
    - `desktop_lyrics.py` 新增 `Pango.Layout` 缓存与静态歌词帧跳过重绘机制。已通过全模块 `py_compile`、AES 字节级一致性校验与 Keep-Alive 实测验证。
- 分阶段推进全栈 CPU / GPU / 内存深度优化：
  - **阶段 1（FFT 与音频分析优化）**：在 `shader_overlay/dsp.py` 实现零 `complex` 对象分配的 256 点实数半长 FFT（`rfft256_mags` / `rfft256_mags_ring`，128 点复数蝶形 + 实频谱解包，双精度误差 `< 1e-14`），统一替换 `tui.py` 与 `audio_analyzer.py` 的重复 256 点复数 FFT 并消除切片与 `deque` 拷贝；为 `AudioBeatAnalyzer` 增加 `set_capture_enabled` 门控，在窗口隐藏或已有 TUI `lookahead` 数据时停止后台 `parec` 子进程与 67Hz 实时 FFT；为 `LookaheadPeakTimeline.sample_aligned` 增加帧级滑动窗口统计量缓存，消除 60Hz VSync 下相邻同帧重复 `sorted()`。
  - **阶段 2（Shader、X11 抓屏与大歌词图集优化）**：在 `FRAG_SHADER` 增加屏幕中央透明死区 Early-Out 裁剪并移除废弃的 `u_lyric_tex` 分支；将 `X11ScreenGrabber` 升级为 1/2 分辨率 `XRenderSetPictureTransform` 硬件缩放合成 + 预分配双缓冲乒乓复用 + `visual` 格式缓存（降低 75% SHM/PCIe 带宽并消除每秒 ~330MB `bytes` 堆分配，同时保留每帧 `resolve_toplevel_xid` 防 `xfwm4` 递归自采样）；将 `GlyphAtlas` 改为单通道 `cairo.FORMAT_A8` + OpenGL `GL_R8` 零拷贝指针上传并在关闭大歌词/隐藏窗口时彻底释放 CPU 表面与显存（图集内存与显存占用降低 75%~100%）；在 `LYRIC_FRAG_SHADER` 增加四角空白粗 Mipmap 提前 `discard` 并用硬件偏导 `dFdx/dFdy` 取代 4 次邻域法线采样；在静默暂停稳态下自动停止 60Hz 空转重绘。
  - **阶段 3（运行时内存与 IPC 传输优化）**：将 `ncm.py` 的 `AudioStreamProxy` 流式缓存目录从 `/tmp`（2.0G `tmpfs` 内存盘）迁至磁盘 `~/.cache/ncm-lite/streams/`、收敛 `MAX_CACHED_FILES = 2` 并自动清理死亡进程残留目录（释放 180~360MB 隐式 RAM 占用）；将 `tui.py` 的 `PeakTimeline` 与 `audio_analyzer.py` 的 `LookaheadPeakTimeline` 重构为单块连续 `array.array('f')` 存储（消除数十万个嵌套 `tuple` 与装箱 `float` 对象）；为 `lyric_ipc.py` 增加 `want_peaks` 握手按需订阅并移除服务端整曲 `_peaks_frames` 内存副本，使无 Shader 在线时 TUI 跳过 `_flush_peaks_ipc`、桌面歌词不再接收和解析冗余 `peaks` 流。
