#!/usr/bin/env python3
"""ncm-lite TUI —— 网易云音乐无损流媒体终端播放器（柔光慢动画 + 真FFT频谱/波形版）。"""
from __future__ import annotations

import array
import asyncio
import bisect
import math
import random
import subprocess
import sys
import threading
import time
from typing import Any

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, Static

import ncm
from desktop_lyrics import launch_desktop_lyrics
from lyric_ipc import LyricIpcServer
from screen_shader import launch_screen_shader
from shader_overlay.dsp import FFT_N, LowEndFilterBank, rfft256_mags
from player import MpvPlayer, PlayerState

# ── 柔和粉彩调色板（Sakura Twilight Pastel） ─────────────────────────────────
PASTEL_RING: tuple[tuple[int, int, int], ...] = (
    (143, 216, 194),  # #8fd8c2 薄荷海沫
    (163, 196, 232),  # #a3c4e8 雾蓝暮云
    (180, 167, 229),  # #b4a7e5 柔和薰衣草
    (232, 158, 184),  # #e89eb8 樱花浅粉
    (240, 183, 164),  # #f0b7a4 暖杏桃色
    (235, 211, 167),  # #ebd3a7 奶油淡金
)

FG_PRIMARY = "#dde2f2"
FG_SECONDARY = "#97a1c4"
FG_MUTED = "#677196"
FG_MINT = "#8fd8c2"
FG_LAVENDER = "#b4a7e5"
FG_ROSE = "#e89eb8"
FG_PEACH = "#f0b7a4"
FG_CREAM = "#ebd3a7"

# ── 预计算音频参数（12kHz 单声道，40ms / 帧 = 25Hz，256 点 FFT -> 24 对数频段） ──
PEAK_SR = 12000
PEAK_HOP = 480  # 40ms/frame -> 25 FPS
PEAK_FPS = PEAK_SR / PEAK_HOP  # 25.0
NUM_BANDS = 24

