"""基于 OpenGL 动态字形图集与独立流体运动学的全屏巨型动态排印歌词引擎。"""
from __future__ import annotations

import array
import bisect
import ctypes
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any


# ── 语言字符分类工具 ──────────────────────────────────────────────────────────
_SOFT_JP_PARTICLES = frozenset(
    "のにをはがてとでもへやなかっゃゅょぁぃぅぇぉわねよさぞぜ"
)
_SOFT_ZH_PARTICLES = frozenset(
    "的了着过地得啊呀呢吧吗哦哇啦嘛嘛么之乎者也"
)
_SILENT_PUNCT = frozenset(
    " ,.，。、;；:：\"'“”‘’()（）[]【】{}《》<>-—–_·•/\\|　\t\r\n"
)
_EXPRESSIVE_PUNCT = frozenset("!?！？♪♫♡♥★☆")


def _is_cjk_char(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3040 <= cp <= 0x30FF  # 平假名 / 片假名
        or 0x31F0 <= cp <= 0x31FF
        or 0x3400 <= cp <= 0x4DBF  # CJK 扩展 A
        or 0x4E00 <= cp <= 0x9FFF  # CJK 基本汉字
        or 0xF900 <= cp <= 0xFAFF
        or 0xAC00 <= cp <= 0xD7AF  # 韩文音节
    )


def _is_hiragana(ch: str) -> bool:
    cp = ord(ch)
    return 0x3040 <= cp <= 0x309F


def _is_katakana(ch: str) -> bool:
    cp = ord(ch)
    return 0x30A0 <= cp <= 0x30FF


