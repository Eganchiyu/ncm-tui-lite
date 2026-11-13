#!/usr/bin/env python3
"""ncm-lite TUI —— 网易云音乐无损流媒体终端播放器（柔光慢动画版）。"""
from __future__ import annotations

import asyncio
import bisect
import math
import random
import sys
import time
from typing import Any

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, Static

import ncm
from player import MpvPlayer

# ── 柔和粉彩调色板（Sakura Twilight Pastel） ─────────────────────────────────
PASTEL_RING: tuple[tuple[int, int, int], ...] = (
    (143, 216, 194),  # #8fd8c2 薄荷海沫
    (163, 196, 232),  # #a3c4e8 雾蓝暮云
    (180, 167, 229),  # #b4a7e5 柔和薰衣草
    (232, 158, 184),  # #e89eb8 樱花浅粉
    (240, 183, 164),  # #f0b7a4 暖杏桃色
    (235, 211, 167),  # #ebd3a7 奶油淡金
)

BG_STAGE = "#171a29"
FG_PRIMARY = "#dde2f2"
FG_SECONDARY = "#97a1c4"
FG_MUTED = "#677196"
FG_MINT = "#8fd8c2"
FG_LAVENDER = "#b4a7e5"
FG_ROSE = "#e89eb8"
FG_PEACH = "#f0b7a4"
FG_CREAM = "#ebd3a7"


def _clamp_u8(v: float) -> int:
    return max(0, min(255, int(round(v))))


def _lerp_rgb(
    c1: tuple[int, int, int], c2: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    # 余弦缓动，让色相过渡更加柔顺
    eased = 0.5 - 0.5 * math.cos(math.pi * t)
    return (
        _clamp_u8(c1[0] + (c2[0] - c1[0]) * eased),
        _clamp_u8(c1[1] + (c2[1] - c1[1]) * eased),
        _clamp_u8(c1[2] + (c2[2] - c1[2]) * eased),
    )


def sample_pastel(phase: float) -> tuple[int, int, int]:
    """从闭合的柔和粉彩环中平滑取色（phase 为任意实数，周期为 1.0）。"""
    n = len(PASTEL_RING)
    pos = (phase % 1.0) * n
    idx = int(pos) % n
    nxt = (idx + 1) % n
    return _lerp_rgb(PASTEL_RING[idx], PASTEL_RING[nxt], pos - int(pos))


def rgb_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def pastel_hex(phase: float, brightness: float = 1.0) -> str:
    r, g, b = sample_pastel(phase)
    if brightness != 1.0:
        # 向深暮色基底 (#171a29) 柔和收敛，避免偏灰发脏
        base = (23, 26, 41)
        f = max(0.0, min(1.25, brightness))
        r = _clamp_u8(base[0] + (r - base[0]) * f)
        g = _clamp_u8(base[1] + (g - base[1]) * f)
        b = _clamp_u8(base[2] + (b - base[2]) * f)
    return f"#{r:02x}{g:02x}{b:02x}"


def gradient_text(
    content: str,
    phase: float,
    spread: float = 0.035,
    bold: bool = False,
    brightness: float = 1.0,
) -> Text:
    out = Text(no_wrap=True)
    for i, ch in enumerate(content):
        if ch == " ":
            out.append(" ")
            continue
        color = pastel_hex(phase + i * spread, brightness=brightness)
        style = f"bold {color}" if bold else color
        out.append(ch, style=style)
    return out


def fmt_time(seconds: float) -> str:
    if seconds <= 0:
        return "00:00"
    total = int(seconds)
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ── 慢动画 & ASCII 艺术组件 ──────────────────────────────────────────────────
class AmbientBrand(Static):
    """顶部柔光极光渐变品牌条 + 慢漂浮音符星尘。"""

    _NOTES = ("♪", "♫", "♩", "♬", "✦", "✧")
    _MOTES = "  ·  ˚  ｡  ·  ✦  ·  ˚  ✧  ·  "

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)
        self.render_frame(0.0, playing=False)

    def render_frame(self, t: float, playing: bool) -> None:
        phase = t * 0.035  # 约 28 秒完成一轮柔和全色环流转
        note_idx = int(t * (0.7 if playing else 0.3)) % len(self._NOTES)
        note = self._NOTES[note_idx]
        pulse = 0.82 + 0.18 * math.sin(t * 1.4)

        out = Text(no_wrap=True)
        out.append(f" {note} ", style=f"bold {pastel_hex(phase, pulse)}")
        out.append_text(gradient_text("ncm-lite", phase + 0.08, spread=0.045, bold=True))
        out.append("  ·  ", style=FG_MUTED)
        out.append("柔光无损音流", style=FG_SECONDARY)

        # 慢漂移的尾迹星尘
        motes = self._MOTES
        shift = int(t * (2.2 if playing else 1.0)) % len(motes)
        scrolled = motes[shift:] + motes[:shift]
        out.append("   ")
        for i, ch in enumerate(scrolled[:18]):
            if ch == " ":
                out.append(" ")
            else:
                twinkle = 0.45 + 0.45 * math.sin(t * 1.2 + i * 0.65)
                out.append(ch, style=pastel_hex(phase + 0.35 + i * 0.04, brightness=twinkle))
        self.update(out)