_BAND_RANGES: list[tuple[int, int, float]] = []
for _b in range(NUM_BANDS):
    _f0 = 1.0 * (126.0 ** (_b / NUM_BANDS))
    _f1 = 1.0 * (126.0 ** ((_b + 1) / NUM_BANDS))
    _i0 = max(1, int(math.floor(_f0)))
    _i1 = max(_i0 + 1, min(FFT_N // 2, int(math.ceil(_f1))))
    # 高频轻微倾斜补偿，让人声与镲片和低频鼓点能量均衡
    _tilt = 0.72 + 0.85 * (_b / (NUM_BANDS - 1))
    _BAND_RANGES.append((_i0, _i1, _tilt))


_CLICK_BIN_LO = max(1, int(round(1600.0 * FFT_N / PEAK_SR)))
_CLICK_BIN_HI = min(FFT_N // 2, int(round(4500.0 * FFT_N / PEAK_SR)) + 1)


def _compute_fft_bands_and_click(
    samples: list[float], offset: int = 0
) -> tuple[tuple[float, ...], float]:
    """256 点实数半长 FFT：返回 (24 对数频段感知能量 0..1, 1.6~4.5kHz 未削顶线性敲击能量)。"""
    mags = rfft256_mags(samples, offset)
    out: list[float] = []
    for i0, i1, tilt in _BAND_RANGES:
        pk = max(mags[i0:i1]) * tilt
        out.append(min(1.0, math.log1p(pk * 5.2) / 2.25))
    click_slice = mags[_CLICK_BIN_LO:_CLICK_BIN_HI]
    if click_slice:
        click_lin = math.sqrt(sum(m * m for m in click_slice) / len(click_slice))
    else:
        click_lin = 0.0
    return tuple(out), click_lin


def _clamp_u8(v: float) -> int:
    return max(0, min(255, int(round(v))))


def _lerp_rgb(
    c1: tuple[int, int, int], c2: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    eased = 0.5 - 0.5 * math.cos(math.pi * t)
    return (
        _clamp_u8(c1[0] + (c2[0] - c1[0]) * eased),
        _clamp_u8(c1[1] + (c2[1] - c1[1]) * eased),
        _clamp_u8(c1[2] + (c2[2] - c1[2]) * eased),
    )


def sample_pastel(phase: float) -> tuple[int, int, int]:
    """从闭合的柔和粉彩环中平滑取色（phase 周期为 1.0）。"""
    n = len(PASTEL_RING)
    pos = (phase % 1.0) * n
    idx = int(pos) % n
    nxt = (idx + 1) % n
    return _lerp_rgb(PASTEL_RING[idx], PASTEL_RING[nxt], pos - int(pos))


_PASTEL_LUT: dict[tuple[int, int], str] = {}


def pastel_hex(phase: float, brightness: float = 1.0) -> str:
    """基于 256×40 量化查找表的零分配粉彩取色（消除每帧数百次 cos 与字符串格式化）。"""
    p_bin = int((phase % 1.0) * 256.0) & 255
    b_bin = max(0, min(50, int(round(brightness * 40.0))))
    key = (p_bin, b_bin)
    cached = _PASTEL_LUT.get(key)
    if cached is not None:
        return cached

    r, g, b = sample_pastel(p_bin / 256.0)
    if b_bin != 40:
        base = (23, 26, 41)
        f = b_bin / 40.0
        r = _clamp_u8(base[0] + (r - base[0]) * f)
        g = _clamp_u8(base[1] + (g - base[1]) * f)
        b = _clamp_u8(base[2] + (b - base[2]) * f)
    res = f"#{r:02x}{g:02x}{b:02x}"
    _PASTEL_LUT[key] = res
    return res


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


# ── 流式音频 Peak & FFT 频谱预计算引擎 ────────────────────────────────────────
class PeakTimeline:
    """线程安全的渐进式音频波形 Peak + 24 频段 FFT 频谱时间轴（25Hz 紧凑 float32 连续数组）。"""

    # 每帧固定 30 个 float32:
    # [0]: peak, [1]: rms, [2..25]: bands_24, [26]: sub_rms, [27]: body_rms, [28]: body_peak, [29]: click_lin
    STRIDE = 2 + NUM_BANDS + 4  # 30

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: array.array = array.array("f")
        self.done: bool = False

    def frame_count(self) -> int:
        with self._lock:
            return len(self._data) // self.STRIDE

    def computed_seconds(self) -> float:
        with self._lock:
            return (len(self._data) // self.STRIDE) / PEAK_FPS

    def append_flat(self, flat: array.array) -> None:
        if not flat:
            return
        with self._lock:
            self._data.extend(flat)

    def copy_range(self, start_idx: int) -> tuple[int, array.array] | None:
        s = self.STRIDE
        with self._lock:
            total = len(self._data) // s
            if total <= start_idx:
                return None
            return start_idx, self._data[start_idx * s : total * s]

    def mark_done(self) -> None:
        with self._lock:
            self.done = True

    def sample_spectrum(self, sec: float) -> tuple[float, float, list[float]] | None:
        """线性插值读取指定秒数的 (peak, rms, 24频段能量列表)。"""
        if sec < 0:
            sec = 0.0
        idx_f = sec * PEAK_FPS
        idx0 = int(idx_f)
        frac = idx_f - idx0
        s = self.STRIDE
        with self._lock:
            d = self._data
            n = len(d) // s
            if n == 0 or idx0 >= n:
                return None
            o0 = idx0 * s
            p0 = d[o0]
            r0 = d[o0 + 1]
            if idx0 + 1 >= n:
                return p0, r0, [d[o0 + 2 + k] for k in range(NUM_BANDS)]
            o1 = o0 + s
            p1 = d[o1]
            r1 = d[o1 + 1]
            bands = [
                d[o0 + 2 + k] + (d[o1 + 2 + k] - d[o0 + 2 + k]) * frac
                for k in range(NUM_BANDS)
            ]
        return p0 + (p1 - p0) * frac, r0 + (r1 - r0) * frac, bands

    def sample_waveform(
        self, center_sec: float, half_span_sec: float, count: int
    ) -> list[tuple[float, float]] | None:
        """读取 [center - span, center + span] 区间 count 个点的 (peak, rms)。"""
        s = self.STRIDE
        with self._lock:
            d = self._data
            n = len(d) // s
            if n == 0:
                return None
            max_idx = n - 1
            if center_sec * PEAK_FPS > max_idx + 12:
                return None
            out: list[tuple[float, float]] = []
            for i in range(count):
                rel = ((i / max(1, count - 1)) * 2.0 - 1.0) * half_span_sec
                idx = int(round((center_sec + rel) * PEAK_FPS))
                if 0 <= idx <= max_idx:
                    off = idx * s
                    out.append((d[off], d[off + 1]))
                else:
                    out.append((0.0, 0.0))
            return out


class PeakPrecomputer:
    """后台使用 ffmpeg 渐进式节流预计算当前歌曲的 25Hz 波形 Peak 与 24 频段 FFT 频谱。"""

    LOOKAHEAD_HIGH_SEC = 18.0
    LOOKAHEAD_LOW_SEC = 13.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None

    def start(
        self,
        url: str,
        timeline: PeakTimeline,
        get_play_pos: Any = None,
    ) -> None:
        self.cancel()
        self._stop_flag = threading.Event()
        ev = self._stop_flag
        self._thread = threading.Thread(
            target=self._worker,
            args=(url, timeline, ev, get_play_pos),
            daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        self._stop_flag.set()
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=0.5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    def _worker(
        self,
        url: str,
        timeline: PeakTimeline,
        stop_ev: threading.Event,
        get_play_pos: Any = None,
    ) -> None:
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
        ]
        if url.startswith(("http://", "https://")) and not url.startswith("http://127.0.0.1:"):
            cmd.extend(
                [
                    "-user_agent",
                    ncm.UA_WEB,
                    "-headers",
                    f"Referer: {ncm.REFERER}\r\n",
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_delay_max",
                    "2",
                ]
            )
        cmd.extend(
            [
                "-i",
                url,
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(PEAK_SR),
                "-f",
                "s16le",
                "pipe:1",
            ]
        )
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            timeline.mark_done()
            return

        with self._lock:
            if stop_ev.is_set():
                proc.terminate()
                return
            self._proc = proc

        frame_bytes = PEAK_HOP * 2  # 480 samples * 2 bytes = 960 bytes/frame
        read_chunk = frame_bytes * 10  # 每 400ms 音频块批量处理一次
        leftover = b""
        win_offset = (PEAK_HOP - FFT_N) // 2
        low_filter = LowEndFilterBank(PEAK_SR, decimate=4)

        try:
            assert proc.stdout is not None
            while not stop_ev.is_set():
                # 渐进式前瞻节流：始终保持领先当前播放进度 ~18 秒，避免切歌瞬间 100% CPU 满载狂算整首歌
                if get_play_pos is not None:
                    comp_sec = timeline.computed_seconds()
                    try:
                        cur_pos = float(get_play_pos())
                    except Exception:  # noqa: BLE001
                        cur_pos = 0.0
                    if comp_sec > cur_pos + self.LOOKAHEAD_HIGH_SEC:
                        while not stop_ev.is_set():
                            if stop_ev.wait(timeout=0.25):
                                break
                            try:
                                cur_pos = float(get_play_pos())
                            except Exception:  # noqa: BLE001
                                break
                            if timeline.computed_seconds() <= cur_pos + self.LOOKAHEAD_LOW_SEC:
                                break
                        if stop_ev.is_set():
                            break

                chunk = proc.stdout.read(read_chunk)
                if not chunk:
                    break
                buf = leftover + chunk
                usable = (len(buf) // frame_bytes) * frame_bytes
                leftover = buf[usable:]
                if usable == 0:
                    continue

                pcm = array.array("h")
                pcm.frombytes(buf[:usable])
                if sys.byteorder != "little":
                    pcm.byteswap()

                flat_batch = array.array("f")
                inv = 1.0 / PEAK_HOP
                inv_scale = 1.0 / 32768.0
                hop_floats = [0.0] * PEAK_HOP
                for start in range(0, len(pcm), PEAK_HOP):
                    mx = 0
                    s_sq = 0.0
                    for j in range(PEAK_HOP):
                        raw_s = pcm[start + j]
                        ar = raw_s if raw_s >= 0 else -raw_s
                        if ar > mx:
                            mx = ar
                        x = raw_s * inv_scale
                        hop_floats[j] = x
                        s_sq += x * x
                    peak = min(1.0, (mx * inv_scale) ** 0.72)
                    rms = min(1.0, (math.sqrt(s_sq * inv) ** 0.72) * 1.45)

                    sub_rms, body_rms, body_peak, _sub_pk = low_filter.process_block(hop_floats)
                    bands, click_lin = _compute_fft_bands_and_click(hop_floats, win_offset)
                    flat_batch.extend((peak, rms, *bands, sub_rms, body_rms, body_peak, click_lin))
                timeline.append_flat(flat_batch)
        except Exception:  # noqa: BLE001
            pass
        finally:
            timeline.mark_done()
            try:
                if proc.stdout:
                    proc.stdout.close()
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=0.5)
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                if self._proc is proc:
                    self._proc = None


# ── 慢动画 & ASCII 艺术组件 ──────────────────────────────────────────────────
class AmbientBrand(Static):
    """顶部柔光极光渐变品牌条 + 慢漂浮音符星尘。"""

    _NOTES = ("♪", "♫", "♩", "♬", "✦", "✧")
    _MOTES = "  ·  ˚  ｡  ·  ✦  ·  ˚  ✧  ·  "

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)
        self.render_frame(0.0, playing=False)

    def render_frame(self, t: float, playing: bool) -> None:
        phase = t * 0.035
        note_idx = int(t * (0.7 if playing else 0.3)) % len(self._NOTES)
        note = self._NOTES[note_idx]
        pulse = 0.82 + 0.18 * math.sin(t * 1.4)

        out = Text(no_wrap=True)
        out.append(f" {note} ", style=f"bold {pastel_hex(phase, pulse)}")
        out.append_text(gradient_text("ncm-lite", phase + 0.08, spread=0.045, bold=True))
        out.append(" · ", style=FG_MUTED)
        out.append("柔光无损音流", style=FG_SECONDARY)

        motes = self._MOTES
        shift = int(t * (2.2 if playing else 1.0)) % len(motes)
        scrolled = motes[shift:] + motes[:shift]
        out.append("  ")
        for i, ch in enumerate(scrolled[:16]):
            if ch == " ":
                out.append(" ")
            else:
                twinkle = 0.45 + 0.45 * math.sin(t * 1.2 + i * 0.65)
                out.append(ch, style=pastel_hex(phase + 0.35 + i * 0.04, brightness=twinkle))
        self.update(out, layout=False)


class VinylCoverStage(Static):
    """紧凑版专辑封面 / ASCII 黑胶唱片机慢动画舞台（6 行高）。"""

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

        groove_chars = ("╱", "│", "╲", "─", "╱", "│", "╲", "─")
        g = [groove_chars[(ring_step + i) % 8] for i in range(6)]

        star_set = ("✦", "·", "˚", "✧", "·", " ")
        stars = [star_set[int(t * 0.65 + idx * 2.1) % len(star_set)] for idx in range(2)]
        arms = ("◎", "╱", "╰") if status in ("playing", "loading") else ("◎", "│", " ")

        caption_map = {
            "idle": "· s l o w   v i n y l   d r i f t ·",
            "loading": "◐ w a k i n g   s t r e a m ◑",
            "playing": "♫ a n a l o g   p a s t e l ♫",
            "error": "· n o   c o v e r   a r t ·",
        }
        caption = caption_map.get(status, caption_map["idle"])

        lines = [
            f"  {stars[0]}  ╭───────────────────────────╮  {stars[1]} ",
            f"   ╭─┴─╮   {g[0]}  ╭───────────╮  {g[1]}   ╭─┴─╮",
            f"   │ ╭─╯ {g[2]}   │   ( ◎ )   │   {g[3]} ╰─╮ │ {arms[0]}",
            f"   │ ╰─╮ {g[4]}   ╰───────────╯   {g[5]} ╭─╯ │ {arms[1]}",
            f"   ╰───┴─────── ╌ ╌ ╌ ╌ ╌ ───────┴───╯ {arms[2]}",
        ]

        out = Text()
        for row_idx, line in enumerate(lines):
            for col_idx, ch in enumerate(line):
                if ch == " ":
                    out.append(" ")
                elif ch in ("✦", "✧", "˚", "·"):
                    tw = 0.55 + 0.45 * math.sin(t * 1.3 + row_idx + col_idx * 0.3)
                    out.append(ch, style=pastel_hex(phase + 0.45 + row_idx * 0.07, brightness=tw))
                elif ch == "◎":
                    out.append(ch, style=f"bold {pastel_hex(phase + 0.15, brightness=1.08)}")
                elif ch in ("╱", "╲", "│", "─", "╌") and 10 <= col_idx <= 32 and 1 <= row_idx <= 4:
                    shimmer = 0.65 + 0.35 * math.sin(t * 1.1 - col_idx * 0.25 + row_idx * 0.5)
                    out.append(ch, style=pastel_hex(phase + col_idx * 0.025, brightness=shimmer))
                elif ch in ("╭", "╮", "╰", "╯") and row_idx in (1, 2, 3) and 13 <= col_idx <= 27:
                    out.append(ch, style=pastel_hex(phase + 0.28, brightness=0.88))
                else:
                    out.append(ch, style=pastel_hex(phase + row_idx * 0.03, brightness=0.52))
            out.append("\n")

        out.append("  ")
        out.append_text(
            gradient_text(
                caption,
                phase + 0.2,
                spread=0.024,
                brightness=0.72 + 0.22 * math.sin(t * 0.9),
            )
        )
        self.update(out, layout=False)


class AmbientWaveVisualizer(Static):
    """5 行高真 FFT 频谱柱状图(+Peak 峰值帽) & 真实对称采样波形图。"""

    _BAR_BLOCKS = (" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")
    _VIZ_ROWS = 5
    _SUB_PER_ROW = 8

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)
        self.viz_mode: str = "spectrum"  # "spectrum" | "waveform"
        self._energy: float = 0.14
        self._bar_smooth: list[float] = [0.0] * NUM_BANDS
        self._peak_hold: list[float] = [0.0] * NUM_BANDS
        self._hold_ticks: list[int] = [0] * NUM_BANDS
        self._wave_smooth: list[float] = []
        self.render_wave(0.0, state="idle")

    def toggle_mode(self) -> str:
        self.viz_mode = "waveform" if self.viz_mode == "spectrum" else "spectrum"
        return self.viz_mode

    def render_wave(
        self,
        t: float,
        state: str = "idle",
        pos: float = 0.0,
        timeline: PeakTimeline | None = None,
    ) -> None:
        target = {
            "playing": 1.0,
            "buffering": 0.45,
            "paused": 0.16,
            "idle": 0.12,
        }.get(state, 0.12)
        self._energy += (target - self._energy) * 0.18

        width = max(24, min(58, (self.size.width or 38) - 2))
        if self.viz_mode == "waveform":
            self._render_waveform_mode(t, state, pos, timeline, width)
        else:
            self._render_spectrum_mode(t, state, pos, timeline, width)

    def _render_spectrum_mode(
        self,
        t: float,
        state: str,
        pos: float,
        timeline: PeakTimeline | None,
        width: int,
    ) -> None:
        spec = timeline.sample_spectrum(pos) if (timeline and state == "playing") else None
        if spec is not None:
            _pk, _rms, raw_bands = spec
            has_live = True
        else:
            # 待机/暂停或尚未解码时保持低矮柔缓的呼吸基线
            raw_bands = [
                self._energy * (0.18 + 0.12 * math.sin(t * 1.4 - b * 0.35))
                for b in range(NUM_BANDS)
            ]
            has_live = False

        # 更新 24 频段平滑柱高 + Peak Hold 峰值帽（瞬间吸顶 -> 短暂停留 -> 匀速缓落）
        for b in range(NUM_BANDS):
            val = max(0.02, min(1.0, raw_bands[b]))
            cur = self._bar_smooth[b]
            rate = 0.78 if val > cur else 0.28
            cur = cur + (val - cur) * rate
            self._bar_smooth[b] = cur

            if cur >= self._peak_hold[b]:
                self._peak_hold[b] = cur
                self._hold_ticks[b] = 8  # 约 0.4s 悬停
            elif self._hold_ticks[b] > 0:
                self._hold_ticks[b] -= 1
            else:
                self._peak_hold[b] = max(cur, self._peak_hold[b] - 0.026)

        # 根据当前宽度排布频谱柱（优先每柱 1 列 + 1 格间距，居中对齐）
        num_bars = min(NUM_BANDS, max(12, width // 2))
        step = 2 if num_bars * 2 - 1 <= width else 1
        total_w = num_bars * step - (1 if step == 2 else 0)
        left_pad = max(0, (width - total_w) // 2)

        # 重采样 24 频段到 num_bars
        bars_val: list[float] = []
        bars_pk: list[float] = []
        for i in range(num_bars):
            f_idx = i * (NUM_BANDS - 1) / max(1, num_bars - 1)
            i0 = int(f_idx)
            i1 = min(NUM_BANDS - 1, i0 + 1)
            frac = f_idx - i0
            bars_val.append(self._bar_smooth[i0] * (1.0 - frac) + self._bar_smooth[i1] * frac)
            bars_pk.append(self._peak_hold[i0] * (1.0 - frac) + self._peak_hold[i1] * frac)

        max_px = self._VIZ_ROWS * self._SUB_PER_ROW  # 40 levels (0..40)
        phase = t * 0.04
        out = Text(no_wrap=True)

        for row in range(self._VIZ_ROWS):
            # row 0 是最顶行 (px 32..40), row 4 是最底行 (px 0..8)
            row_bot = (self._VIZ_ROWS - 1 - row) * self._SUB_PER_ROW
            row_top = row_bot + self._SUB_PER_ROW
            if left_pad > 0:
                out.append(" " * left_pad)

            for i in range(num_bars):
                h_px = int(round(bars_val[i] * max_px))
                pk_px = int(round(bars_pk[i] * max_px))
                pk_px = max(1, min(max_px, pk_px))

                c_phase = phase + (i / max(1, num_bars - 1)) * 0.65
                # 顶部亮度高、底部柔和
                row_bright = 0.65 + 0.45 * ((self._VIZ_ROWS - 1 - row) / max(1, self._VIZ_ROWS - 1))

                # 先判断 Peak 峰值帽是否落在本行（且位于柱体上方或顶端）
                pk_in_row = row_bot < pk_px <= row_top
                if h_px >= row_top:
                    ch = "█"
                    style = pastel_hex(c_phase, brightness=row_bright)
                elif h_px > row_bot:
                    frac_sub = h_px - row_bot
                    ch = self._BAR_BLOCKS[frac_sub]
                    style = pastel_hex(c_phase, brightness=row_bright)
                elif pk_in_row and has_live:
                    # 独立悬空 Peak 峰值帽：根据亚像素位置选 ▁ / ▂ / ▔
                    sub_pos = pk_px - row_bot
                    ch = "▔" if sub_pos >= 6 else ("─" if sub_pos >= 3 else "▁")
                    style = f"bold {pastel_hex(c_phase + 0.08, brightness=1.18)}"
                elif row == self._VIZ_ROWS - 1:
                    ch = "▁"
                    style = pastel_hex(c_phase, brightness=0.32)
                else:
                    ch = " "
                    style = ""

                if style:
                    out.append(ch, style=style)
                else:
                    out.append(" ")

                if step == 2 and i < num_bars - 1:
                    out.append(" ")

            if row < self._VIZ_ROWS - 1:
                out.append("\n")

        self.update(out, layout=False)

    def _render_waveform_mode(
        self,
        t: float,
        state: str,
        pos: float,
        timeline: PeakTimeline | None,
        width: int,
    ) -> None:
        win = timeline.sample_waveform(pos, 2.6, width) if (timeline and state != "idle") else None
        if len(self._wave_smooth) != width:
            self._wave_smooth = [0.06] * width

        center = width // 2
        vals: list[float] = []
        for i in range(width):
            if win is not None:
                pk, rms = win[i]
                raw = 0.65 * pk + 0.35 * rms
                prev = self._wave_smooth[i]
                sm = prev + (raw - prev) * 0.55
            else:
                nx = i / max(1, width - 1)
                raw = self._energy * (0.2 + 0.16 * math.sin(nx * 10.0 - t * 2.0)) * math.sin(math.pi * nx)
                sm = raw
            self._wave_smooth[i] = sm
            vals.append(max(0.03, min(1.0, sm)))

        # 5 行对称波形：row 2 为中心轴，row 0..1 向上，row 3..4 向下
        # 每侧 2.5 行 ≈ 20 级纵向精度
        half_px = 20
        phase = t * 0.04
        out = Text(no_wrap=True)

        for row in range(self._VIZ_ROWS):
            for i in range(width):
                amp_px = max(1, int(round(vals[i] * half_px)))
                is_past = i <= center
                is_head = i == center

                c_phase = phase + (i / max(1, width - 1)) * 0.55
                if is_head and state == "playing":
                    ch = "┃"
                    style = "bold #f7e3d4"
                else:
                    br = 0.98 if is_past else 0.42
                    style = pastel_hex(c_phase, brightness=br)
                    if row == 2:
                        # 中心对称轴始终充实
                        ch = "█" if amp_px >= 4 else ("▆" if amp_px >= 2 else "━")
                    elif row < 2:
                        # 上半部：row 1 (4..12), row 0 (12..20)
                        dist_bot = (1 - row) * 8 + 4
                        dist_top = dist_bot + 8
                        if amp_px >= dist_top:
                            ch = "█"
                        elif amp_px > dist_bot:
                            ch = self._BAR_BLOCKS[amp_px - dist_bot]
                        else:
                            ch = " "
                    else:
                        # 下半部对称镜像：row 3 (4..12), row 4 (12..20)
                        dist_bot = (row - 3) * 8 + 4
                        dist_top = dist_bot + 8
                        if amp_px >= dist_top:
                            ch = "█"
                        elif amp_px > dist_bot:
                            # 下半部用上对齐方块 ▀/upper 或柔和点阵，使上下镜像干净
                            sub = amp_px - dist_bot
                            ch = "█" if sub >= 6 else ("▀" if sub >= 3 else "▔")
                        else:
                            ch = " "

                if ch == " ":
                    out.append(" ")
                else:
                    out.append(ch, style=style)

            if row < self._VIZ_ROWS - 1:
                out.append("\n")

        self.update(out, layout=False)


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
            t.append(f" [{dur}]", style=FG_SECONDARY)
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
            t.append("✦ ", style=FG_PEACH)
            t.append(self.orig, style="bold #f6dce8")
            t.append(" ✦", style=FG_PEACH)
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
        self.update(t, layout=False)


class SeekBar(Static):
    """支持柔和粉彩渐变的点击跳转进度条（带脏检查避免无意义重绘）。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)
        self.duration: float = 0.0
        self.position: float = 0.0
        self._last_state: tuple[int, int, int, bool] | None = None

    def update_progress(
        self, position: float, duration: float, anim_t: float = 0.0, playing: bool = False
    ) -> None:
        self.position = max(0.0, position)
        self.duration = max(0.0, duration)
        width = max(18, (self.size.width or 44) - 2)
        ratio = min(1.0, self.position / self.duration) if self.duration > 0 else 0.0
        filled = int(round(width * ratio))
        filled = max(0, min(width - 1, filled)) if width > 1 else 0

        # 将动画相位量化到 ~5Hz，若格数与相位桶均未变化则跳过 Rich Text 重建与 DOM 刷新
        phase_tick = int(anim_t * 3.0) if playing else 0
        state_key = (width, filled, phase_tick, playing)
        if state_key == self._last_state:
            return
        self._last_state = state_key

        phase = anim_t * 0.04
        out = Text(no_wrap=True)

        for i in range(filled):
            c_phase = phase + (i / max(1, width)) * 0.6
            out.append("━", style=pastel_hex(c_phase, brightness=0.95))

        if width > 2:
            if playing:
                pulse_char = "◉" if (phase_tick % 2 == 0) else "◈"
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

        self.update(out, layout=False)

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

    .hidden-pane {
        display: none;
    }

    #top-bar {
        height: 2;
        background: #171a29;
        border-bottom: solid #2a3048;
        padding: 0 1;
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
        padding: 0;
    }

    #left-pane {
        width: 24%;
        min-width: 20;
        border: round #2c334d;
        background: #161926;
    }

    #mid-pane {
        width: 1fr;
        min-width: 26;
        border: round #2c334d;
        background: #151825;
    }

    #right-pane {
        width: 1fr;
        min-width: 28;
        border: round #2c334d;
        padding: 0;
        background: #141724;
    }

    #left-pane:focus-within,
    #mid-pane:focus-within,
    #right-pane:focus-within {
        border: round #7e74ab;
    }

    .pane-header {
        height: 1;
        background: #1b1f31;
        color: #b4a7e5;
        text-style: bold;
        padding: 0 1;
        content-align: left middle;
    }

    #filter-input {
        height: 1;
        border: none;
        background: #1b2033;
        color: #dde2f2;
        padding: 0 1;
    }

    #filter-input:focus {
        background: #22273e;
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
        min-height: 7;
        align: center middle;
        background: #171a29;
        border: round #2c334d;
        padding: 0;
        margin: 0 1;
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
        margin: 0;
    }

    #now-title {
        text-style: bold;
        color: #f4dce7;
        content-align: center middle;
        text-align: center;
        padding: 0 1;
        margin: 0;
    }

    #now-meta {
        color: #97a1c4;
        text-align: center;
        padding: 0 1;
        margin: 0;
    }

    #now-quality {
        color: #8fd8c2;
        text-align: center;
        text-style: bold;
        padding: 0 1;
        margin: 0;
    }

    #wave-viz {
        height: 5;
        content-align: center middle;
        text-align: center;
        margin: 0 1;
    }

    #lyric-scroll {
        height: 1fr;
        border-top: dashed #2b314a;
        padding: 0;
    }

    .lyric-line {
        padding: 0 1;
        margin-bottom: 0;
        color: #677196;
        text-align: center;
    }

    .lyric-line:hover {
        color: #c5cde8;
    }

    .lyric-active {
        background: #21253b;
        text-style: bold;
        padding: 0 1;
    }

    #player-bar {
        height: 4;
        background: #171a29;
        border-top: solid #2a3048;
        padding: 0 1;
    }

    #progress-row {
        height: 1;
        align: left middle;
    }

    #time-cur, #time-dur {
        width: 7;
        color: #aeb7d6;
        content-align: center middle;
    }

    #seek-bar {
        width: 1fr;
        content-align: center middle;
    }

    #controls-row {
        height: 2;
        align: left middle;
    }

    #controls-row Button {
        min-width: 5;
        height: 1;
        margin-right: 1;
        padding: 0 1;
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
        Binding("v", "toggle_viz_mode", "频谱/波形"),
        Binding("1", "toggle_playlists", "歌单栏"),
        Binding("2", "toggle_cover", "封面"),
        Binding("3", "toggle_visualizer", "可视化"),
        Binding("0", "reset_layout", "还原面板"),
        Binding("l", "toggle_desktop_lyrics", "桌面歌词"),
        Binding("L", "toggle_desktop_lyrics_lock", "歌词穿透", show=False),
        Binding("V", "toggle_screen_shader", "流质视效"),
        Binding("K", "toggle_shader_big_lyrics", "流质大歌词"),
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

        self.show_playlists: bool = True
        self.show_cover: bool = True
        self.show_visualizer: bool = True

        self._load_seq: int = 0
        self._prefetched_seq: int = -1
        self._consecutive_failures: int = 0
        self._cover_cache: dict[str, str] = {}
        self._cover_status: str = "idle"
        self._peak_cache: dict[int, PeakTimeline] = {}
        self._current_peaks: PeakTimeline | None = None
        self._peaks_sent_tid: int = 0
        self._peaks_sent_idx: int = 0
        self._peak_worker = PeakPrecomputer()
        self._player: MpvPlayer | None = None
        self._lyric_ipc: LyricIpcServer | None = None
        self._lyric_proc: subprocess.Popen | None = None
        self._shader_proc: subprocess.Popen | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tick_timer = None
        self._tick_count: int = 0
        self._anim_origin: float = time.monotonic()
        self._last_time_cur: str = ""
        self._last_time_dur: str = ""
        self._last_btn_label: str = ""
        self._last_playing_item: TrackItem | None = None

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
                yield Input(placeholder="/ 搜索歌名、歌手、专辑...", id="filter-input")
                yield ListView(id="track-list")

            with Vertical(id="right-pane"):
                with Vertical(id="cover-box"):
                    yield VinylCoverStage(id="cover-art")
                yield Static("", id="cover-aura", markup=False)
                yield Static("未播放歌曲", id="now-title")
                yield Static("左侧选歌单，Enter 或 x 播放", id="now-meta")
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
                yield Button("-5s", id="btn-back")
                yield Button("+5s", id="btn-fwd")
                yield Static("音量 100% · 待机", id="status-label")
        yield Footer()

    async def on_mount(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._lyric_ipc = LyricIpcServer(
            on_command=self._on_lyric_ipc_cmd,
            on_peaks_subscriber=self._on_peaks_subscriber,
        )
        try:
            self._lyric_ipc.start()
        except OSError:
            self._lyric_ipc = None
        self._player = MpvPlayer(
            on_track_end=self._on_mpv_track_end,
            on_state_change=self._on_mpv_state_change,
        )
        self._tick_timer = self.set_interval(0.05, self._tick_player_ui)
        asyncio.create_task(self._bootstrap())

    async def on_unmount(self) -> None:
        if self._tick_timer:
            self._tick_timer.stop()
            self._tick_timer = None
        self._peak_worker.cancel()
        self._cleanup_desktop_lyrics()
        if self._player:
            self._player.close()
            self._player = None

    def _cleanup_desktop_lyrics(self) -> None:
        if self._lyric_ipc and (
            (self._lyric_proc is not None and self._lyric_proc.poll() is None)
            or (self._shader_proc is not None and self._shader_proc.poll() is None)
        ):
            self._lyric_ipc.send_control("quit")
        for proc_attr in ("_lyric_proc", "_shader_proc"):
            proc = getattr(self, proc_attr, None)
            if proc is not None and proc.poll() is None:
                try:
                    proc.wait(timeout=0.8)
                except subprocess.TimeoutExpired:
                    proc.terminate()
            setattr(self, proc_attr, None)
        if self._lyric_ipc:
            self._lyric_ipc.close()
            self._lyric_ipc = None

    def _on_peaks_subscriber(self) -> None:
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._resync_peaks_for_subscriber)

    def _resync_peaks_for_subscriber(self) -> None:
        ipc = self._lyric_ipc
        tid = self._peaks_sent_tid
        if ipc is None or not tid or self._current_peaks is None:
            return
        self._peaks_sent_idx = 0
        ipc.publish_peaks(track_id=tid, fps=PEAK_FPS, start_idx=0, frames=[], reset=True)
        self._flush_peaks_ipc()

    def _flush_peaks_ipc(self) -> None:
        ipc = self._lyric_ipc
        tl = self._current_peaks
        tid = self._peaks_sent_tid
        if ipc is None or tl is None or not tid or not ipc.has_peak_clients():
            return
        copied = tl.copy_range(self._peaks_sent_idx)
        if copied is None:
            return
        start_idx, raw_slice = copied
        s = PeakTimeline.STRIDE
        n_frames = len(raw_slice) // s
        batch: list[list[float]] = []
        for idx in range(n_frames):
            off = idx * s
            rms = raw_slice[off + 1]
            b_base = off + 2
            kick = (raw_slice[b_base] + raw_slice[b_base + 1] + raw_slice[b_base + 2]) / 3.0
            punch = (raw_slice[b_base + 3] + raw_slice[b_base + 4] + raw_slice[b_base + 5]) / 3.0
            mid = sum(raw_slice[b_base + 6 : b_base + 14]) / 8.0
            high = sum(raw_slice[b_base + 14 : b_base + 24]) / 10.0
            b8 = [
                round(
                    (
                        raw_slice[b_base + i * 3]
                        + raw_slice[b_base + i * 3 + 1]
                        + raw_slice[b_base + i * 3 + 2]
                    )
                    / 3.0,
                    3,
                )
                for i in range(8)
            ]
            sub_rms = raw_slice[off + 26]
            body_rms = raw_slice[off + 27]
            body_peak = raw_slice[off + 28]
            click_lin = raw_slice[off + 29]
            batch.append(
                [
                    round(kick, 3),
                    round(punch, 3),
                    round(mid, 3),
                    round(rms, 3),
                    round(high, 3),
                    *b8,
                    round(sub_rms, 4),
                    round(body_rms, 4),
                    round(body_peak, 4),
                    round(click_lin, 4),
                ]
            )
        self._peaks_sent_idx = start_idx + len(batch)
        ipc.publish_peaks(track_id=tid, fps=PEAK_FPS, start_idx=start_idx, frames=batch)

    def _on_mpv_state_change(self, st: PlayerState) -> None:
        ipc = self._lyric_ipc
        if ipc is None:
            return
        dur = st.duration
        tr = self.current_track
        if dur <= 0 and tr:
            dur = (tr.get("duration_ms") or 0) / 1000.0
        ipc.publish_state(
            playing=st.playing,
            paused=st.paused,
            buffering=st.buffering,
            time_pos=st.time_pos,
            time_pos_at=st.time_pos_at,
            duration=dur,
            speed=st.speed,
            seq=st.seq,
        )

    def _on_lyric_ipc_cmd(self, msg: dict[str, Any]) -> None:
        if msg.get("type") != "cmd":
            return
        action = msg.get("action")
        val = msg.get("value")
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._handle_lyric_cmd_on_loop, action, val)

    def _handle_lyric_cmd_on_loop(self, action: str | None, val: Any) -> None:
        if action == "toggle_pause":
            self.action_toggle_pause()
        elif action == "next":
            asyncio.create_task(self.action_next_track())
        elif action == "prev":
            asyncio.create_task(self.action_prev_track())
        elif action == "seek_relative" and isinstance(val, (int, float)) and self._player:
            self._player.seek_relative(float(val))
        elif action == "vol_delta" and isinstance(val, (int, float)) and self._player:
            self._player.add_volume(float(val))

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
        self._last_playing_item = None
        playing_id = self.current_track["id"] if self.current_track else None
        items: list[TrackItem] = []
        for idx, tr in enumerate(tracks):
            is_pl = tr["id"] == playing_id
            it = TrackItem(tr, idx, playing=is_pl)
            if is_pl:
                self._last_playing_item = it
            items.append(it)
        if items:
            await lv.extend(items)

    @on(ListView.Selected, "#playlist-list")
    async def on_playlist_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, PlaylistItem):
            await self._load_playlist(event.item.playlist)

    @on(ListView.Selected, "#track-list")
    async def on_track_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, TrackItem):
            return
        selected = event.item.track
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

    def _ensure_shuffle_bag(self) -> None:
        n = len(self.play_queue)
        if n > 1 and not self.shuffle_bag:
            self.shuffle_bag = [i for i in range(n) if i != self.current_index]
            random.shuffle(self.shuffle_bag)

    def _peek_next_index(self) -> int | None:
        n = len(self.play_queue)
        if n == 0:
            return None
        if n == 1:
            return 0
        if self.shuffle_mode:
            self._ensure_shuffle_bag()
            return self.shuffle_bag[-1] if self.shuffle_bag else 0
        return 0 if self.current_index is None else (self.current_index + 1) % n

    def _pick_next_shuffle_index(self) -> int | None:
        n = len(self.play_queue)
        if n == 0:
            return None
        if n == 1:
            return 0
        self._ensure_shuffle_bag()
        return self.shuffle_bag.pop()

    def _get_current_play_pos(self) -> float:
        p = self._player
        if p is None:
            return 0.0
        st = p.get_state()
        pos = st.time_pos
        if st.playing and not st.paused and not st.buffering and st.time_pos_at > 0:
            pos += max(0.0, min(0.35, time.monotonic() - st.time_pos_at))
        return pos

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

        self._peak_worker.cancel()
        self._current_peaks = None

        self._cover_status = "loading"
        cover_stage = self.query_one("#cover-art", VinylCoverStage)
        cover_stage.clear_cover(time.monotonic() - self._anim_origin, status="loading")

        self.query_one("#now-title", Static).update(track["name"])
        self.query_one("#now-meta", Static).update(f"{track['artists']} · {track['album']}")
        self.query_one("#now-quality", Static).update(f"◌ 正在解析 {self.level.upper()} 无损直链...")
        self._refresh_playing_marker()

        if self._lyric_ipc:
            self._lyric_ipc.publish_track(track, has_yrc=False, lines=[])

        # 点歌瞬间并行拉取封面与歌词，无需等待音频直链解析完成
        asyncio.create_task(self._load_cover_and_lyrics(track, seq))

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

        actual_lv = str(info.get("level") or self.level)
        playable_url = await asyncio.to_thread(
            ncm.resolve_cached_stream, int(track["id"]), raw_url, actual_lv
        )
        if seq != self._load_seq:
            return

        ok = self._player.play(playable_url)
        if not ok:
            self.notify(f"播放器加载失败: {track['name']}", severity="warning")
            await self._skip_on_failure(track)
            return

        self._consecutive_failures = 0
        self._cover_status = "playing"
        lv = actual_lv.upper()
        fmt = (info.get("type") or "flac").upper()
        br = int((info.get("br") or 0) / 1000)
        size_mb = round((info.get("size") or 0) / 1048576, 1)
        self.query_one("#now-quality", Static).update(f"✦ {lv} · {fmt} · {br}kbps · {size_mb}MB")

        tid = int(track["id"])
        cached_peaks = self._peak_cache.get(tid)
        if cached_peaks is not None:
            self._current_peaks = cached_peaks
        else:
            timeline = PeakTimeline()
            if len(self._peak_cache) >= 3:
                self._peak_cache.pop(next(iter(self._peak_cache)))
            self._peak_cache[tid] = timeline
            self._current_peaks = timeline
            self._peak_worker.start(
                playable_url, timeline, get_play_pos=self._get_current_play_pos
            )
        self._peaks_sent_tid = tid
        self._peaks_sent_idx = 0
        if self._lyric_ipc:
            self._lyric_ipc.publish_peaks(
                track_id=tid, fps=PEAK_FPS, start_idx=0, frames=[], reset=True
            )
            self._flush_peaks_ipc()

    async def _skip_on_failure(self, track: dict[str, Any]) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= min(8, max(1, len(self.play_queue))):
            self.query_one("#now-quality", Static).update("连续多首不可播，已停止自动跳过")
            self._consecutive_failures = 0
            self._cover_status = "idle"
            return
        await asyncio.sleep(0.3)
        await self._play_next(auto=True)

    def _compute_cover_dims(self) -> tuple[int, int]:
        cover_box = self.query_one("#cover-box")
        right_pane = self.query_one("#right-pane")
        pane_h = right_pane.size.height or 30
        max_w = max(16, min(32, (cover_box.size.width or 34) - 4))
        max_h = max(6, min(10, pane_h // 4))
        h = max(6, min(max_h, max_w // 2))
        w = min(max_w, h * 2)
        return w, h

    async def _load_cover_and_lyrics(self, track: dict[str, Any], seq: int) -> None:
        pic_url = track.get("pic_url") or ""
        cover_stage = self.query_one("#cover-art", VinylCoverStage)

        async def _do_cover() -> None:
            if not pic_url:
                if seq == self._load_seq:
                    self._cover_status = "error"
                    cover_stage.clear_cover(time.monotonic() - self._anim_origin, status="error")
                return
            if pic_url in self._cover_cache:
                ansi = self._cover_cache[pic_url]
            else:
                w, h = self._compute_cover_dims()
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

        async def _do_lyrics() -> None:
            try:
                rich_lyric = await asyncio.to_thread(ncm.song_lyric_rich, self.cookie, track["id"])
            except Exception:  # noqa: BLE001
                rich_lyric = {"has_yrc": False, "lines": []}
            if seq != self._load_seq:
                return
            rich_lines = rich_lyric.get("lines") or []
            if self._lyric_ipc:
                self._lyric_ipc.publish_track(
                    track,
                    has_yrc=bool(rich_lyric.get("has_yrc")),
                    lines=rich_lines,
                )
            lyrics = [(float(r["start"]), str(r["orig"]), str(r.get("trans") or "")) for r in rich_lines]
            await self._render_lyrics(lyrics)

        await asyncio.gather(_do_cover(), _do_lyrics())

    async def _prefetch_next_track(self, next_track: dict[str, Any], seq: int) -> None:
        """后台静默预取下一首曲目的直链、歌词与封面，使切歌达到 0ms 瞬切。"""
        if seq != self._load_seq:
            return
        tid = int(next_track["id"])
        pic_url = next_track.get("pic_url") or ""
        w, h = self._compute_cover_dims()

        def _bg_prefetch() -> None:
            try:
                ncm.stream_url(self.cookie, tid, self.level)
            except Exception:  # noqa: BLE001
                pass
            try:
                ncm.song_lyric_rich(self.cookie, tid)
            except Exception:  # noqa: BLE001
                pass
            if pic_url:
                try:
                    ncm.fetch_cover_ansi(pic_url, w, h)
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.to_thread(_bg_prefetch)

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
        playing_id = self.current_track["id"] if self.current_track else None
        if self._last_playing_item is not None:
            try:
                if self._last_playing_item.track["id"] != playing_id:
                    self._last_playing_item.set_playing(False)
                    self._last_playing_item = None
                else:
                    return
            except Exception:  # noqa: BLE001
                self._last_playing_item = None

        if playing_id is None:
            return
        lv = self.query_one("#track-list", ListView)
        for item in lv.children:
            if isinstance(item, TrackItem) and item.track["id"] == playing_id:
                item.set_playing(True)
                self._last_playing_item = item
                break

    def _render_cover_aura(self, t: float, playing: bool, paused: bool) -> Text:
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

        now = time.monotonic()
        t = now - self._anim_origin
        self._tick_count += 1
        st = self._player.get_state()
        is_actively_playing = st.playing and not st.paused and not st.buffering

        dur = st.duration
        if dur <= 0 and self.current_track:
            dur = (self.current_track.get("duration_ms") or 0) / 1000.0

        pos = st.time_pos
        if is_actively_playing and st.time_pos_at > 0:
            delta = max(0.0, min(0.35, now - st.time_pos_at))
            pos = pos + delta
            if dur > 0:
                pos = min(dur, pos)

        # 播放过 18 秒或剩余不足 25 秒时，后台静默预取下一首曲目
        if (
            is_actively_playing
            and self._prefetched_seq != self._load_seq
            and (pos >= 18.0 or (dur > 5.0 and (dur - pos) <= 25.0))
        ):
            self._prefetched_seq = self._load_seq
            nxt_idx = self._peek_next_index()
            if nxt_idx is not None and 0 <= nxt_idx < len(self.play_queue):
                asyncio.create_task(
                    self._prefetch_next_track(self.play_queue[nxt_idx], self._load_seq)
                )

        # 待机/暂停自动降频：活跃播放时 20 FPS 刷频谱，暂停/待机时降至 2.5 FPS 省电降温
        should_draw_viz = is_actively_playing or st.buffering or (self._tick_count % 8 == 0)
        if self.show_visualizer and should_draw_viz:
            wave_state = (
                "buffering"
                if st.buffering
                else ("paused" if st.paused else ("playing" if st.playing else "idle"))
            )
            wave_viz.render_wave(t, state=wave_state, pos=pos, timeline=self._current_peaks)

        if self._lyric_ipc and self._current_peaks is not None:
            self._flush_peaks_ipc()

        seek_bar.update_progress(pos, dur, anim_t=t, playing=is_actively_playing)

        cur_str = fmt_time(pos)
        if cur_str != self._last_time_cur:
            self._last_time_cur = cur_str
            time_cur.update(cur_str, layout=False)

        dur_str = fmt_time(dur)
        if dur_str != self._last_time_dur:
            self._last_time_dur = dur_str
            time_dur.update(dur_str, layout=False)

        # 慢动画与状态装饰（活跃播放 ~5 FPS，待机/暂停 ~1.6 FPS）
        ambient_div = 4 if (is_actively_playing or st.buffering) else 12
        if self._tick_count % ambient_div == 0:
            brand.render_frame(t, playing=is_actively_playing)
            if self.show_cover:
                if not cover_stage.has_cover:
                    ascii_state = (
                        "loading"
                        if self._cover_status == "loading"
                        else (
                            "playing"
                            if is_actively_playing
                            else ("error" if self._cover_status == "error" else "idle")
                        )
                    )
                    cover_stage.render_ascii_frame(t, status=ascii_state)
                cover_aura.update(
                    self._render_cover_aura(t, playing=st.playing, paused=st.paused),
                    layout=False,
                )

            target_btn_lbl = "▶" if (st.paused or not st.playing) else "⏸"
            if target_btn_lbl != self._last_btn_label:
                self._last_btn_label = target_btn_lbl
                play_btn.label = target_btn_lbl

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
                mode_tag = "FFT频谱" if wave_viz.viz_mode == "spectrum" else "波形"
                if self._current_peaks and self._current_peaks.done:
                    status_text.append(f"播放中·{mode_tag}就绪", style=FG_MINT)
                else:
                    status_text.append(f"播放中·{mode_tag}", style=FG_MINT)
            else:
                status_text.append("✧ 待机", style=FG_MUTED)
            status_lbl.update(status_text, layout=False)

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

    # ── 面板开关与可视化模式切换 ──────────────────────────────────────────────
    def _apply_panel_visibility(self) -> None:
        self.query_one("#left-pane").set_class(not self.show_playlists, "hidden-pane")
        self.query_one("#cover-box").set_class(not self.show_cover, "hidden-pane")
        self.query_one("#cover-aura").set_class(not self.show_cover, "hidden-pane")
        self.query_one("#wave-viz").set_class(not self.show_visualizer, "hidden-pane")

    def action_toggle_viz_mode(self) -> None:
        viz = self.query_one("#wave-viz", AmbientWaveVisualizer)
        if not self.show_visualizer:
            self.show_visualizer = True
            self._apply_panel_visibility()
        mode = viz.toggle_mode()
        self.notify(
            "可视化模式: 真 FFT 频谱图 (+Peak 峰值帽)"
            if mode == "spectrum"
            else "可视化模式: 真实对称采样波形图"
        )

    def action_toggle_playlists(self) -> None:
        self.show_playlists = not self.show_playlists
        self._apply_panel_visibility()
        if not self.show_playlists:
            self.query_one("#track-list", ListView).focus()

    def action_toggle_cover(self) -> None:
        self.show_cover = not self.show_cover
        self._apply_panel_visibility()

    def action_toggle_visualizer(self) -> None:
        self.show_visualizer = not self.show_visualizer
        self._apply_panel_visibility()

    def action_reset_layout(self) -> None:
        self.show_playlists = True
        self.show_cover = True
        self.show_visualizer = True
        self._apply_panel_visibility()

    def action_toggle_desktop_lyrics(self) -> None:
        if self._lyric_ipc and self._lyric_ipc.has_clients():
            self._lyric_ipc.send_control("toggle_visible")
            return
        if self._lyric_proc is not None and self._lyric_proc.poll() is None:
            if self._lyric_ipc:
                self._lyric_ipc.send_control("toggle_visible")
            return
        try:
            proc = launch_desktop_lyrics(foreground=False)
            if isinstance(proc, subprocess.Popen):
                self._lyric_proc = proc
            self.notify("已开启 512×64 桌面歌词（左键拖动 / 滚轮调偏移 / Shift+L 穿透）")
        except Exception as e:  # noqa: BLE001
            self.notify(f"启动桌面歌词失败: {e}", severity="error")

    def action_toggle_desktop_lyrics_lock(self) -> None:
        if self._lyric_ipc and self._lyric_ipc.has_clients():
            self._lyric_ipc.send_control("toggle_lock")
            self.notify("已切换桌面歌词鼠标穿透状态")
        else:
            self.notify("桌面歌词未连接，按 l 启动", severity="warning")

    def action_toggle_screen_shader(self) -> None:
        if self._shader_proc is not None and self._shader_proc.poll() is None:
            if self._lyric_ipc:
                self._lyric_ipc.send_control("toggle_shader")
            return
        try:
            proc = launch_screen_shader(active=True, foreground=False)
            if isinstance(proc, subprocess.Popen):
                self._shader_proc = proc
            self.notify("已启动全屏流质 Shader 视效（Shift+V / Ctrl+Alt+V 切换，Shift+K / Ctrl+Alt+L 开关大歌词）")
        except Exception as e:  # noqa: BLE001
            self.notify(f"启动流质视效失败: {e}", severity="error")

    def action_toggle_shader_big_lyrics(self) -> None:
        if self._shader_proc is not None and self._shader_proc.poll() is None:
            if self._lyric_ipc:
                self._lyric_ipc.send_control("toggle_big_lyrics")
                self.notify("已切换流质大歌词显示（Shift+K / Ctrl+Alt+L）")
            return
        try:
            proc = launch_screen_shader(active=True, big_lyrics=True, foreground=False)
            if isinstance(proc, subprocess.Popen):
                self._shader_proc = proc
            self.notify("已启动全屏流质视效并开启大歌词（Shift+K / Ctrl+Alt+L 随时开关）")
        except Exception as e:  # noqa: BLE001
            self.notify(f"启动流质大歌词失败: {e}", severity="error")

    # ── 播放控制快捷键 ───────────────────────────────────────────────────────
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
        self._peak_worker.cancel()
        self._cleanup_desktop_lyrics()
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