def _has_kana(text: str) -> bool:
    return any(_is_hiragana(c) or _is_katakana(c) for c in text)


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = _clamp((x - edge0) / max(1e-6, edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _ease_out_back(t: float, s: float = 1.15) -> float:
    t = _clamp(t, 0.0, 1.0)
    u = t - 1.0
    return 1.0 + u * u * ((s + 1.0) * u + s)


def _ease_out_quint(t: float) -> float:
    t = _clamp(t, 0.0, 1.0)
    u = 1.0 - t
    return 1.0 - u * u * u * u * u


def _ease_in_cubic(t: float) -> float:
    t = _clamp(t, 0.0, 1.0)
    return t * t * t


def _hash_float(a: int, b: int, c: int = 0) -> float:
    """确定性 [0, 1) 伪随机哈希，避免每帧抖动且跨行形态丰富。"""
    x = math.sin(a * 127.1 + b * 311.7 + c * 74.7) * 43758.5453123
    return x - math.floor(x)


# ── 动态字形图集 (Cairo 光栅化 -> OpenGL Mipmap 纹理) ────────────────────────
@dataclass
class AtlasEntry:
    """图集内单个字形单元格的 UV 与几何比例信息。"""

    u0: float
    v0: float
    u1: float
    v1: float
    # 单元格总宽/高（含安全光晕留白）
    cell_w: float
    cell_h: float
    # 有效墨迹宽/高（不含安全留白），用于精确排版与包围盒估算
    ink_w: float
    ink_h: float
    # 单元格相对有效墨迹的膨胀倍率（供渲染四边形外扩光晕区）
    pad_scale_x: float
    pad_scale_y: float


class GlyphAtlas:
    """按需打包高分辨率字形与译文条的 2048×2048 Cairo 离屏图集。"""

    ATLAS_W = 2048
    ATLAS_H = 2048

    def __init__(self, pango_ctx: Any) -> None:
        self.surface: Any = None
        self.cr: Any = None
        self.font_zh, self.font_jp, self.font_trans = self._pick_fonts(pango_ctx)

        self.entries: dict[tuple[str, str, int], AtlasEntry] = {}
        self._cursor_x: int = 2
        self._cursor_y: int = 2
        self._row_h: int = 0

        self.version: int = 0
        self.dirty_full: bool = False
        self.dirty_y0: int = 0
        self.dirty_y1: int = 0

    def _ensure_surface(self) -> None:
        if self.surface is None:
            import cairo

            self.surface = cairo.ImageSurface(cairo.FORMAT_A8, self.ATLAS_W, self.ATLAS_H)
            self.cr = cairo.Context(self.surface)
            self.dirty_full = True
            self.dirty_y0 = 0
            self.dirty_y1 = self.ATLAS_H

    def release_memory(self) -> None:
        """在关闭大歌词或隐藏 Shader 窗口时彻底释放 4MB Cairo 离屏表面与缓存。"""
        self.cr = None
        self.surface = None
        self.entries.clear()
        self._cursor_x = 2
        self._cursor_y = 2
        self._row_h = 0
        self.dirty_full = False
        self.dirty_y0 = 0
        self.dirty_y1 = 0
        self.version += 1

    @staticmethod
    def _pick_fonts(pango_ctx: Any) -> tuple[str, str, str]:
        families: set[str] = set()
        try:
            families = {f.get_name() for f in pango_ctx.list_families()}
        except Exception:  # noqa: BLE001
            pass

        def choose(cands: tuple[str, ...]) -> str:
            for c in cands:
                if c in families:
                    return c
            return "Sans"

        # 思源宋体 Bold 刀锋衬线：巨型全屏歌词电影感天花板
        font_zh = choose(
            (
                "Noto Serif CJK SC",
                "Source Han Serif SC",
                "LXGW WenKai",
                "Noto Sans CJK SC",
                "Microsoft YaHei",
                "WenQuanYi Micro Hei",
                "Sans",
            )
        )
        font_jp = choose(
            (
                "Noto Serif CJK JP",
                "Noto Serif CJK SC",
                "Noto Sans CJK JP",
                "Noto Sans CJK SC",
                "Sans",
            )
        )
        font_trans = choose(
            (
                "LXGW WenKai",
                "Noto Serif CJK SC",
                "Noto Sans CJK SC",
                "Microsoft YaHei",
                "Sans",
            )
        )
        return font_zh, font_jp, font_trans

    def clear(self) -> None:
        import cairo

        if self.cr is not None:
            self.cr.save()
            self.cr.set_operator(cairo.OPERATOR_CLEAR)
            self.cr.paint()
            self.cr.restore()
            self.dirty_full = True
            self.dirty_y0 = 0
            self.dirty_y1 = self.ATLAS_H
        self.entries.clear()
        self._cursor_x = 2
        self._cursor_y = 2
        self._row_h = 0
        self.version += 1

    def has_space_for(self, est_w: int, est_h: int) -> bool:
        if self._cursor_x + est_w + 2 <= self.ATLAS_W and self._cursor_y + est_h + 2 <= self.ATLAS_H:
            return True
        next_y = self._cursor_y + self._row_h + 2
        return next_y + est_h + 2 <= self.ATLAS_H

    def get_or_rasterize(
        self,
        text: str,
        *,
        style: str = "main_zh",
        px_size: int = 200,
    ) -> AtlasEntry | None:
        key = (text, style, px_size)
        existing = self.entries.get(key)
        if existing is not None:
            return existing

        self._ensure_surface()
        import cairo
        from gi.repository import Pango, PangoCairo

        is_trans = style == "trans"
        if is_trans:
            family = self.font_trans
            weight = Pango.Weight.NORMAL
            pad = 22
            max_w_limit = 1600
        elif style == "main_jp":
            family = self.font_jp
            weight = Pango.Weight.BOLD
            pad = 30
            max_w_limit = 920
        elif style == "soft_jp":
            family = self.font_jp
            weight = Pango.Weight.NORMAL
            pad = 28
            max_w_limit = 640
        elif style == "soft_zh":
            family = self.font_zh
            weight = Pango.Weight.NORMAL
            pad = 28
            max_w_limit = 640
        else:
            family = self.font_zh
            weight = Pango.Weight.BOLD
            pad = 30
            max_w_limit = 920

        layout = PangoCairo.create_layout(self.cr)
        actual_size = float(px_size)

        def build_layout(sz: float) -> tuple[Any, Any]:
            desc = Pango.FontDescription()
            desc.set_family(family)
            desc.set_absolute_size(sz * Pango.SCALE)
            desc.set_weight(weight)
            layout.set_font_description(desc)
            if is_trans:
                attrs = Pango.AttrList()
                attrs.insert(Pango.attr_letter_spacing_new(int(3.2 * Pango.SCALE)))
                layout.set_attributes(attrs)
            else:
                layout.set_attributes(None)
            layout.set_text(text, -1)
            return layout.get_pixel_extents()

        ink, logical = build_layout(actual_size)
        raw_w = max(ink.width, logical.width, 4)
        if raw_w > max_w_limit:
            actual_size = max(24.0, actual_size * (max_w_limit / float(raw_w)))
            ink, logical = build_layout(actual_size)

        # 取 ink 与 logical 联合裁剪区，确保宋体顿笔与斜体撇捺绝不被裁切
        min_x = min(ink.x, logical.x)
        min_y = min(ink.y, logical.y)
        max_x = max(ink.x + ink.width, logical.x + logical.width)
        max_y = max(ink.y + ink.height, logical.y + logical.height)

        box_w = max(8, int(math.ceil(max_x - min_x)))
        box_h = max(8, int(math.ceil(max_y - min_y)))
        cell_w = box_w + pad * 2
        cell_h = box_h + pad * 2

        if cell_w + 4 > self.ATLAS_W or cell_h + 4 > self.ATLAS_H:
            return None

        # Row-Shelf 装箱
        if self._cursor_x + cell_w + 2 > self.ATLAS_W:
            self._cursor_x = 2
            self._cursor_y += self._row_h + 2
            self._row_h = 0

        if self._cursor_y + cell_h + 2 > self.ATLAS_H:
            return None

        x0 = self._cursor_x
        y0 = self._cursor_y
        self._cursor_x += cell_w + 2
        self._row_h = max(self._row_h, cell_h)

        # 清空目标单元格并纯白高精度光栅化
        self.cr.save()
        self.cr.rectangle(x0, y0, cell_w, cell_h)
        self.cr.clip()
        self.cr.set_operator(cairo.OPERATOR_CLEAR)
        self.cr.paint()
        self.cr.set_operator(cairo.OPERATOR_OVER)
        self.cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)
        self.cr.move_to(x0 + pad - min_x, y0 + pad - min_y)
        PangoCairo.show_layout(self.cr, layout)
        self.cr.restore()

        ink_w = float(max(6, ink.width if ink.width > 0 else box_w))
        ink_h = float(max(6, ink.height if ink.height > 0 else box_h))
        entry = AtlasEntry(
            u0=x0 / float(self.ATLAS_W),
            v0=y0 / float(self.ATLAS_H),
            u1=(x0 + cell_w) / float(self.ATLAS_W),
            v1=(y0 + cell_h) / float(self.ATLAS_H),
            cell_w=float(cell_w),
            cell_h=float(cell_h),
            ink_w=ink_w,
            ink_h=ink_h,
            pad_scale_x=float(cell_w) / float(box_w),
            pad_scale_y=float(cell_h) / float(box_h),
        )
        self.entries[key] = entry
        if not self.dirty_full:
            if self.dirty_y1 <= self.dirty_y0:
                self.dirty_y0 = y0
                self.dirty_y1 = y0 + cell_h
            else:
                self.dirty_y0 = min(self.dirty_y0, y0)
                self.dirty_y1 = max(self.dirty_y1, y0 + cell_h)
        self.version += 1
        return entry

    def consume_dirty_upload(self) -> tuple[bool, int, int, int, Any] | None:
        """若图集有新写入，返回零拷贝 (is_full, y0, height, data_ptr, keepalive_view)。"""
        if self.surface is None or (not self.dirty_full and self.dirty_y1 <= self.dirty_y0):
            return None
        self.surface.flush()
        mv = memoryview(self.surface.get_data())
        stride = self.surface.get_stride()
        c_arr = (ctypes.c_ubyte * len(mv)).from_buffer(mv)
        base_addr = ctypes.addressof(c_arr)
        if self.dirty_full:
            self.dirty_full = False
            self.dirty_y0 = 0
            self.dirty_y1 = 0
            return True, 0, self.ATLAS_H, base_addr, mv
        y0 = max(0, self.dirty_y0)
        y1 = min(self.ATLAS_H, self.dirty_y1)
        self.dirty_y0 = 0
        self.dirty_y1 = 0
        if y1 <= y0:
            return None
        return False, y0, y1 - y0, base_addr + y0 * stride, mv


# ── 数据结构：词元、单字运动体、活跃句 ───────────────────────────────────────
@dataclass
class LyricToken:
    text: str
    t_start: float
    t_end: float
    style: str
    size_weight: float
    break_before: bool = False
    gap_before: bool = False
    is_soft: bool = False
    is_hero_cand: bool = False


@dataclass
class GlyphParticle:
    """屏幕上单个独立流动字形的完整几何与物理动效状态。"""

    text: str
    style: str
    px_size: int
    entry: AtlasEntry
    t_start: float
    t_end: float
    is_trans: bool = False

    # 排版目标位置（屏幕归一化坐标 x in [0,1] 左->右, y in [0,1] 下->上）
    base_x: float = 0.5
    base_y: float = 0.5
    # 含 padding 单元格在像素空间中的基准宽/高（按基准 1080 高度归一化：占屏高比例）
    base_h_norm: float = 0.22
    cell_aspect: float = 1.0
    base_rot: float = 0.0
    depth: float = 1.0  # 0.72..1.0：前景大字 vs 柔焦远景字
    seed: float = 0.0

    # 独立入场 / 退场向量与错落延迟
    enter_delay: float = 0.0
    enter_dur: float = 0.55
    in_dx: float = 0.0
    in_dy: float = -0.05
    in_rot: float = 0.0

    exit_delay: float = 0.0
    exit_dur: float = 0.78
    out_dx: float = 0.04
    out_dy: float = 0.06
    out_rot: float = 0.0

    # 持续漂浮参数（每字独立相位与频率）
    drift_fx: float = 0.65
    drift_fy: float = 0.52
    drift_amp_x: float = 0.008
    drift_amp_y: float = 0.011
    drift_rot_amp: float = 0.032

    # 实时平滑状态
    lit_smooth: float = 0.0
    ignite_env: float = 0.0
    ignite_vel: float = 0.0
    was_lit: bool = False
    kick_ox: float = 0.0
    kick_oy: float = 0.0


@dataclass
class ActiveLine:
    """正在屏幕上呈现或优雅退场的一整句歌词。"""

    line_key: tuple[Any, ...]
    line_idx: int
    born_mono: float
    glyphs: list[GlyphParticle] = field(default_factory=list)
    exiting: bool = False
    exit_start_mono: float = 0.0
    exit_total_dur: float = 1.15


# ── 核心引擎：BigLyricsRenderer ───────────────────────────────────────────────
class BigLyricsRenderer:
    """绝美电影级全屏动态排印歌词引擎（铺满 80% 屏幕、每字独立错落流动）。"""

    # 顶点布局：14 个 float / 顶点（每字 6 顶点 = 84 float）
    # a_pos(2) + a_uv(2) + a_cell_uv(4) + a_state(4) + a_meta(4) = 16 floats
    FLOATS_PER_VERTEX = 16

    def __init__(self, pango_ctx: Any) -> None:
        self.atlas = GlyphAtlas(pango_ctx)
        self.track: dict[str, Any] | None = None
        self.has_yrc: bool = False
        self.lines: list[dict[str, Any]] = []
        self.line_starts: list[float] = []
        self.connected: bool = False
        self.has_content: bool = False

        self._active_lines: list[ActiveLine] = []
        self._current_key: tuple[Any, ...] | None = None
        self._last_aspect: float = 16.0 / 9.0
        self._line_counter: int = 0
        self._vbuf: array.array = array.array("f")
        self._vert_count: int = 0

    def release_resources(self) -> None:
        """清空活跃句并释放底层 Cairo 图集内存。"""
        self._active_lines.clear()
        self._current_key = None
        self.has_content = False
        del self._vbuf[:]
        self._vert_count = 0
        self.atlas.release_memory()

    def apply_track(self, msg: dict[str, Any]) -> None:
        self.connected = True
        self.track = msg.get("track")
        self.has_yrc = bool(msg.get("has_yrc", False))
        self.lines = list(msg.get("lines") or [])
        self.line_starts = [float(r["start"]) for r in self.lines]
        self._current_key = None
        now = time.monotonic()
        for al in self._active_lines:
            if not al.exiting:
                al.exiting = True
                al.exit_start_mono = now

    def _resolve_target(
        self, pos: float
    ) -> tuple[tuple[Any, ...] | None, int, str, str, float, float, list[list[Any]]]:
        """解析当前播放位置应展示的歌词行或曲目标题。"""
        if not self.track:
            return None, -1, "", "", 0.0, 0.0, []

        title = str(self.track.get("name") or "").strip()
        artists = str(self.track.get("artists") or "").strip()
        track_id = self.track.get("id") or title

        if not self.lines:
            if not title:
                return None, -1, "", "", 0.0, 0.0, []
            return ("title_only", track_id), 0, title, artists or "纯音乐", 0.0, 1e9, []

        idx = bisect.bisect_right(self.line_starts, pos) - 1
        if idx < 0:
            first = self.lines[0]
            first_start = float(first["start"])
            wait = max(0.0, first_start - pos)
            if wait <= 0.36:
                orig = str(first.get("orig") or "").strip()
                trans = str(first.get("trans") or "").strip()
                end = float(first["end"])
                if not orig:
                    return None, 0, "", "", first_start, end, []
                return (
                    ("line", track_id, 0, orig),
                    0,
                    orig,
                    trans,
                    first_start,
                    end,
                    list(first.get("words") or []),
                )
            if wait > 1.25:
                if not title:
                    return None, -1, "", "", 0.0, 0.0, []
                return ("intro", track_id), 0, title, artists, 0.0, first_start, []
            # 0.36 < wait <= 1.25：歌名优雅退场留白，再干净衔接第一句歌词
            return None, -1, "", "", 0.0, first_start, []

        row = self.lines[idx]
        start = float(row["start"])
        end = float(row["end"])
        orig = str(row.get("orig") or "").strip()
        trans = str(row.get("trans") or "").strip()

        # 1) 优先检查下一句预浮入窗口（含间奏结束后直接衔接下一句，彻底杜绝旧句复现！）
        if (idx + 1) < len(self.line_starts):
            next_start = self.line_starts[idx + 1]
            # 间奏留白后提前 0.36s 浮入，连唱切句提前 0.30s 且需当前句接近尾声
            lead = 0.36 if (pos > end + 1.2) else 0.30
            if (next_start - pos) <= lead and pos >= end - 0.12:
                nrow = self.lines[idx + 1]
                norig = str(nrow.get("orig") or "").strip()
                ntrans = str(nrow.get("trans") or "").strip()
                if norig:
                    return (
                        ("line", track_id, idx + 1, norig),
                        idx + 1,
                        norig,
                        ntrans,
                        float(nrow["start"]),
                        float(nrow["end"]),
                        list(nrow.get("words") or []),
                    )

        # 2) 已超过当前句结束余韵且尚未进入下一句预浮入窗口 -> 始终保持间奏/尾奏留白
        hold_tail = 1.45 if self.has_yrc else 1.25
        if pos > end + hold_tail:
            return None, idx, "", "", start, end, []

        if not orig:
            return None, idx, "", "", start, end, []
        return ("line", track_id, idx, orig), idx, orig, trans, start, end, list(row.get("words") or [])

    # ── 智能逐字切分 + YRC/LRC 精确时间轴映射 ────────────────────────────────
    def _tokenize_line(
        self,
        orig: str,
        line_start: float,
        line_end: float,
        words: list[list[Any]],
        is_banner: bool = False,
    ) -> list[LyricToken]:
        is_jp = _has_kana(orig)
        main_style = "main_jp" if is_jp else "main_zh"
        soft_style = "soft_jp" if is_jp else "soft_zh"

        # 1) 构造 (text_chunk, t_start, t_end) 原始序列
        raw_spans: list[tuple[str, float, float]] = []
        if self.has_yrc and words and not is_banner:
            prev_end = line_start
            for w in words:
                if len(w) < 3:
                    continue
                ws = float(w[0])
                we = float(w[1])
                wt = str(w[2])
                if not wt:
                    continue
                # 兼容极少数把 dur 存进 w[1] 的格式
                if we < ws:
                    we = ws + max(0.05, we)
                we = max(ws + 0.04, we)
                prev_end = max(prev_end, we)
                raw_spans.append((wt, ws, we))

        if not raw_spans:
            dur = max(0.4, line_end - line_start) if not is_banner else 2.2
            active_dur = dur * 0.88
            raw_spans = [(orig, line_start, line_start + active_dur)]

        # 判断是否偏纯拉丁短句（若是且总字母<=14，拆单字母更灵动）
        non_space = [c for c in orig if not c.isspace()]
        cjk_count = sum(1 for c in non_space if _is_cjk_char(c))
        latin_letter_mode = (cjk_count == 0 and len(non_space) <= 14)

        tokens: list[LyricToken] = []
        pending_break = False
        pending_gap = False

        for chunk, cs, ce in raw_spans:
            # 将单个 span 拆分为原子件（CJK 逐字、英文逐词或短句逐字母）
            atoms: list[tuple[str, str]] = []  # (kind, text) kind in {'char','word','punct','break','space'}
            buf: list[str] = []

            def flush_buf() -> None:
                if not buf:
                    return
                word_str = "".join(buf)
                buf.clear()
                if latin_letter_mode:
                    for ch in word_str:
                        atoms.append(("char", ch))
                else:
                    atoms.append(("word", word_str))

            for ch in chunk:
                if ch in _SILENT_PUNCT:
                    flush_buf()
                    if ch in (",", "，", "、", ";", "；", "。", "—", "–", "/"):
                        atoms.append(("break", ""))
                    else:
                        atoms.append(("space", ""))
                elif ch in _EXPRESSIVE_PUNCT:
                    flush_buf()
                    atoms.append(("punct", ch))
                elif _is_cjk_char(ch):
                    flush_buf()
                    atoms.append(("char", ch))
                else:
                    buf.append(ch)
            flush_buf()

            visible_atoms = [a for a in atoms if a[0] in ("char", "word", "punct")]
            total_w = sum(max(1, len(a[1])) for a in visible_atoms)
            span_dur = max(0.05, ce - cs)
            cursor_t = cs

            for kind, txt in atoms:
                if kind == "break":
                    pending_break = True
                    pending_gap = True
                    continue
                if kind == "space":
                    pending_gap = True
                    continue

                w_frac = max(1, len(txt)) / float(max(1, total_w))
                t0 = cursor_t
                t1 = cursor_t + span_dur * w_frac
                cursor_t = t1

                # 语义尺寸权重与轻重字风
                is_soft = False
                is_hero = False
                style = main_style
                if kind == "punct":
                    sw = 0.64
                    is_soft = True
                    style = soft_style
                elif len(txt) == 1 and (txt in _SOFT_JP_PARTICLES or txt in _SOFT_ZH_PARTICLES):
                    sw = 0.72
                    is_soft = True
                    style = soft_style
                elif len(txt) == 1 and _is_hiragana(txt):
                    sw = 0.88
                elif len(txt) == 1 and _is_katakana(txt):
                    sw = 1.06
                    is_hero = True
                elif len(txt) == 1 and _is_cjk_char(txt):
                    sw = 1.18
                    is_hero = True
                elif kind == "word":
                    # 英文长单词适当收敛以免横宽过长
                    sw = _clamp(1.16 - max(0, len(txt) - 5) * 0.055, 0.76, 1.18)
                    is_hero = len(txt) >= 3
                else:
                    sw = 1.06
                    is_hero = True

                tokens.append(
                    LyricToken(
                        text=txt,
                        t_start=t0,
                        t_end=max(t0 + 0.06, t1),
                        style=style,
                        size_weight=sw,
                        break_before=pending_break,
                        gap_before=pending_gap,
                        is_soft=is_soft,
                        is_hero_cand=is_hero,
                    )
                )
                pending_break = False
                pending_gap = False

        # 兜底：若整行全是标点被滤空，保留原文首段
        if not tokens and orig.strip():
            tokens.append(
                LyricToken(
                    text=orig.strip()[:16],
                    t_start=line_start,
                    t_end=max(line_start + 0.6, line_end),
                    style=main_style,
                    size_weight=1.1,
                    is_hero_cand=True,
                )
            )

        # 限制极端超长句（防爆图集与屏幕过度拥挤）
        if len(tokens) > 40:
            tokens = tokens[:40]

        # 拖长音 Hero 加权：歌唱持续时间明显高于中位数的实词获得视觉主焦放大！
        if len(tokens) >= 3 and not is_banner:
            durs = [max(0.05, tk.t_end - tk.t_start) for tk in tokens]
            med = max(0.08, statistics.median(durs))
            for i, tk in enumerate(tokens):
                ratio = durs[i] / med
                if tk.is_hero_cand and ratio > 1.32:
                    boost = _clamp(1.0 + (ratio - 1.32) * 0.24, 1.0, 1.34)
                    tk.size_weight *= boost

        return tokens

    # ── 80% 全屏铺满 + 每字动态字号错落排印算法 ───────────────────────────────
    def _split_into_rows(
        self,
        tokens: list[LyricToken],
        packed: list[tuple[int, AtlasEntry]],
        mode: int,
    ) -> list[list[int]]:
        """综合词元个数与视觉宽窄比，智能切分为 1~4 层自然错落的行组。"""
        n = len(tokens)
        if n <= 1:
            return [list(range(n))]

        # 估算各词元的视觉宽高比，避免英文宽词横排挤成细长条
        widths = [
            _clamp(ent.ink_w / max(1.0, ent.ink_h), 0.45, 4.2) * tk.size_weight
            for tk, (_sz, ent) in zip(tokens, packed)
        ]
        total_w = sum(widths)

        if n <= 3 and total_w <= 6.2:
            return [list(range(n))]
        if n == 4 and total_w <= 5.8 and mode == 0:
            return [list(range(n))]

        # 按视觉总宽度自动匹配最佳层数，使纵横比贴近 16:9 满屏 80% 舞台
        if total_w <= 11.5 and n <= 11:
            target_rows = 2
        elif total_w <= 20.5 and n <= 24:
            target_rows = 3
        else:
            target_rows = 4
        target_rows = min(target_rows, n)
        if target_rows <= 1:
            return [list(range(n))]

        # 计算前缀宽以便按视觉宽均匀断行，同时优先贴近自然语意断点（逗号/空格）
        prefix = [0.0]
        for w in widths:
            prefix.append(prefix[-1] + w)
        break_cands = {i for i in range(1, n) if tokens[i].break_before}
        gap_cands = {i for i in range(1, n) if tokens[i].gap_before}

        cuts: list[int] = [0]
        for r in range(1, target_rows):
            ideal_w = total_w * r / float(target_rows)
            best = cuts[-1] + 1
            best_score = 1e9
            for c in range(cuts[-1] + 1, n - (target_rows - 1 - r) + 1):
                w_diff = abs(prefix[c] - ideal_w) / max(1.0, total_w / float(target_rows))
                bonus = -0.38 if c in break_cands else (-0.18 if c in gap_cands else 0.0)
                if r == target_rows - 1 and (n - c) == 1 and n > 4:
                    bonus += 0.45
                score = w_diff + bonus
                if score < best_score:
                    best_score = score
                    best = c
            if best > cuts[-1] and best < n:
                cuts.append(best)
        cuts.append(n)

        rows: list[list[int]] = []
        for a, b in zip(cuts[:-1], cuts[1:]):
            if b > a:
                rows.append(list(range(a, b)))
        return rows if rows else [list(range(n))]

    def _ensure_atlas_entries(
        self,
        tokens: list[LyricToken],
        trans: str,
    ) -> tuple[list[tuple[int, AtlasEntry]], AtlasEntry | None]:
        """为本句所有字形与译文光栅化并打包进图集；若图集满则保留当前活跃字形重建。"""

        def try_pack(scale_down: float = 1.0) -> tuple[list[tuple[int, AtlasEntry]] | None, AtlasEntry | None]:
            out: list[tuple[int, AtlasEntry]] = []
            for tk in tokens:
                base_px = 196 if len(tk.text) == 1 else int(_clamp(176 - len(tk.text) * 8, 92, 168))
                px_sz = max(72, int(round(base_px * scale_down)))
                ent = self.atlas.get_or_rasterize(tk.text, style=tk.style, px_size=px_sz)
                if ent is None:
                    return None, None
                out.append((px_sz, ent))
            t_ent = None
            if trans.strip():
                t_sz = max(28, int(round(42 * scale_down)))
                t_ent = self.atlas.get_or_rasterize(trans.strip(), style="trans", px_size=t_sz)
                if t_ent is None:
                    return None, None
            return out, t_ent

        packed, t_ent = try_pack(1.0)
        if packed is not None:
            return packed, t_ent

        # 图集已满：清空后先重新放入仍在退场的上一句字形，再放本句
        self.atlas.clear()
        for al in self._active_lines:
            for g in al.glyphs:
                new_ent = self.atlas.get_or_rasterize(g.text, style=g.style, px_size=g.px_size)
                if new_ent is not None:
                    g.entry = new_ent

        packed, t_ent = try_pack(1.0)
        if packed is not None:
            return packed, t_ent

        # 极端兜底：放弃旧残影并轻微缩小光栅化字号确保 100% 成功
        self._active_lines.clear()
        self.atlas.clear()
        packed, t_ent = try_pack(0.75)
        return (packed or []), t_ent

    def _compose_active_line(
        self,
        line_key: tuple[Any, ...],
        line_idx: int,
        orig: str,
        trans: str,
        line_start: float,
        line_end: float,
        words: list[list[Any]],
        now: float,
        aspect: float,
        is_banner: bool = False,
    ) -> ActiveLine:
        tokens = self._tokenize_line(orig, line_start, line_end, words, is_banner=is_banner)
        packed, trans_entry = self._ensure_atlas_entries(tokens, trans)
        n = len(packed)
        self._line_counter += 1
        lseq = self._line_counter

        # 选择构图流派（每句流转变化，视觉永不乏味）
        if n <= 4:
            mode = 0  # MONUMENTAL 超巨幅海报
        elif n >= 15:
            mode = 4  # POETIC_TERRACE 多层错落诗意梯田
        else:
            mode = 1 + ((line_idx + lseq) % 3)  # 1:斜瀑 2:双峰潮汐 3:杂志焦点

        # 1) 为每个字分配强烈的动态字号倍率（错落有致！）
        hero_idx = -1
        if n >= 3:
            best_score = -1.0
            for i, tk in enumerate(tokens):
                sc = tk.size_weight * (1.22 if tk.is_hero_cand else 0.65)
                sc += 0.16 * _hash_float(lseq, i, 11)
                if sc > best_score:
                    best_score = sc
                    hero_idx = i

        size_muls: list[float] = []
        for i, tk in enumerate(tokens):
            # 确定性交错韵律波：相邻字大小反相起伏
            alt_wave = 1.0 + (0.13 if (i % 2 == 0) else -0.10) * (0.65 + 0.35 * _hash_float(lseq, i, 3))
            rand_var = 0.90 + 0.24 * _hash_float(lseq, i, 7)
            mul = tk.size_weight * alt_wave * rand_var
            if i == hero_idx and mode in (0, 3):
                mul *= 1.28
            elif i == 0 and not tk.is_soft:
                mul *= 1.12
            size_muls.append(_clamp(mul, 0.62, 1.56))

        # 2) 在等比物理平面 (X in aspect-units, Y in height-units) 中排布错落坐标
        rows = self._split_into_rows(tokens, packed, mode)
        num_rows = len(rows)

        # 每个字的物理墨迹宽/高（以临时基准字高 1.0 单位起步，最后统一 Auto-Fit 撑满 80% 屏幕！）
        items: list[dict[str, Any]] = []
        row_gap = 1.18 if num_rows <= 2 else 1.12
        slope_sign = 1.0 if ((line_idx + lseq) % 2 == 0) else -1.0

        for r_i, row_indices in enumerate(rows):
            # 行纵坐标（自上而下排，OpenGL Y 向上）
            row_center_y = (num_rows - 1) * 0.5 * row_gap - r_i * row_gap

            # 每行交错水平缩进（非对称杂志感）
            if mode == 1:  # 斜瀑
                row_shift_x = (r_i - (num_rows - 1) * 0.5) * 0.95 * slope_sign
            elif mode == 2:  # 双峰潮汐
                row_shift_x = (-0.55 if r_i % 2 == 0 else 0.55) * slope_sign
            elif mode == 4:  # 诗意梯田
                offsets = (-0.65, 0.55, -0.20, 0.38)
                row_shift_x = offsets[r_i % len(offsets)] * slope_sign
            else:
                row_shift_x = (r_i - (num_rows - 1) * 0.5) * 0.45 * slope_sign

            # 计算行内横向推进
            row_items: list[dict[str, Any]] = []
            cursor_x = 0.0
            row_len = len(row_indices)
            for col_i, idx in enumerate(row_indices):
                px_sz, ent = packed[idx]
                tk = tokens[idx]
                h_unit = size_muls[idx]
                # 有效墨迹宽高比（限制单字极端窄宽，例如标点或 "I"）
                ink_aspect = _clamp(ent.ink_w / max(1.0, ent.ink_h), 0.36, 4.2)
                w_unit = h_unit * ink_aspect

                if col_i > 0:
                    gap = 0.14 * min(h_unit, row_items[-1]["h"])
                    if tk.break_before:
                        gap += 0.36
                    elif tk.gap_before:
                        gap += 0.24
                    cursor_x += gap

                cx = cursor_x + w_unit * 0.5
                cursor_x += w_unit

                # 每字独立垂直错落（形成流动曲线与高低起伏）
                frac = col_i / float(max(1, row_len - 1)) if row_len > 1 else 0.5
                jitter_y = (_hash_float(lseq, idx, 19) - 0.5) * 0.44 * h_unit
                if mode == 0:  # MONUMENTAL 大斜对角
                    wave_y = (0.5 - frac) * 0.62 * slope_sign
                elif mode == 1:  # DIAGONAL_CASCADE
                    wave_y = (0.5 - frac) * 0.55 * slope_sign
                elif mode == 2:  # TWIN_WAVE 丝滑正弦波
                    wave_y = math.sin(frac * math.pi * 1.45 + r_i * 1.35) * 0.34
                elif mode == 3:  # HERO_EDITORIAL 围绕主字高低错落
                    wave_y = math.cos(frac * math.pi * 1.65 + 0.4) * 0.30
                else:  # POETIC_TERRACE
                    wave_y = math.sin(frac * math.pi * 1.8 + r_i * 1.7) * 0.24

                # 精致小虚词略偏向斜上/斜下，像日系 MV 排版依偎在主字旁边
                if tk.is_soft:
                    jitter_y += (0.24 if (idx % 2 == 0) else -0.22) * h_unit

                cy = row_center_y + wave_y + jitter_y
                # 独立微倾角（±5.5° 以内，既有流动感又保持高级克制）
                base_rot = (_hash_float(lseq, idx, 23) - 0.5) * 0.165
                if idx == hero_idx:
                    base_rot *= 0.45

                row_items.append(
                    {
                        "idx": idx,
                        "px_sz": px_sz,
                        "ent": ent,
                        "tk": tk,
                        "x": cx,
                        "y": cy,
                        "w": w_unit,
                        "h": h_unit,
                        "rot": base_rot,
                    }
                )

            # 本行水平居中 + 错位缩进
            if row_items:
                r_mid = 0.5 * (
                    min(it["x"] - it["w"] * 0.5 for it in row_items)
                    + max(it["x"] + it["w"] * 0.5 for it in row_items)
                )
                for it in row_items:
                    it["x"] = it["x"] - r_mid + row_shift_x
                items.extend(row_items)

        # 3) 轻量级防重叠斥力松弛（保留紧凑艺术呼吸，绝不让笔画互相糊死）
        for _ in range(8):
            for i in range(len(items)):
                a = items[i]
                for j in range(i + 1, len(items)):
                    b = items[j]
                    dx = b["x"] - a["x"]
                    dy = b["y"] - a["y"]
                    min_x = (a["w"] + b["w"]) * 0.50 + 0.055
                    min_y = (a["h"] + b["h"]) * 0.48 + 0.055
                    ox = min_x - abs(dx)
                    oy = min_y - abs(dy)
                    if ox > 0.0 and oy > 0.0:
                        if ox < oy:
                            push = ox * 0.52 * (1.0 if dx >= 0.0 else -1.0)
                            a["x"] -= push * 0.5
                            b["x"] += push * 0.5
                        else:
                            push = oy * 0.52 * (1.0 if dy >= 0.0 else -1.0)
                            a["y"] -= push * 0.5
                            b["y"] += push * 0.5

        # 4) Auto-Fit 归一化：精准撑满整个屏幕 80% 视觉舞台！
        has_trans = trans_entry is not None
        # 主舞台区域：宽度占屏幕 84%，高度占屏幕 76%~80%（底部给译文留 9% 黄金空白）
        stage_x0, stage_x1 = 0.08, 0.92
        stage_y0 = 0.145 if has_trans else 0.10
        stage_y1 = 0.905
        stage_w_phys = (stage_x1 - stage_x0) * aspect
        stage_h_phys = stage_y1 - stage_y0

        if items:
            bx0 = min(it["x"] - it["w"] * 0.5 for it in items)
            bx1 = max(it["x"] + it["w"] * 0.5 for it in items)
            by0 = min(it["y"] - it["h"] * 0.5 for it in items)
            by1 = max(it["y"] + it["h"] * 0.5 for it in items)
            bw = max(0.25, bx1 - bx0)
            bh = max(0.25, by1 - by0)

            scale_fit = min(stage_w_phys / bw, stage_h_phys / bh)
            # 防止单字/超短句放大过头顶破屏幕：单字墨迹最高占屏高 44%（弹跳后约 52%）
            max_item_h = max(it["h"] for it in items)
            max_allowed_scale = 0.44 / max(0.2, max_item_h)
            scale = min(scale_fit, max_allowed_scale)

            # 轻微的构图重心微偏，让每句画面更灵动（绝不机械死居中）
            center_bias_x = (_hash_float(lseq, 91, 1) - 0.5) * 0.035
            center_bias_y = (_hash_float(lseq, 97, 2) - 0.5) * 0.030
            target_cx = (stage_x0 + stage_x1) * 0.5 + center_bias_x
            target_cy = (stage_y0 + stage_y1) * 0.5 + center_bias_y
            src_cx = (bx0 + bx1) * 0.5
            src_cy = (by0 + by1) * 0.5
        else:
            scale = 0.20
            target_cx = 0.5
            target_cy = 0.52
            src_cx = 0.0
            src_cy = 0.0

        # 5) 生成每个独立字形的流体动力学粒子对象
        # 整句共享一个优雅的“流动风向”，但每个字叠加独立发散角
        wind_ang = 0.25 + 0.55 * _hash_float(lseq, 33, 5)  # 偏右上飘散
        if (lseq % 3) == 2:
            wind_ang = math.pi - wind_ang  # 偶尔偏左上飘散
        wind_x = math.cos(wind_ang)
        wind_y = math.sin(wind_ang)

        stagger_step = min(0.038, 0.34 / max(1, n))
        glyphs: list[GlyphParticle] = []

        for order_i, it in enumerate(items):
            idx = it["idx"]
            ent: AtlasEntry = it["ent"]
            tk: LyricToken = it["tk"]

            nx = target_cx + ((it["x"] - src_cx) * scale) / max(0.3, aspect)
            ny = target_cy + (it["y"] - src_cy) * scale
            ink_h_norm = it["h"] * scale
            # 渲染 Quad 高度必须包含图集安全光晕 padding
            cell_h_norm = ink_h_norm * ent.pad_scale_y
            cell_aspect = ent.cell_w / max(1.0, ent.cell_h)

            # 景深层次：核心大字靠近前景 (1.0)，轻虚词或边缘字略退后 (0.76~0.92)
            depth = 1.0 if (idx == hero_idx or it["h"] >= 1.15) else (0.78 + 0.20 * _hash_float(lseq, idx, 41))
            if tk.is_soft:
                depth = min(depth, 0.84)

            seed = _hash_float(lseq, idx, 53) * 10.0 + idx * 0.137

            # 入场起点：顺着来流方向错落涌入
            in_spread = (0.048 + 0.038 * _hash_float(lseq, idx, 61)) * (1.15 - 0.25 * depth)
            in_ang = wind_ang + math.pi + (_hash_float(lseq, idx, 67) - 0.5) * 0.72
            in_dx = math.cos(in_ang) * in_spread / max(0.5, aspect * 0.65)
            in_dy = math.sin(in_ang) * in_spread - 0.022
            in_rot = (_hash_float(lseq, idx, 71) - 0.5) * 0.34

            # 退场向量：顺风轻盈加速飘散
            out_spread = 0.068 + 0.060 * _hash_float(lseq, idx, 73)
            out_ang = wind_ang + (_hash_float(lseq, idx, 79) - 0.5) * 0.65
            out_dx = math.cos(out_ang) * out_spread / max(0.5, aspect * 0.65)
            out_dy = math.sin(out_ang) * out_spread + 0.018
            out_rot = (_hash_float(lseq, idx, 83) - 0.5) * 0.36

            glyphs.append(
                GlyphParticle(
                    text=tk.text,
                    style=tk.style,
                    px_size=it["px_sz"],
                    entry=ent,
                    t_start=tk.t_start,
                    t_end=tk.t_end,
                    is_trans=False,
                    base_x=nx,
                    base_y=ny,
                    base_h_norm=cell_h_norm,
                    cell_aspect=cell_aspect,
                    base_rot=it["rot"],
                    depth=depth,
                    seed=seed,
                    enter_delay=order_i * stagger_step,
                    enter_dur=0.46 + 0.14 * _hash_float(lseq, idx, 89),
                    in_dx=in_dx,
                    in_dy=in_dy,
                    in_rot=in_rot,
                    exit_delay=order_i * min(0.026, 0.24 / max(1, n)),
                    exit_dur=0.68 + 0.20 * _hash_float(lseq, idx, 97),
                    out_dx=out_dx,
                    out_dy=out_dy,
                    out_rot=out_rot,
                    drift_fx=0.48 + 0.36 * _hash_float(lseq, idx, 101),
                    drift_fy=0.42 + 0.32 * _hash_float(lseq, idx, 103),
                    drift_amp_x=(0.0065 + 0.0060 * _hash_float(lseq, idx, 107)) * depth,
                    drift_amp_y=(0.0095 + 0.0085 * _hash_float(lseq, idx, 109)) * depth,
                    drift_rot_amp=0.022 + 0.024 * _hash_float(lseq, idx, 113),
                )
            )

        # 按景深从远到近排序绘制，前景巨字自然叠加在远景轻字之上，立体感倍增！
        glyphs.sort(key=lambda g: g.depth)

        # 6) 译文浮标（优雅悬浮于底部黄金留白区）
        if trans_entry is not None:
            # 译文高度约占屏幕 4.0%（1080p ~ 43px 含 padding，清晰精致不抢戏），宽超 78% 时自动收敛
            t_cell_aspect = trans_entry.cell_w / max(1.0, trans_entry.cell_h)
            t_h_norm = 0.068
            t_w_norm = (t_h_norm * t_cell_aspect) / max(0.3, aspect)
            if t_w_norm > 0.78:
                t_h_norm *= 0.78 / t_w_norm
            glyphs.append(
                GlyphParticle(
                    text=trans.strip(),
                    style="trans",
                    px_size=42,
                    entry=trans_entry,
                    t_start=line_start,
                    t_end=line_end,
                    is_trans=True,
                    base_x=0.5,
                    base_y=0.078,
                    base_h_norm=t_h_norm,
                    cell_aspect=t_cell_aspect,
                    base_rot=0.0,
                    depth=0.95,
                    seed=0.4 + _hash_float(lseq, 777, 1) * 4.0,
                    enter_delay=0.08,
                    enter_dur=0.55,
                    in_dx=0.0,
                    in_dy=-0.024,
                    in_rot=0.0,
                    exit_delay=0.0,
                    exit_dur=0.58,
                    out_dx=wind_x * 0.025,
                    out_dy=0.022,
                    out_rot=0.0,
                    drift_fx=0.36,
                    drift_fy=0.30,
                    drift_amp_x=0.0035,
                    drift_amp_y=0.0035,
                    drift_rot_amp=0.0,
                )
            )

        max_exit = max((g.exit_delay + g.exit_dur for g in glyphs), default=0.85)
        return ActiveLine(
            line_key=line_key,
            line_idx=line_idx,
            born_mono=now,
            glyphs=glyphs,
            exiting=False,
            exit_start_mono=0.0,
            exit_total_dur=max_exit + 0.05,
        )

    # ── 每帧流体动力学更新 + 顶点缓冲构建 ────────────────────────────────────
    def update(
        self,
        pos: float,
        playing: bool,
        dt: float,
        now: float,
        win_w: int,
        win_h: int,
        audio: Any,
    ) -> tuple[tuple[bool, int, int, bytes] | None, array.array, int]:
        """更新每字独立物理状态并生成 OpenGL 顶点流。

        返回 (dirty_atlas_upload_or_None, float32_vertex_array, vertex_count)。
        """
        aspect = float(max(1, win_w)) / float(max(1, win_h))
        aspect_changed = abs(aspect - self._last_aspect) > 0.03
        self._last_aspect = aspect

        target_key, line_idx, orig, trans, line_start, line_end, words = self._resolve_target(pos)
        is_banner = target_key is not None and target_key[0] in ("intro", "title_only")

        # 切句逻辑：旧句进入丝滑风散退场，新句交错浮入
        if target_key != self._current_key or (aspect_changed and target_key is not None):
            for al in self._active_lines:
                if not al.exiting:
                    al.exiting = True
                    al.exit_start_mono = now
            self._current_key = target_key
            if target_key is not None and orig:
                new_line = self._compose_active_line(
                    target_key,
                    line_idx,
                    orig,
                    trans,
                    line_start,
                    line_end,
                    words,
                    now,
                    aspect,
                    is_banner=is_banner,
                )
                # 如果是中途 Seek 到一句中间，已经唱过的字立即同步亮态
                for g in new_line.glyphs:
                    if not g.is_trans and pos >= g.t_end:
                        g.lit_smooth = 1.0
                        g.was_lit = True
                    elif not g.is_trans and pos > g.t_start:
                        g.lit_smooth = _clamp((pos - g.t_start) / max(0.04, g.t_end - g.t_start), 0.0, 1.0)
                        g.was_lit = True
                self._active_lines.append(new_line)
                # 最多同时保留 2 个旧残影 + 1 个当前句，防止快进连切堆积
                if len(self._active_lines) > 3:
                    self._active_lines = self._active_lines[-3:]

        # 清理已彻底消融的退场句
        self._active_lines = [
            al
            for al in self._active_lines
            if (not al.exiting) or ((now - al.exit_start_mono) < al.exit_total_dur)
        ]

        self.has_content = bool(self._active_lines)
        upload = self.atlas.consume_dirty_upload()

        vbuf = self._vbuf
        del vbuf[:]
        if not self._active_lines:
            self._vert_count = 0
            return upload, vbuf, 0

        # 读取音频状态（解耦且丝滑）
        presence = float(getattr(audio, "presence", 0.0)) if audio is not None else 0.0
        audio_p = _smoothstep(0.02, 0.20, presence)
        flow_t = float(getattr(audio, "flow_time", now)) if audio is not None else now
        flow_vx = float(getattr(audio, "flow_vel_x", 0.0)) if audio is not None else 0.0
        flow_vy = float(getattr(audio, "flow_vel_y", 0.0)) if audio is not None else 0.0
        sub_v = float(getattr(audio, "sub", 0.0)) if audio is not None else 0.0
        kick_v = float(getattr(audio, "beat_kick", 0.0)) if audio is not None else 0.0
        beat_rand = float(getattr(audio, "beat_rand_amp", 1.0)) if audio is not None else 1.0
        beat_dx = float(getattr(audio, "beat_dir_x", 0.0)) if audio is not None else 0.0
        beat_dy = float(getattr(audio, "beat_dir_y", 1.0)) if audio is not None else 1.0

        kick_curve = kick_v * kick_v * (3.0 - 2.0 * kick_v)
        sub_curve = sub_v * sub_v * (3.0 - 2.0 * sub_v)
        drift_clock = now * (0.95 if playing else 0.35) + flow_t * 0.28

        # 逐句、逐字更新物理并写入三角形顶点（每字 2 个三角形 = 6 顶点）
        v_count = 0
        inv_aspect = 1.0 / max(0.3, aspect)
        lit_rate = min(1.0, dt * 36.0)
        kick_follow = min(1.0, dt * 16.0)

        for al in self._active_lines:
            age = max(0.0, now - al.born_mono)
            exit_age = max(0.0, now - al.exit_start_mono) if al.exiting else 0.0
            is_line_banner = al.line_key[0] in ("intro", "title_only")

            for g in al.glyphs:
                # 1) 入场缓动 (0 -> 1)
                e_raw = _clamp((age - g.enter_delay) / max(0.08, g.enter_dur), 0.0, 1.0)
                if e_raw <= 0.001:
                    continue
                e_pos = _ease_out_back(e_raw, 0.92)
                e_alpha = _smoothstep(0.0, 0.62, e_raw)
                e_blur = (1.0 - _ease_out_quint(e_raw)) * 0.88

                # 2) 退场缓动 (0 -> 1)
                x_raw = 0.0
                if al.exiting:
                    x_raw = _clamp((exit_age - g.exit_delay) / max(0.10, g.exit_dur), 0.0, 1.0)
                    if x_raw >= 0.999:
                        continue
                x_move = _ease_in_cubic(x_raw)
                x_alpha = 1.0 - _smoothstep(0.12, 1.0, x_raw)
                x_blur = x_raw * 0.95
                dissolve = _smoothstep(0.08, 0.96, x_raw)

                # 3) 歌唱逐字点亮进度 & 弹簧激发脉冲
                if g.is_trans:
                    lit_prog = 0.0
                    ignite = 0.0
                elif is_line_banner:
                    # 歌名前奏优雅呼吸亮态
                    lit_prog = 0.5 + 0.5 * math.sin(now * 0.9 + g.seed)
                    ignite = 0.0
                else:
                    if not al.exiting:
                        if pos >= g.t_end:
                            target_lit = 1.0
                        elif pos > g.t_start:
                            target_lit = (pos - g.t_start) / max(0.04, g.t_end - g.t_start)
                        else:
                            target_lit = 0.0
                        if target_lit > 0.02 and not g.was_lit:
                            g.was_lit = True
                            g.ignite_vel += 6.8  # 触发丝滑弹性膨胀冲顶！
                        elif target_lit == 0.0:
                            g.was_lit = False
                        g.lit_smooth += (target_lit - g.lit_smooth) * lit_rate

                    # 二阶阻尼弹簧更新 ignite_env（平滑过冲再温柔回落）
                    spring_acc = -42.0 * g.ignite_env - 9.2 * g.ignite_vel
                    g.ignite_vel += spring_acc * dt
                    g.ignite_env = max(0.0, g.ignite_env + g.ignite_vel * dt)
                    lit_prog = _clamp(g.lit_smooth, 0.0, 1.0)
                    ignite = _clamp(g.ignite_env, 0.0, 1.0)

                # 4) 每字独立持续液态漂浮 + 音律微互动
                ph1 = g.seed * 2.31
                ph2 = g.seed * 3.77 + 1.4
                drift_x = (
                    math.sin(drift_clock * g.drift_fx + ph1) * g.drift_amp_x
                    + math.cos(drift_clock * g.drift_fy * 0.68 + ph2) * (g.drift_amp_x * 0.48)
                    + flow_vx * 0.0022 * g.depth * audio_p
                )
                drift_y = (
                    math.cos(drift_clock * g.drift_fy + ph2) * g.drift_amp_y
                    + math.sin(drift_clock * g.drift_fx * 0.62 + ph1) * (g.drift_amp_y * 0.48)
                    + flow_vy * 0.0022 * g.depth * audio_p
                )
                drift_rot = math.sin(drift_clock * (g.drift_fx * 0.72) + ph1) * g.drift_rot_amp

                # 底鼓视差微推（按景深和字序呈现有机波纹弹性）
                kick_gain = (0.0085 * kick_curve * beat_rand * audio_p) * g.depth * (0.45 + 0.55 * lit_prog)
                target_kox = beat_dx * kick_gain * inv_aspect
                target_koy = beat_dy * kick_gain
                g.kick_ox += (target_kox - g.kick_ox) * kick_follow
                g.kick_oy += (target_koy - g.kick_oy) * kick_follow

                # 唱到时轻盈上浮 + 动态字号呼吸澎湃！
                lit_lift = (0.011 * lit_prog + 0.014 * ignite) * g.depth if not g.is_trans else 0.0
                breath_scale = 1.0 + 0.026 * math.sin(drift_clock * 1.15 + ph1) + 0.022 * sub_curve * audio_p
                if g.is_trans:
                    dyn_scale = (0.94 + 0.06 * e_pos) * (1.0 + 0.05 * x_move)
                else:
                    dyn_scale = (
                        (0.74 + 0.26 * e_pos)
                        * (1.0 + 0.055 * lit_prog + 0.165 * ignite + 0.032 * kick_curve * beat_rand * audio_p)
                        * breath_scale
                        * (1.0 + 0.14 * x_move)
                    )

                # 最终位置 (0..1) 与倾角 (弧度)
                cx = g.base_x + (1.0 - e_pos) * g.in_dx + x_move * g.out_dx + drift_x + g.kick_ox
                cy = g.base_y + (1.0 - e_pos) * g.in_dy + x_move * g.out_dy + drift_y + g.kick_oy + lit_lift
                rot = (
                    g.base_rot * (1.0 - 0.35 * lit_prog)
                    + (1.0 - e_pos) * g.in_rot
                    + x_move * g.out_rot
                    + drift_rot
                )

                # 景深轻柔虚化 + 入场对焦 + 退场散焦
                depth_blur = max(0.0, (0.90 - g.depth) * 0.42) * (1.0 - 0.72 * lit_prog)
                blur_amt = _clamp(max(depth_blur, max(e_blur, x_blur)), 0.0, 1.0)
                alpha = _clamp(e_alpha * x_alpha, 0.0, 1.0)
                if alpha < 0.003:
                    continue

                # 计算在物理纵高单位下的半宽/半高，再旋转并转为 OpenGL NDC (-1..1)
                hh = g.base_h_norm * dyn_scale * 0.5
                hw = hh * g.cell_aspect
                cos_r = math.cos(rot)
                sin_r = math.sin(rot)

                # 4 个角在等比空间中的偏移 -> 转为屏幕归一化再映射 NDC
                def corner_ndc(lx: float, ly: float) -> tuple[float, float]:
                    rx = lx * cos_r - ly * sin_r
                    ry = lx * sin_r + ly * cos_r
                    sx = cx + rx * inv_aspect
                    sy = cy + ry
                    return sx * 2.0 - 1.0, sy * 2.0 - 1.0

                # TL(-hw, +hh), TR(+hw, +hh), BL(-hw, -hh), BR(+hw, -hh)
                tl_x, tl_y = corner_ndc(-hw, hh)
                tr_x, tr_y = corner_ndc(hw, hh)
                bl_x, bl_y = corner_ndc(-hw, -hh)
                br_x, br_y = corner_ndc(hw, -hh)

                ent = g.entry
                u0, v0, u1, v1 = ent.u0, ent.v0, ent.u1, ent.v1
                meta_w = -abs(g.seed + 0.1) if g.is_trans else abs(g.seed + 0.1)

                # Cairo 纹理坐标 v0=顶, v1=底；对应 local_v: 1=顶, 0=底
                # 三角形 1: TL -> BL -> BR；三角形 2: TL -> BR -> TR
                vbuf.extend(
                    (
                        # TL
                        tl_x, tl_y, u0, v0, u0, v0, u1, v1, lit_prog, ignite, blur_amt, alpha, 0.0, 1.0, dissolve, meta_w,
                        # BL
                        bl_x, bl_y, u0, v1, u0, v0, u1, v1, lit_prog, ignite, blur_amt, alpha, 0.0, 0.0, dissolve, meta_w,
                        # BR
                        br_x, br_y, u1, v1, u0, v0, u1, v1, lit_prog, ignite, blur_amt, alpha, 1.0, 0.0, dissolve, meta_w,
                        # TL
                        tl_x, tl_y, u0, v0, u0, v0, u1, v1, lit_prog, ignite, blur_amt, alpha, 0.0, 1.0, dissolve, meta_w,
                        # BR
                        br_x, br_y, u1, v1, u0, v0, u1, v1, lit_prog, ignite, blur_amt, alpha, 1.0, 0.0, dissolve, meta_w,
                        # TR
                        tr_x, tr_y, u1, v0, u0, v0, u1, v1, lit_prog, ignite, blur_amt, alpha, 1.0, 1.0, dissolve, meta_w,
                    )
                )
                v_count += 6

        self._vert_count = v_count
        return upload, vbuf, v_count