class VinylCoverStage(Static):
    """专辑封面 / ASCII 黑胶唱片机慢动画舞台。"""

    _FRAME_LINES = (
        "    {s0}   ╭─────────────────────────╮   {s1} ",
        "  {s2}   │  ╭─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─╮  │     ",
        "    ╭──┴──╯   {g0} ╭─────────╮ {g1}   ╰──┴──╮",
        "    │  ╭────  {g2} │  ( ◎ )  │ {g3}  ──╮  │ {arm0}",
        "    │  │ {g4}   ╰─────────╯   {g5} │  │ {arm1}",
        "    │  ╰────  {g6} ╌ ╌ ╌ ╌ ╌ {g7}  ────╯  │ {arm2}",
        "    │     ╰─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─╯     │ {arm3}",
        "    ╰─────────────────────────────────╯   ",
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)
        self.has_cover: bool = False
        self.render_ascii_frame(0.0, status="idle")

    def set_cover_ansi(self, ansi: str) -> None:
        self.has_cover = True
        self.update(Text.from_ansi(ansi))

    def clear_cover(self, t: float = 0.0, status: str = "idle") -> None:
        self.has_cover = False
        self.render_ascii_frame(t, status=status)

    def render_ascii_frame(self, t: float, status: str = "idle") -> None:
        if self.has_cover:
            return
        speed = 0.75 if status == "playing" else (0.5 if status == "loading" else 0.28)
        phase = t * 0.04
        ring_step = int(t * speed * 4.0) % 8

        # 唱片光影纹路缓慢旋转
        groove_chars = ("╱", "│", "╲", "─", "╱", "│", "╲", "─")
        g = [groove_chars[(ring_step + i) % 8] for i in range(8)]

        # 四周缓闪星点
        star_set = ("✦", "·", "˚", "✧", "·", " ")
        stars = []
        for idx in range(3):
            s_idx = int(t * 0.65 + idx * 2.1) % len(star_set)
            stars.append(star_set[s_idx])

        # 唱臂：待机时收起，加载/播放时轻搭在碟片上
        if status in ("playing", "loading"):
            arms = ("◎", "╱", "╱", "╰")
        else:
            arms = ("◎", "│", "│", " ")

        caption_map = {
            "idle": "·  s l o w   v i n y l   d r i f t  ·",
            "loading": "◐  w a k i n g   l o s s l e s s   s t r e a m  ◑",
            "playing": "♫  a n a l o g   p a s t e l   a u r a  ♫",
            "error": "·  n o   c o v e r   a r t w o r k  ·",
        }
        caption = caption_map.get(status, caption_map["idle"])

        lines = [
            f"    {stars[0]}   ╭─────────────────────────╮   {stars[1]} ",
            f"  {stars[2]}   │  ╭─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─╮  │     ",
            f"    ╭──┴──╯   {g[0]} ╭─────────╮ {g[1]}   ╰──┴──╮",
            f"    │  ╭────  {g[2]} │  ( ◎ )  │ {g[3]}  ──╮  │ {arms[0]}",
            f"    │  │ {g[4]}     ╰─────────╯     {g[5]} │  │ {arms[1]}",
            f"    │  ╰────  {g[6]} ╌ ╌ ╌ ╌ ╌ {g[7]}  ────╯  │ {arms[2]}",
            f"    │     ╰─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─╯     │ {arms[3]}",
            "    ╰─────────────────────────────────╯   ",
        ]

        out = Text()
        for row_idx, line in enumerate(lines):
            for col_idx, ch in enumerate(line):
                if ch == " ":
                    out.append(" ")
                elif ch in ("✦", "✧", "˚", "·"):
                    tw = 0.55 + 0.45 * math.sin(t * 1.3 + row_idx + col_idx * 0.3)
                    out.append(ch, style=pastel_hex(phase + 0.45 + row_idx * 0.07, brightness=tw))
                elif ch in ("◎",):
                    out.append(ch, style=f"bold {pastel_hex(phase + 0.15, brightness=1.05)}")
                elif ch in ("╱", "╲", "│", "─", "╌") and 12 <= col_idx <= 30 and 2 <= row_idx <= 5:
                    # 唱片沟槽反光带
                    shimmer = 0.65 + 0.35 * math.sin(t * 1.1 - col_idx * 0.25 + row_idx * 0.5)
                    out.append(ch, style=pastel_hex(phase + col_idx * 0.025, brightness=shimmer))
                elif ch in ("╭", "╮", "╰", "╯", "┴") and row_idx in (2, 3, 4, 5) and 14 <= col_idx <= 28:
                    out.append(ch, style=pastel_hex(phase + 0.28, brightness=0.88))
                else:
                    # 柔灰紫机身外框
                    out.append(ch, style=pastel_hex(phase + row_idx * 0.02, brightness=0.52))
            out.append("\n")

        # 底部呼吸感字母标语
        out.append("   ")
        out.append_text(
            gradient_text(
                caption,
                phase + 0.2,
                spread=0.022,
                brightness=0.72 + 0.22 * math.sin(t * 0.9),
            )
        )
        self.update(out)


class AmbientWaveVisualizer(Static):
    """3 行超细腻 Braille 慢速谐波声浪可视化组件。"""

    # Braille 点阵映射: (col 0..1, row 0..3) -> bit
    _DOT_BITS = (
        (0x01, 0x08),
        (0x02, 0x10),
        (0x04, 0x20),
        (0x40, 0x80),
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)
        self._energy: float = 0.16
        self.render_wave(0.0, state="idle")

    def render_wave(self, t: float, state: str = "idle") -> None:
        target = {
            "playing": 1.0,
            "buffering": 0.62,
            "paused": 0.24,
            "idle": 0.16,
        }.get(state, 0.16)
        self._energy += (target - self._energy) * 0.10

        width = max(24, min(52, (self.size.width or 36) - 2))
        sub_cols = width * 2
        # 2 行 Braille 共 8 个垂直子像素级别 (0..7)
        # 计算上下对称或平滑流动的复合慢谐波
        heights: list[float] = []
        env = self._energy
        for x in range(sub_cols):
            nx = x / max(1, sub_cols - 1)
            # 两端平滑衰减窗函数（Hann 窗）
            window = math.sin(math.pi * nx) ** 0.75
            w1 = math.sin(nx * 6.5 - t * 1.15)
            w2 = 0.55 * math.sin(nx * 11.3 + t * 0.72 + 1.1)
            w3 = 0.35 * math.cos(nx * 17.0 - t * 0.48 + 2.4)
            # 平滑包络，在 0.08..1.0 之间柔和呼吸
            amp = (0.48 + 0.52 * (0.5 + 0.5 * ((w1 + w2 + w3) / 1.9)))
            val = window * env * amp
            heights.append(max(0.0, min(1.0, val)))

        # 构造 2 行 Braille 矩阵 (row 0 为上半峰，row 1 为下半柔波)
        top_chars: list[str] = []
        mid_chars: list[str] = []
        for col in range(width):
            mask_top = 0
            mask_mid = 0
            for sub in (0, 1):
                h = heights[col * 2 + sub]
                # 映射至 0..7 像素高度（从底行向上填充）
                level = int(round(h * 7.0))
                # row 1 (mid_chars): 子行 0..3（top=0..bottom=3），仅取上半部形成柔波轮廓
                # 实际上把 8 级从底部 row=7 往上画到 row=7-level：
                # 这样两行拼成连续的 8-pixel 高精细波形！
                top_pixel = 7 - level
                for py in range(top_pixel, 8):
                    if py < 4:
                        mask_top |= self._DOT_BITS[py][sub]
                    else:
                        mask_mid |= self._DOT_BITS[py - 4][sub]
            top_chars.append(chr(0x2800 + mask_top))
            mid_chars.append(chr(0x2800 + mask_mid))

        phase = t * 0.05
        out = Text(no_wrap=True)

        # 第一行：波峰柔光
        for i, ch in enumerate(top_chars):
            c_phase = phase + i / max(1, width) * 0.55
            br = 0.55 + 0.45 * env
            out.append(ch, style=pastel_hex(c_phase, brightness=br))
        out.append("\n")

        # 第二行：波腹主体
        for i, ch in enumerate(mid_chars):
            c_phase = phase + 0.08 + i / max(1, width) * 0.55
            br = 0.65 + 0.35 * env
            out.append(ch, style=pastel_hex(c_phase, brightness=br))
        out.append("\n")

        # 第三行：水面倒影地平线
        ripple_chars = ("·", "╌", "─", "┄", " ")
        for i in range(width):
            nx = i / max(1, width - 1)
            win = math.sin(math.pi * nx)
            if win < 0.12:
                out.append(" ")
                continue
            wave_val = math.sin(nx * 10.0 - t * 0.9) * 0.5 + 0.5
            r_ch = "─" if wave_val > 0.55 else ("╌" if wave_val > 0.25 else "·")
            c_phase = phase + 0.15 + nx * 0.55
            out.append(r_ch, style=pastel_hex(c_phase, brightness=0.28 + 0.26 * env * win))

        self.update(out)


class PlaylistItem(ListItem):
    def __init__(self, playlist: dict[str, Any]) -> None:
        self.playlist = playlist
        name = playlist.get("name") or "未命名歌单"
        count = playlist.get("trackCount") or 0
        is_own = not playlist.get("subscribed")

        lbl_text = Text(no_wrap=True)
        if is_own:
            lbl_text.append("♥ ", style=FG_ROSE)
        else:
            lbl_text.append("✧ ", style=FG_LAVENDER)
        lbl_text.append(name, style=FG_PRIMARY)
        lbl_text.append(f" ({count})", style=FG_MUTED)
        super().__init__(Label(lbl_text))


class TrackItem(ListItem):
    def __init__(self, track: dict[str, Any], index: int, playing: bool = False) -> None:
        self.track = track
        self.track_index = index
        super().__init__(Label(self._build_text(playing)))
        if playing:
            self.add_class("playing-track")

    def _build_text(self, playing: bool) -> Text:
        dur = fmt_time((self.track.get("duration_ms") or 0) / 1000.0)
        t = Text(no_wrap=True)
        if playing:
            t.append("♫ ", style=f"bold {FG_ROSE}")
            t.append(self.track["name"], style=f"bold {FG_PEACH}")
            t.append(f" · {self.track['artists']}", style=FG_LAVENDER)
            t.append(f"  [{dur}]", style=FG_SECONDARY)
        else:
            t.append(f"{self.track_index + 1:>3}. ", style=FG_MUTED)
            t.append(self.track["name"], style=FG_PRIMARY)
            t.append(f" - {self.track['artists']}", style=FG_SECONDARY)
            t.append(f" [{dur}]", style=FG_MUTED)
        return t

    def set_playing(self, playing: bool) -> None:
        self.set_class(playing, "playing-track")
        self.query_one(Label).update(self._build_text(playing))


class LyricLine(Static):
    def __init__(self, sec: float, orig: str, trans: str, line_idx: int) -> None:
        self.sec = sec
        self.orig = orig
        self.trans = trans
        self.line_idx = line_idx
        self._last_distance: int | None = -999
        super().__init__("", markup=False)
        self.add_class("lyric-line")
        self.set_visual_state(None)

    def set_visual_state(self, distance: int | None) -> None:
        """根据距当前歌词行的距离渲染柔和景深层次。"""
        if distance == self._last_distance:
            return
        self._last_distance = distance

        t = Text(justify="center")
        if distance == 0:
            t.append("✦  ", style=FG_PEACH)
            t.append(self.orig, style="bold #f6dce8")
            t.append("  ✦", style=FG_PEACH)
            if self.trans:
                t.append("\n")
                t.append(self.trans, style="#c2b6e8")
        elif distance == 1:
            t.append(self.orig, style="#a6b0d6")
            if self.trans:
                t.append("\n")
                t.append(self.trans, style="#818bb3")
        elif distance == 2:
            t.append(self.orig, style="#848eb5")
            if self.trans:
                t.append("\n")
                t.append(self.trans, style="#6b7499")
        else:
            t.append(self.orig, style="#61698c")
            if self.trans:
                t.append("\n")
                t.append(self.trans, style="#4e5675")
        self.update(t)


class SeekBar(Static):
    """支持柔和粉彩渐变的点击跳转进度条。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)
        self.duration: float = 0.0
        self.position: float = 0.0

    def update_progress(
        self, position: float, duration: float, anim_t: float = 0.0, playing: bool = False
    ) -> None:
        self.position = max(0.0, position)
        self.duration = max(0.0, duration)
        width = max(18, (self.size.width or 44) - 2)
        ratio = min(1.0, self.position / self.duration) if self.duration > 0 else 0.0
        filled = int(round(width * ratio))
        filled = max(0, min(width - 1, filled)) if width > 1 else 0

        phase = anim_t * 0.04
        out = Text(no_wrap=True)

        # 左侧微光渐变已播段
        for i in range(filled):
            c_phase = phase + (i / max(1, width)) * 0.6
            out.append("━", style=pastel_hex(c_phase, brightness=0.95))

        # 呼吸感游标
        if width > 2:
            if playing:
                pulse_char = "◉" if int(anim_t * 1.5) % 2 == 0 else "◈"
                knob_color = pastel_hex(phase + ratio * 0.6, brightness=1.15)
            else:
                pulse_char = "◎"
                knob_color = FG_CREAM
            out.append(pulse_char, style=f"bold {knob_color}")
            rest = max(0, width - filled - 1)
        else:
            rest = max(0, width - filled)

        if rest > 0:
            out.append("─" * rest, style="#323854")

        self.update(out)

    def on_click(self, event: events.Click) -> None:
        if self.duration <= 0 or self.size.width <= 0:
            return
        ratio = max(0.0, min(1.0, event.x / max(1, self.size.width - 1)))
        target = ratio * self.duration
        app = self.app
        if isinstance(app, NcmTuiApp):
            app.seek_to(target)


class NcmTuiApp(App):
    TITLE = "ncm-lite"
    CSS = """
    Screen {
        background: #131521;
        color: #dde2f2;
    }

    #top-bar {
        height: 2;
        background: #171a29;
        border-bottom: solid #2a3048;
        padding: 0 2;
        content-align: left middle;
        layout: horizontal;
    }

    #brand {
        width: 1fr;
        content-align: left middle;
    }

    #user-badge {
        width: auto;
        content-align: right middle;
        color: #97a1c4;
    }

    #main-area {
        height: 1fr;
        padding: 0 1;
    }

    #left-pane {
        width: 26%;
        min-width: 22;
        border: round #2c334d;
        background: #161926;
    }

    #mid-pane {
        width: 37%;
        min-width: 28;
        border: round #2c334d;
        background: #151825;
    }

    #right-pane {
        width: 37%;
        min-width: 30;
        border: round #2c334d;
        padding: 0 1;
        background: #141724;
    }

    #left-pane:focus-within,
    #mid-pane:focus-within,
    #right-pane:focus-within {
        border: round #7e74ab;
    }

    .pane-header {
        height: 2;
        background: #1b1f31;
        color: #b4a7e5;
        text-style: bold;
        padding: 0 1;
        content-align: left middle;
        border-bottom: solid #262c44;
    }

    #filter-input {
        height: 3;
        border: none;
        border-bottom: solid #262c44;
        background: #181c2c;
        color: #dde2f2;
        padding: 0 1;
    }

    #filter-input:focus {
        background: #1d2236;
        border-bottom: solid #8e83b8;
    }

    ListView {
        background: transparent;
        height: 1fr;
    }

    ListItem {
        padding: 0 1;
        height: auto;
    }

    ListItem.--highlight {
        background: #252b42;
        color: #f4e6ee;
    }

    .playing-track {
        background: #212238;
        text-style: bold;
    }

    #cover-box {
        height: auto;
        min-height: 10;
        align: center middle;
        background: #171a29;
        border: round #2c334d;
        padding: 0 1;
        margin-top: 0;
        margin-bottom: 0;
    }

    #cover-art {
        width: auto;
        height: auto;
        content-align: center middle;
    }

    #cover-aura {
        height: 1;
        text-align: center;
        content-align: center middle;
        margin-bottom: 0;
    }

    #now-title {
        text-style: bold;
        color: #f4dce7;
        content-align: center middle;
        text-align: center;
        margin-top: 0;
    }

    #now-meta {
        color: #97a1c4;
        text-align: center;
        margin-bottom: 0;
    }

    #now-quality {
        color: #8fd8c2;
        text-align: center;
        text-style: bold;
        margin-bottom: 0;
    }

    #wave-viz {
        height: 3;
        content-align: center middle;
        text-align: center;
        margin: 0 0 0 0;
    }

    #lyric-scroll {
        height: 1fr;
        border-top: dashed #2b314a;
        padding: 0 0;
    }

    .lyric-line {
        padding: 0 1;
        margin-bottom: 1;
        color: #677196;
        text-align: center;
    }

    .lyric-line:hover {
        color: #c5cde8;
    }

    .lyric-active {
        background: #21253b;
        text-style: bold;
    }

    #player-bar {
        height: 5;
        background: #171a29;
        border-top: solid #2a3048;
        padding: 0 2;
    }

    #progress-row {
        height: 1;
        align: left middle;
    }

    #time-cur, #time-dur {
        width: 8;
        color: #aeb7d6;
        content-align: center middle;
    }

    #seek-bar {
        width: 1fr;
        content-align: center middle;
    }

    #controls-row {
        height: 3;
        align: left middle;
    }

    #controls-row Button {
        min-width: 6;
        height: 3;
        margin-right: 1;
        background: #23283d;
        border: none;
        color: #dce1f2;
    }

    #controls-row Button:hover {
        background: #313856;
        color: #f6e4ec;
    }

    #btn-play {
        background: #d98ba9;
        color: #141622;
        text-style: bold;
    }

    #btn-play:hover {
        background: #e89eb8;
        color: #141622;
    }

    #status-label {
        width: 1fr;
        content-align: right middle;
        color: #97a1c4;
    }

    Footer {
        background: #131521;
        color: #8c96ba;
    }
    """

    BINDINGS = [
        Binding("space", "toggle_pause", "播放/暂停"),
        Binding("p", "toggle_pause", "暂停", show=False),
        Binding("n", "next_track", "下一首"),
        Binding("b", "prev_track", "上一首"),
        Binding("left", "seek_back", "后退5s"),
        Binding("right", "seek_fwd", "快进5s"),
        Binding("comma", "seek_back_15", "后退15s", show=False),
        Binding("full_stop", "seek_fwd_15", "快进15s", show=False),
        Binding("s", "toggle_shuffle", "随机/顺序"),
        Binding("x", "shuffle_current_playlist", "随机播当前歌单"),
        Binding("slash", "focus_filter", "搜索"),
        Binding("escape", "clear_filter", "清除搜索", show=False),
        Binding("minus", "vol_down", "音量-", key_display="-"),
        Binding("equals_sign", "vol_up", "音量+", key_display="="),
        Binding("plus", "vol_up", "音量+", show=False),
        Binding("r", "reload_playlists", "刷新歌单"),
        Binding("q", "quit_app", "退出"),
    ]

    def __init__(self, level: str = "lossless", cookie: str = "") -> None:
        super().__init__()
        self.level = level
        self.cookie = cookie
        self.user_info: dict[str, Any] = {}
        self.playlists: list[dict[str, Any]] = []
        self.viewing_tracks: list[dict[str, Any]] = []
        self.filtered_tracks: list[dict[str, Any]] = []

        self.play_queue: list[dict[str, Any]] = []
        self.play_queue_name: str = ""
        self.current_index: int | None = None
        self.current_track: dict[str, Any] | None = None
        self.shuffle_mode: bool = True
        self.shuffle_bag: list[int] = []
        self.history: list[int] = []

        self.lyrics: list[tuple[float, str, str]] = []
        self.lyric_times: list[float] = []
        self.active_lyric_idx: int = -1

        self._load_seq: int = 0
        self._consecutive_failures: int = 0
        self._cover_cache: dict[str, str] = {}
        self._cover_status: str = "idle"
        self._player: MpvPlayer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tick_timer = None
        self._anim_origin: float = time.monotonic()

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield AmbientBrand(id="brand")
            yield Static("校验账号中...", id="user-badge")

        with Horizontal(id="main-area"):
            with Vertical(id="left-pane"):
                yield Static("✦ 我的歌单", classes="pane-header")
                yield ListView(id="playlist-list")

            with Vertical(id="mid-pane"):
                yield Static("♫ 歌曲列表", id="track-header", classes="pane-header")
                yield Input(placeholder="按 / 搜索歌名、歌手、专辑...", id="filter-input")
                yield ListView(id="track-list")

            with Vertical(id="right-pane"):
                with Vertical(id="cover-box"):
                    yield VinylCoverStage(id="cover-art")
                yield Static("", id="cover-aura", markup=False)
                yield Static("未播放歌曲", id="now-title")
                yield Static("从左侧选择歌单，按 Enter 或 x 开始播放", id="now-meta")
                yield Static(f"✦ 目标音质 · {self.level.upper()}", id="now-quality")
                yield AmbientWaveVisualizer(id="wave-viz")
                yield VerticalScroll(id="lyric-scroll")

        with Vertical(id="player-bar"):
            with Horizontal(id="progress-row"):
                yield Static("00:00", id="time-cur")
                yield SeekBar(id="seek-bar")
                yield Static("00:00", id="time-dur")
            with Horizontal(id="controls-row"):
                yield Button("⏮", id="btn-prev")
                yield Button("▶", id="btn-play")
                yield Button("⏭", id="btn-next")
                yield Button("🔀 随机", id="btn-mode")
                yield Button("↺ -5s", id="btn-back")
                yield Button("+5s ↻", id="btn-fwd")
                yield Static("音量 100% · 待机", id="status-label")
        yield Footer()

    async def on_mount(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._player = MpvPlayer(on_track_end=self._on_mpv_track_end)
        # 约 8.3 FPS 的柔缓动画刷新，低 CPU 占用且画面如丝般顺滑
        self._tick_timer = self.set_interval(0.12, self._tick_player_ui)
        asyncio.create_task(self._bootstrap())

    async def on_unmount(self) -> None:
        if self._tick_timer:
            self._tick_timer.stop()
            self._tick_timer = None
        if self._player:
            self._player.close()
            self._player = None

    def _on_mpv_track_end(self, reason: str) -> None:
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._handle_track_end_on_loop, reason)

    def _handle_track_end_on_loop(self, reason: str) -> None:
        if reason == "eof":
            self._consecutive_failures = 0
            asyncio.create_task(self._play_next(auto=True))
        elif reason == "error":
            self.notify("音频流读取失败，尝试下一首", severity="warning")
            asyncio.create_task(self._play_next(auto=True))

    async def _bootstrap(self) -> None:
        try:
            me = await asyncio.to_thread(ncm.whoami, self.cookie)
            prof = me.get("profile") or {}
            uid = prof.get("userId")
            if me.get("code") != 200 or not uid:
                self.query_one("#user-badge", Static).update("cookie 已失效，请先执行 login")
                self.notify("cookie 无效或已过期，请先运行 ncm.py login", severity="error")
                return
            self.user_info = prof
            badge = Text(no_wrap=True)
            if prof.get("vipType"):
                badge.append("✦ 黑胶VIP ", style=f"bold {FG_CREAM}")
            else:
                badge.append("· 普通会员 ", style=FG_SECONDARY)
            badge.append(str(prof.get("nickname", "")), style=FG_PRIMARY)
            self.query_one("#user-badge", Static).update(badge)

            playlists = await asyncio.to_thread(ncm.user_playlists, self.cookie, uid)
            self.playlists = playlists
            lv = self.query_one("#playlist-list", ListView)
            await lv.clear()
            for pl in playlists:
                await lv.append(PlaylistItem(pl))
            if playlists:
                lv.index = 0
                await self._load_playlist(playlists[0])
        except Exception as e:  # noqa: BLE001
            self.notify(f"初始化失败: {e}", severity="error")

    async def _load_playlist(self, playlist: dict[str, Any]) -> None:
        name = playlist.get("name") or "歌单"
        self.query_one("#track-header", Static).update(f"◌ 加载中: {name}...")
        try:
            tracks = await asyncio.to_thread(ncm.playlist_tracks, self.cookie, playlist["id"])
        except Exception as e:  # noqa: BLE001
            self.notify(f"加载歌单失败: {e}", severity="error")
            return
        self.viewing_tracks = tracks
        self.query_one("#filter-input", Input).value = ""
        await self._render_track_list(tracks, name)

    async def _render_track_list(self, tracks: list[dict[str, Any]], header_title: str | None = None) -> None:
        self.filtered_tracks = tracks
        if header_title is not None:
            self.query_one("#track-header", Static).update(f"♫ {header_title} ({len(tracks)}首)")
        lv = self.query_one("#track-list", ListView)
        await lv.clear()
        playing_id = self.current_track["id"] if self.current_track else None
        for idx, tr in enumerate(tracks):
            await lv.append(TrackItem(tr, idx, playing=(tr["id"] == playing_id)))

    @on(ListView.Selected, "#playlist-list")
    async def on_playlist_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, PlaylistItem):
            await self._load_playlist(event.item.playlist)

    @on(ListView.Selected, "#track-list")
    async def on_track_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, TrackItem):
            return
        selected = event.item.track
        # 将当前浏览歌单设为播放队列
        self.play_queue = list(self.viewing_tracks)
        pl_lv = self.query_one("#playlist-list", ListView)
        if pl_lv.highlighted_child and isinstance(pl_lv.highlighted_child, PlaylistItem):
            self.play_queue_name = pl_lv.highlighted_child.playlist.get("name") or ""
        try:
            q_idx = next(i for i, t in enumerate(self.play_queue) if t["id"] == selected["id"])
        except StopIteration:
            q_idx = 0
        self.shuffle_bag.clear()
        if self.current_index is not None:
            self.history.append(self.current_index)
        await self._play_index(q_idx)

    @on(Input.Changed, "#filter-input")
    async def on_filter_changed(self, event: Input.Changed) -> None:
        kw = event.value.strip().lower()
        if not kw:
            await self._render_track_list(self.viewing_tracks)
            return
        matched = [
            t
            for t in self.viewing_tracks
            if kw in t["name"].lower() or kw in t["artists"].lower() or kw in t["album"].lower()
        ]
        await self._render_track_list(matched)

    @on(Button.Pressed)
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-play":
            self.action_toggle_pause()
        elif bid == "btn-next":
            await self.action_next_track()
        elif bid == "btn-prev":
            await self.action_prev_track()
        elif bid == "btn-mode":
            self.action_toggle_shuffle()
        elif bid == "btn-back":
            self.action_seek_back()
        elif bid == "btn-fwd":
            self.action_seek_fwd()

    def _pick_next_shuffle_index(self) -> int | None:
        n = len(self.play_queue)
        if n == 0:
            return None
        if n == 1:
            return 0
        if not self.shuffle_bag:
            self.shuffle_bag = [i for i in range(n) if i != self.current_index]
            random.shuffle(self.shuffle_bag)
        return self.shuffle_bag.pop()

    async def _play_next(self, auto: bool = False) -> None:
        if not self.play_queue:
            if self.viewing_tracks:
                self.play_queue = list(self.viewing_tracks)
                self.shuffle_bag.clear()
            else:
                return
        if self.current_index is not None:
            self.history.append(self.current_index)
            if len(self.history) > 200:
                self.history.pop(0)
        if self.shuffle_mode:
            nxt = self._pick_next_shuffle_index()
        else:
            nxt = 0 if self.current_index is None else (self.current_index + 1) % len(self.play_queue)
        if nxt is not None:
            await self._play_index(nxt, auto=auto)

    async def _play_index(self, idx: int, auto: bool = False) -> None:
        if not (0 <= idx < len(self.play_queue)) or not self._player:
            return
        self._load_seq += 1
        seq = self._load_seq
        track = self.play_queue[idx]
        self.current_index = idx
        self.current_track = track

        self._cover_status = "loading"
        cover_stage = self.query_one("#cover-art", VinylCoverStage)
        cover_stage.clear_cover(time.monotonic() - self._anim_origin, status="loading")

        self.query_one("#now-title", Static).update(track["name"])
        self.query_one("#now-meta", Static).update(f"{track['artists']} · {track['album']}")
        self.query_one("#now-quality", Static).update(f"◌ 正在解析 {self.level.upper()} 无损直链...")
        self._refresh_playing_marker()

        try:
            info = await asyncio.to_thread(ncm.stream_url, self.cookie, track["id"], self.level)
        except Exception as e:  # noqa: BLE001
            if seq != self._load_seq:
                return
            self.notify(f"获取直链出错: {e}", severity="error")
            await self._skip_on_failure(track)
            return

        if seq != self._load_seq:
            return

        raw_url = info.get("url")
        if not raw_url:
            self.notify(f"跳过无版权/不可播曲目: {track['name']}", severity="warning")
            await self._skip_on_failure(track)
            return

        playable_url = await asyncio.to_thread(ncm.probe_playable_url, raw_url)
        if seq != self._load_seq:
            return

        ok = self._player.play(playable_url)
        if not ok:
            self.notify(f"播放器加载失败: {track['name']}", severity="warning")
            await self._skip_on_failure(track)
            return

        self._consecutive_failures = 0
        self._cover_status = "playing"
        lv = (info.get("level") or self.level).upper()
        fmt = (info.get("type") or "flac").upper()
        br = int((info.get("br") or 0) / 1000)
        size_mb = round((info.get("size") or 0) / 1048576, 1)
        self.query_one("#now-quality", Static).update(f"✦ {lv} · {fmt} · {br}kbps · {size_mb}MB")

        # 并行加载封面与歌词
        asyncio.create_task(self._load_cover_and_lyrics(track, seq))

    async def _skip_on_failure(self, track: dict[str, Any]) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= min(8, max(1, len(self.play_queue))):
            self.query_one("#now-quality", Static).update("连续多首不可播，已停止自动跳过")
            self._consecutive_failures = 0
            self._cover_status = "idle"
            return
        await asyncio.sleep(0.3)
        await self._play_next(auto=True)

    async def _load_cover_and_lyrics(self, track: dict[str, Any], seq: int) -> None:
        pic_url = track.get("pic_url") or ""
        cover_stage = self.query_one("#cover-art", VinylCoverStage)
        if pic_url:
            if pic_url in self._cover_cache:
                ansi = self._cover_cache[pic_url]
            else:
                cover_box = self.query_one("#cover-box")
                right_pane = self.query_one("#right-pane")
                pane_h = right_pane.size.height or 30
                max_w = max(20, min(38, (cover_box.size.width or 36) - 6))
                max_h = max(8, min(15, pane_h // 3))
                h = max(8, min(max_h, max_w // 2))
                w = min(max_w, h * 2)
                try:
                    ansi = await asyncio.to_thread(ncm.fetch_cover_ansi, pic_url, w, h)
                except Exception:  # noqa: BLE001
                    ansi = ""
                if ansi:
                    if len(self._cover_cache) > 40:
                        self._cover_cache.clear()
                    self._cover_cache[pic_url] = ansi
            if seq != self._load_seq:
                return
            if ansi:
                cover_stage.set_cover_ansi(ansi)
            else:
                self._cover_status = "error"
                cover_stage.clear_cover(time.monotonic() - self._anim_origin, status="error")
        else:
            self._cover_status = "error"
            cover_stage.clear_cover(time.monotonic() - self._anim_origin, status="error")

        try:
            lyrics = await asyncio.to_thread(ncm.song_lyric, self.cookie, track["id"])
        except Exception:  # noqa: BLE001
            lyrics = []
        if seq != self._load_seq:
            return
        await self._render_lyrics(lyrics)

    async def _render_lyrics(self, lyrics: list[tuple[float, str, str]]) -> None:
        self.lyrics = lyrics
        self.lyric_times = [row[0] for row in lyrics]
        self.active_lyric_idx = -1
        scroll = self.query_one("#lyric-scroll", VerticalScroll)
        await scroll.remove_children()
        if not lyrics:
            await scroll.mount(Static("✦ 纯音乐 / 暂无歌词 ✦", classes="lyric-line"))
            return
        widgets = [LyricLine(sec, orig, trans, i) for i, (sec, orig, trans) in enumerate(lyrics)]
        await scroll.mount_all(widgets)

    @on(events.Click)
    def on_any_click(self, event: events.Click) -> None:
        if isinstance(event.widget, LyricLine):
            self.seek_to(event.widget.sec)

    def _refresh_playing_marker(self) -> None:
        lv = self.query_one("#track-list", ListView)
        playing_id = self.current_track["id"] if self.current_track else None
        for item in lv.children:
            if isinstance(item, TrackItem):
                item.set_playing(item.track["id"] == playing_id)

    def _render_cover_aura(self, t: float, playing: bool, paused: bool) -> Text:
        """封面下方的一行缓慢流动粉彩光带与小黑胶转速指示。"""
        phase = t * 0.045
        disc_frames = ("◐", "◓", "◑", "◒")
        disc = disc_frames[int(t * (2.5 if playing else 0.7)) % 4]
        out = Text(no_wrap=True)
        if self.current_track:
            if playing and not paused:
                out.append(f"{disc} ", style=f"bold {pastel_hex(phase, 1.05)}")
                out.append_text(
                    gradient_text("· ˚ ｡ 粉彩流光律动中 ｡ ˚ ·", phase + 0.1, spread=0.025, brightness=0.82)
                )
                out.append(f" {disc}", style=f"bold {pastel_hex(phase + 0.5, 1.05)}")
            elif paused:
                out.append("◎  ·  ", style=FG_CREAM)
                out.append("时间暂停于此刻", style=FG_SECONDARY)
                out.append("  ·  ◎", style=FG_CREAM)
            else:
                out.append_text(gradient_text("· ˚ ✧ 正在载入音律 ✧ ˚ ·", phase, spread=0.03, brightness=0.78))
        else:
            out.append_text(gradient_text("· ˚ ｡ 静候下一段旋律 ｡ ˚ ·", phase, spread=0.025, brightness=0.68))
        return out

    def _tick_player_ui(self) -> None:
        if not self._player or not self.is_mounted:
            return
        try:
            brand = self.query_one("#brand", AmbientBrand)
            cover_stage = self.query_one("#cover-art", VinylCoverStage)
            cover_aura = self.query_one("#cover-aura", Static)
            wave_viz = self.query_one("#wave-viz", AmbientWaveVisualizer)
            time_cur = self.query_one("#time-cur", Static)
            time_dur = self.query_one("#time-dur", Static)
            seek_bar = self.query_one(SeekBar)
            play_btn = self.query_one("#btn-play", Button)
            status_lbl = self.query_one("#status-label", Static)
        except Exception:  # noqa: BLE001
            return

        t = time.monotonic() - self._anim_origin
        st = self._player.get_state()
        is_actively_playing = st.playing and not st.paused and not st.buffering

        dur = st.duration
        if dur <= 0 and self.current_track:
            dur = (self.current_track.get("duration_ms") or 0) / 1000.0
        pos = st.time_pos

        # 慢动画组件更新
        brand.render_frame(t, playing=is_actively_playing)
        if not cover_stage.has_cover:
            ascii_state = (
                "loading"
                if self._cover_status == "loading"
                else ("playing" if is_actively_playing else ("error" if self._cover_status == "error" else "idle"))
            )
            cover_stage.render_ascii_frame(t, status=ascii_state)
        cover_aura.update(self._render_cover_aura(t, playing=st.playing, paused=st.paused))

        wave_state = (
            "buffering"
            if st.buffering
            else ("paused" if st.paused else ("playing" if st.playing else "idle"))
        )
        wave_viz.render_wave(t, state=wave_state)

        time_cur.update(fmt_time(pos))
        time_dur.update(fmt_time(dur))
        seek_bar.update_progress(pos, dur, anim_t=t, playing=is_actively_playing)

        play_btn.label = "▶" if (st.paused or not st.playing) else "⏸"

        # 底部状态条微动画
        eq_frames = ("▁▂▄▃", "▂▄▆▄", "▃▆▄▂", "▄▃▂▄")
        spin_frames = ("◐", "◓", "◑", "◒")
        status_text = Text(no_wrap=True)
        status_text.append(f"音量 {int(st.volume)}% · ", style=FG_SECONDARY)
        if st.buffering:
            sp = spin_frames[int(t * 4) % 4]
            status_text.append(f"{sp} 缓冲中...", style=FG_CREAM)
        elif st.paused:
            status_text.append("⏸ 已暂停", style=FG_PEACH)
        elif st.playing:
            eq = eq_frames[int(t * 3.0) % 4]
            status_text.append(f"{eq} ", style=pastel_hex(t * 0.05, brightness=1.0))
            status_text.append("播放中", style=FG_MINT)
        else:
            status_text.append("✧ 待机", style=FG_MUTED)
        status_lbl.update(status_text)

        # 歌词丝滑滚动 + 景深渐变高亮
        if self.lyric_times and st.playing:
            idx = bisect.bisect_right(self.lyric_times, pos + 0.15) - 1
            if idx != self.active_lyric_idx:
                scroll = self.query_one("#lyric-scroll", VerticalScroll)
                lines = list(scroll.query(LyricLine))
                prev_idx = self.active_lyric_idx
                if 0 <= prev_idx < len(lines):
                    lines[prev_idx].remove_class("lyric-active")
                self.active_lyric_idx = idx

                # 只更新新旧锚点邻近几行的景深颜色，保持零卡顿
                touched: set[int] = set()
                for anchor in (prev_idx, idx):
                    if anchor >= 0:
                        for j in range(max(0, anchor - 3), min(len(lines), anchor + 4)):
                            touched.add(j)
                for j in touched:
                    dist = abs(j - idx) if idx >= 0 else None
                    lines[j].set_visual_state(dist if (dist is not None and dist <= 2) else None)

                if 0 <= idx < len(lines):
                    target = lines[idx]
                    target.add_class("lyric-active")
                    scroll.scroll_to_widget(
                        target,
                        animate=True,
                        duration=0.55,
                        easing="out_cubic",
                        center=True,
                        force=True,
                    )

    def seek_to(self, seconds: float) -> None:
        if self._player:
            self._player.seek_to(seconds)

    def action_toggle_pause(self) -> None:
        if not self._player:
            return
        if not self.current_track and self.viewing_tracks:
            asyncio.create_task(self.action_shuffle_current_playlist())
            return
        self._player.toggle_pause()

    async def action_next_track(self) -> None:
        await self._play_next(auto=False)

    async def action_prev_track(self) -> None:
        if not self.play_queue:
            return
        if self.history:
            prev = self.history.pop()
        elif self.current_index is not None:
            prev = (self.current_index - 1) % len(self.play_queue)
        else:
            prev = 0
        await self._play_index(prev)

    def action_seek_back(self) -> None:
        if self._player:
            self._player.seek_relative(-5.0)

    def action_seek_fwd(self) -> None:
        if self._player:
            self._player.seek_relative(5.0)

    def action_seek_back_15(self) -> None:
        if self._player:
            self._player.seek_relative(-15.0)

    def action_seek_fwd_15(self) -> None:
        if self._player:
            self._player.seek_relative(15.0)

    def action_toggle_shuffle(self) -> None:
        self.shuffle_mode = not self.shuffle_mode
        self.shuffle_bag.clear()
        btn = self.query_one("#btn-mode", Button)
        btn.label = "🔀 随机" if self.shuffle_mode else "🔁 顺序"
        self.notify("已切换为随机播放" if self.shuffle_mode else "已切换为列表顺序循环")

    async def action_shuffle_current_playlist(self) -> None:
        if not self.viewing_tracks:
            return
        self.play_queue = list(self.viewing_tracks)
        self.shuffle_mode = True
        self.shuffle_bag.clear()
        self.query_one("#btn-mode", Button).label = "🔀 随机"
        idx = random.randrange(len(self.play_queue))
        await self._play_index(idx)

    def action_focus_filter(self) -> None:
        self.query_one("#filter-input", Input).focus()

    def action_clear_filter(self) -> None:
        inp = self.query_one("#filter-input", Input)
        if inp.value:
            inp.value = ""
        self.query_one("#track-list", ListView).focus()

    def action_vol_down(self) -> None:
        if self._player:
            self._player.add_volume(-5.0)

    def action_vol_up(self) -> None:
        if self._player:
            self._player.add_volume(5.0)

    async def action_reload_playlists(self) -> None:
        await self._bootstrap()
        self.notify("歌单已刷新")

    async def action_quit_app(self) -> None:
        if self._tick_timer:
            self._tick_timer.stop()
            self._tick_timer = None
        if self._player:
            self._player.close()
            self._player = None
        self.exit()


def run_tui(level: str = "lossless") -> int:
    cookie = ncm.load_cookie()
    app = NcmTuiApp(level=level, cookie=cookie)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(run_tui())
