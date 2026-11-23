#!/usr/bin/env python3
"""ncm-lite 常驻顶层桌面歌词窗口（GTK3 + Cairo + PangoCairo，VSync 高刷 & PLL 毫秒同步）。"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from lyric_ipc import LyricIpcClient, get_lyric_sock_path

DEFAULT_WIDTH = 512
DEFAULT_HEIGHT = 64
CONFIG_PATH = Path.home() / ".config" / "ncm-lite" / "lyric_overlay.json"


def _can_import_gtk(python_bin: str) -> bool:
    try:
        res = subprocess.run(
            [
                python_bin,
                "-c",
                "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def find_gtk_python() -> str | None:
    """查找带有 PyGObject (Gtk 3.0) 的 Python 解释器。"""
    try:
        import gi  # noqa: F401

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401

        return sys.executable
    except Exception:  # noqa: BLE001
        pass

    candidates: list[str] = []
    for path in ("/usr/bin/python3", shutil.which("python3") or ""):
        if path and path not in candidates and os.path.abspath(path) != os.path.abspath(sys.executable):
            candidates.append(path)
    for cand in candidates:
        if _can_import_gtk(cand):
            return cand
    return None


def launch_desktop_lyrics(
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    offset_ms: int | None = None,
    locked: bool = False,
    foreground: bool = True,
    sock_path: str | None = None,
) -> int | subprocess.Popen:
    """启动桌面歌词进程（支持从 venv 自动切换至系统带 GTK 的 Python）。"""
    py = find_gtk_python()
    if not py:
        msg = "未找到可用的 PyGObject (GTK 3.0) 环境，请确认已安装 python3-gi / python3-gi-cairo。"
        if foreground:
            print(msg, file=sys.stderr)
            return 1
        raise RuntimeError(msg)

    script = str(Path(__file__).resolve())
    cmd = [py, script, "--width", str(width), "--height", str(height)]
    if offset_ms is not None:
        cmd.extend(["--offset-ms", str(offset_ms)])
    if locked:
        cmd.append("--lock")
    if sock_path:
        cmd.extend(["--sock", sock_path])

    if foreground:
        if os.path.abspath(py) == os.path.abspath(sys.executable):
            return _run_gui(
                width=width,
                height=height,
                offset_ms=offset_ms,
                locked=locked,
                sock_path=sock_path,
            )
        return subprocess.call(cmd)

    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class SyncedClock:
    """抗 IPC/音频缓冲抖动的单调锁相外推时钟（毫秒级时间对齐）。"""

    def __init__(self, offset_ms: int = 0) -> None:
        self.playing: bool = False
        self.paused: bool = False
        self.buffering: bool = False
        self.speed: float = 1.0
        self.duration: float = 0.0
        self.seq: int = -1
        self.anchor_pos: float = 0.0
        self.anchor_mono: float = 0.0
        self.offset_ms: int = offset_ms

    def is_running(self) -> bool:
        return self.playing and not self.paused and not self.buffering

    def update_from_state(self, msg: dict[str, Any], now: float | None = None) -> None:
        t_now = time.monotonic() if now is None else now
        was_running = self.is_running()

        playing = bool(msg.get("playing", False))
        paused = bool(msg.get("paused", False))
        buffering = bool(msg.get("buffering", False))
        speed = max(0.1, float(msg.get("speed", 1.0) or 1.0))
        duration = max(0.0, float(msg.get("duration", 0.0) or 0.0))
        seq = int(msg.get("seq", 0) or 0)
        raw_pos = max(0.0, float(msg.get("time_pos", 0.0) or 0.0))
        pos_at = float(msg.get("time_pos_at", 0.0) or 0.0)

        now_running = playing and not paused and not buffering
        if now_running and pos_at > 0.0:
            ipc_age = max(0.0, min(0.5, t_now - pos_at))
            target = raw_pos + ipc_age * speed
        else:
            target = raw_pos

        if not was_running or not now_running or seq != self.seq or self.anchor_mono <= 0.0:
            self.anchor_pos = target
            self.anchor_mono = t_now
        else:
            pred = self.anchor_pos + max(0.0, t_now - self.anchor_mono) * self.speed
            err = target - pred
            if abs(err) > 0.08:
                self.anchor_pos = target
                self.anchor_mono = t_now
            elif abs(err) > 0.0015:
                # 一阶锁相阻尼，并对正常播歌中的微小负抖动做单调不倒退钳位
                blended = pred + err * 0.35
                if err < 0 and blended < pred:
                    blended = max(pred - 0.002, blended)
                self.anchor_pos = max(0.0, blended)
                self.anchor_mono = t_now

        self.playing = playing
        self.paused = paused
        self.buffering = buffering
        self.speed = speed
        self.duration = duration
        self.seq = seq

    def position(self, now: float | None = None) -> float:
        t_now = time.monotonic() if now is None else now
        if self.is_running() and self.anchor_mono > 0.0:
            base = self.anchor_pos + max(0.0, t_now - self.anchor_mono) * self.speed
        else:
            base = self.anchor_pos
        return max(0.0, base + self.offset_ms / 1000.0)


def compute_word_fill_ratio(words: list[list[Any]], orig: str, pos: float) -> tuple[int, float]:
    """给定逐字列表 [[start, end, text], ...] 与当前时间 pos，返回 (当前字起始字节偏移, 字内0..1进度) 及是否完全唱完。

    返回 (byte_cursor, current_word_frac)。
    """
    if not words:
        return 0, 0.0
    byte_offset = 0
    full_bytes = len(orig.encode("utf-8"))
    for w_start, w_end, w_text in words:
        w_bytes = len(str(w_text).encode("utf-8"))
        if pos <= w_start:
            return min(byte_offset, full_bytes), 0.0
        if pos < w_end:
            dur = max(0.01, w_end - w_start)
            frac = max(0.0, min(1.0, (pos - w_start) / dur))
            return min(byte_offset, full_bytes), frac
        byte_offset += w_bytes
    return full_bytes, 1.0


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _load_config() -> dict[str, Any]:
    try:
        if CONFIG_PATH.is_file():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_config(cfg: dict[str, Any]) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _run_gui(
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    offset_ms: int | None = None,
    locked: bool = False,
    sock_path: str | None = None,
) -> int:
    import cairo
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo

    cfg = _load_config()
    init_offset = int(cfg.get("offset_ms", 0)) if offset_ms is None else int(offset_ms)

    class DesktopLyricWindow(Gtk.Window):
        def __init__(self) -> None:
            super().__init__(type=Gtk.WindowType.TOPLEVEL)
            self.win_w = max(280, int(width))
            self.win_h = max(44, int(height))
            self.show_trans: bool = bool(cfg.get("show_trans", True))
            self.bg_mode: str = str(cfg.get("bg_mode", "capsule"))  # capsule | minimal
            self.bg_opacity: float = float(cfg.get("bg_opacity", 0.72))
            self.click_locked: bool = bool(locked)

            self.clock = SyncedClock(offset_ms=init_offset)
            self.connected: bool = False
            self.track: dict[str, Any] | None = None
            self.has_yrc: bool = False
            self.lines: list[dict[str, Any]] = []
            self.line_starts: list[float] = []

            self._hovered: bool = False
            self._dragging: bool = False
            self._drag_offset_x: float = 0.0
            self._drag_offset_y: float = 0.0
            self._toast_text: str = ""
            self._toast_until: float = 0.0
            self._tick_id: int = 0
            self._hidden_by_user: bool = False
            self._font_family = self._pick_font_family()
            self._layout_cache: dict[tuple[str, float, bool], tuple[Any, float, float]] = {}
            self._last_draw_sig: tuple[Any, ...] | None = None
            self._last_line_dynamic: bool = True

            self._setup_window_flags()
            self._da = Gtk.DrawingArea()
            self._da.set_size_request(self.win_w, self.win_h)
            self._da.add_events(
                Gdk.EventMask.BUTTON_PRESS_MASK
                | Gdk.EventMask.BUTTON_RELEASE_MASK
                | Gdk.EventMask.POINTER_MOTION_MASK
                | Gdk.EventMask.ENTER_NOTIFY_MASK
                | Gdk.EventMask.LEAVE_NOTIFY_MASK
                | Gdk.EventMask.SCROLL_MASK
            )
            self._da.connect("draw", self._on_draw)
            self._da.connect("button-press-event", self._on_button_press)
            self._da.connect("button-release-event", self._on_button_release)
            self._da.connect("motion-notify-event", self._on_motion)
            self._da.connect("enter-notify-event", self._on_enter)
            self._da.connect("leave-notify-event", self._on_leave)
            self._da.connect("scroll-event", self._on_scroll)
            self.connect("destroy", self._on_destroy)
            self.add(self._da)

            self._restore_position()
            self.show_all()
            if self.click_locked:
                self._apply_click_through(True)

            # 定期保活置顶（避免某些全屏应用切回时把 UTILITY 窗口压到底层）
            GLib.timeout_add(3000, self._keep_above_keepalive)

            self._ipc = LyricIpcClient(
                on_message=lambda m: GLib.idle_add(self._handle_ipc_message, m),
                on_connection_change=lambda c: GLib.idle_add(self._handle_conn_change, c),
                sock_path=sock_path or get_lyric_sock_path(),
            )
            self._ipc.start()
            self._ensure_tick_active()

        def _pick_font_family(self) -> str:
            ctx = self.get_pango_context()
            families = {f.get_name() for f in ctx.list_families()}
            for cand in (
                "Noto Sans CJK SC",
                "Source Han Sans SC",
                "Microsoft YaHei",
                "WenQuanYi Micro Hei",
                "PingFang SC",
                "Sans",
            ):
                if cand in families:
                    return cand
            return "Sans"

        def _setup_window_flags(self) -> None:
            self.set_title("ncm-lite 桌面歌词")
            self.set_default_size(self.win_w, self.win_h)
            self.set_resizable(False)
            self.set_decorated(False)
            self.set_keep_above(True)
            self.stick()
            self.set_skip_taskbar_hint(True)
            self.set_skip_pager_hint(True)
            self.set_accept_focus(False)
            self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
            self.set_app_paintable(True)
            scr = self.get_screen()
            if scr is not None:
                vis = scr.get_rgba_visual()
                if vis is not None:
                    self.set_visual(vis)

        def _monitor_workarea(self) -> tuple[int, int, int, int]:
            disp = Gdk.Display.get_default()
            mon = disp.get_primary_monitor() if disp else None
            if mon is None and disp and disp.get_n_monitors() > 0:
                mon = disp.get_monitor(0)
            if mon is not None:
                geo = mon.get_workarea()
                return geo.x, geo.y, geo.width, geo.height
            return 0, 0, 1920, 1080

        def _default_position(self) -> tuple[int, int]:
            mx, my, mw, mh = self._monitor_workarea()
            x = mx + (mw - self.win_w) // 2
            y = my + mh - self.win_h - 48
            return x, y

        def _restore_position(self) -> None:
            mx, my, mw, mh = self._monitor_workarea()
            dx, dy = self._default_position()
            x = int(cfg.get("x", dx))
            y = int(cfg.get("y", dy))
            if x < mx - self.win_w + 40 or x > mx + mw - 40:
                x = dx
            if y < my - self.win_h + 20 or y > my + mh - 20:
                y = dy
            self.move(x, y)

        def _persist_settings(self) -> None:
            x, y = self.get_position()
            cfg.update({
                "x": x,
                "y": y,
                "offset_ms": self.clock.offset_ms,
                "show_trans": self.show_trans,
                "bg_mode": self.bg_mode,
                "bg_opacity": self.bg_opacity,
            })
            _save_config(cfg)

        def _keep_above_keepalive(self) -> bool:
            if self.get_visible() and not self._hidden_by_user:
                self.set_keep_above(True)
            return True

        def _apply_click_through(self, locked: bool) -> None:
            self.click_locked = locked
            if locked:
                self.input_shape_combine_region(cairo.Region())
                self._show_toast("已锁定穿透（TUI 按 Shift+L 解锁）", 2.2)
            else:
                self.input_shape_combine_region(None)
                self._show_toast("已解锁鼠标交互", 1.6)
            self._ensure_tick_active()

        def _show_toast(self, text: str, secs: float = 1.4) -> None:
            self._toast_text = text
            self._toast_until = time.monotonic() + secs
            self._ensure_tick_active()

        def _ensure_tick_active(self) -> None:
            if self._tick_id == 0:
                self._tick_id = self._da.add_tick_callback(self._on_frame_tick)

        def _should_keep_ticking(self, now: float) -> bool:
            if not self.get_visible():
                return False
            if self.clock.is_running():
                return True
            if self._hovered or self._dragging:
                return True
            if now < self._toast_until:
                return True
            return False

        def _on_frame_tick(self, _widget: Any, _frame_clock: Any) -> bool:
            now = time.monotonic()
            pos = self.clock.position(now)
            orig, trans, _line_progress, words = self._resolve_display_state(pos)
            is_karaoke = bool(words) and self.clock.playing
            has_toast = (now < self._toast_until) and bool(self._toast_text)
            if is_karaoke or self._last_line_dynamic:
                dyn_bucket = int(pos * 60.0)
            else:
                dyn_bucket = 0
            sig = (
                orig,
                trans,
                self.show_trans,
                self.bg_mode,
                self._hovered,
                self._dragging,
                has_toast,
                self._toast_text if has_toast else "",
                self.clock.playing,
                self.clock.paused,
                dyn_bucket,
            )
            if sig != self._last_draw_sig:
                self._last_draw_sig = sig
                self._da.queue_draw()
            if not self._should_keep_ticking(now):
                self._tick_id = 0
                return False
            return True

        def _handle_conn_change(self, connected: bool) -> bool:
            self.connected = connected
            if not connected:
                self.clock.playing = False
                self.clock.paused = False
                self.clock.buffering = False
            self._da.queue_draw()
            self._ensure_tick_active()
            return False

        def _handle_ipc_message(self, msg: dict[str, Any]) -> bool:
            mtype = msg.get("type")
            if mtype == "track":
                self.track = msg.get("track")
                self.has_yrc = bool(msg.get("has_yrc", False))
                self.lines = list(msg.get("lines") or [])
                self.line_starts = [float(r["start"]) for r in self.lines]
            elif mtype == "state":
                self.clock.update_from_state(msg)
            elif mtype == "control":
                act = msg.get("action")
                if act == "toggle_visible":
                    if self.get_visible():
                        self._hidden_by_user = True
                        self.hide()
                    else:
                        self._hidden_by_user = False
                        self.show_all()
                        self.set_keep_above(True)
                        if self.click_locked:
                            self._apply_click_through(True)
                elif act == "show":
                    self._hidden_by_user = False
                    self.show_all()
                    self.set_keep_above(True)
                elif act == "hide":
                    self._hidden_by_user = True
                    self.hide()
                elif act == "toggle_lock":
                    self._apply_click_through(not self.click_locked)
                elif act == "unlock":
                    self._apply_click_through(False)
                elif act == "quit":
                    self._persist_settings()
                    Gtk.main_quit()
                    return False
            self._da.queue_draw()
            self._ensure_tick_active()
            return False

        def _on_enter(self, _w: Any, _ev: Any) -> bool:
            self._hovered = True
            self._ensure_tick_active()
            return False

        def _on_leave(self, _w: Any, _ev: Any) -> bool:
            self._hovered = False
            self._da.queue_draw()
            return False

        def _on_scroll(self, _w: Any, ev: Any) -> bool:
            step = 10 if (ev.state & Gdk.ModifierType.SHIFT_MASK) else 50
            if ev.direction == Gdk.ScrollDirection.UP:
                delta = step
            elif ev.direction == Gdk.ScrollDirection.DOWN:
                delta = -step
            else:
                return False
            self.clock.offset_ms = max(-5000, min(5000, self.clock.offset_ms + delta))
            self._persist_settings()
            sign = "+" if self.clock.offset_ms > 0 else ""
            self._show_toast(f"歌词偏移 {sign}{self.clock.offset_ms}ms", 1.3)
            self._da.queue_draw()
            return True

        def _on_button_press(self, _w: Any, ev: Any) -> bool:
            if ev.button == 1:
                if ev.type == Gdk.EventType._2BUTTON_PRESS:
                    self._ipc.send_command("toggle_pause")
                    return True
                self._dragging = True
                wx, wy = self.get_position()
                self._drag_offset_x = ev.x_root - wx
                self._drag_offset_y = ev.y_root - wy
                self._ensure_tick_active()
                return True
            if ev.button == 3:
                self._show_context_menu(ev)
                return True
            return False

        def _on_motion(self, _w: Any, ev: Any) -> bool:
            if self._dragging:
                nx = int(round(ev.x_root - self._drag_offset_x))
                ny = int(round(ev.y_root - self._drag_offset_y))
                self.move(nx, ny)
                return True
            return False

        def _on_button_release(self, _w: Any, ev: Any) -> bool:
            if ev.button == 1 and self._dragging:
                self._dragging = False
                self._persist_settings()
                return True
            return False

        def _show_context_menu(self, ev: Any) -> None:
            menu = Gtk.Menu()

            def add_item(label: str, cb: Any) -> None:
                it = Gtk.MenuItem(label=label)
                it.connect("activate", lambda *_: cb())
                menu.append(it)

            add_item("播放 / 暂停 (双击)", lambda: self._ipc.send_command("toggle_pause"))
            add_item("上一首", lambda: self._ipc.send_command("prev"))
            add_item("下一首", lambda: self._ipc.send_command("next"))
            menu.append(Gtk.SeparatorMenuItem())

            add_item(
                "隐藏译文" if self.show_trans else "显示译文",
                self._toggle_trans,
            )
            add_item(
                "切换极简无底模式" if self.bg_mode == "capsule" else "切换磨砂胶囊背景",
                self._toggle_bg_mode,
            )

            off_menu = Gtk.Menu()
            for lbl, d in (
                ("提前 100ms (-100)", -100),
                ("提前 20ms (-20)", -20),
                ("重置偏移 (0ms)", None),
                ("延后 20ms (+20)", 20),
                ("延后 100ms (+100)", 100),
            ):
                it = Gtk.MenuItem(label=lbl)
                it.connect("activate", lambda *_a, delta=d: self._adjust_offset(delta))
                off_menu.append(it)
            off_root = Gtk.MenuItem(label=f"时间对齐 ({self.clock.offset_ms:+d}ms)")
            off_root.set_submenu(off_menu)
            menu.append(off_root)

            menu.append(Gtk.SeparatorMenuItem())
            add_item("锁定鼠标穿透 (TUI按Shift+L解锁)", lambda: self._apply_click_through(True))
            add_item("还原居中位置", self._reset_position)
            add_item("关闭桌面歌词", Gtk.main_quit)

            menu.show_all()
            menu.popup_at_pointer(ev)

        def _toggle_trans(self) -> None:
            self.show_trans = not self.show_trans
            self._persist_settings()
            self._da.queue_draw()

        def _toggle_bg_mode(self) -> None:
            self.bg_mode = "minimal" if self.bg_mode == "capsule" else "capsule"
            self._persist_settings()
            self._da.queue_draw()

        def _adjust_offset(self, delta: int | None) -> None:
            if delta is None:
                self.clock.offset_ms = 0
            else:
                self.clock.offset_ms = max(-5000, min(5000, self.clock.offset_ms + delta))
            self._persist_settings()
            self._show_toast(f"歌词偏移 {self.clock.offset_ms:+d}ms", 1.3)
            self._da.queue_draw()

        def _reset_position(self) -> None:
            x, y = self._default_position()
            self.move(x, y)
            self._persist_settings()

        def _on_destroy(self, _w: Any) -> None:
            self._persist_settings()
            self._ipc.close()
            if self._tick_id:
                self._da.remove_tick_callback(self._tick_id)
                self._tick_id = 0
            Gtk.main_quit()

        # ── Cairo 高刷矢量绘制 ───────────────────────────────────────────────
        @staticmethod
        def _rounded_rect_path(cr: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
            r = min(r, w / 2.0, h / 2.0)
            cr.new_sub_path()
            cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
            cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
            cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
            cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
            cr.close_path()

        def _make_layout(self, cr: cairo.Context, text: str, size_pt: float, bold: bool = False) -> tuple[Pango.Layout, float, float]:
            key = (text, size_pt, bold)
            cached = self._layout_cache.get(key)
            if cached is not None:
                layout, w, h = cached
                PangoCairo.update_layout(cr, layout)
                return layout, w, h

            if len(self._layout_cache) >= 32:
                self._layout_cache.clear()
            layout = PangoCairo.create_layout(cr)
            desc = Pango.FontDescription()
            desc.set_family(self._font_family)
            desc.set_absolute_size(size_pt * Pango.SCALE)
            desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
            layout.set_font_description(desc)
            layout.set_text(text, -1)
            ink, logical = layout.get_pixel_extents()
            _ = ink
            res = (layout, float(logical.width), float(logical.height))
            self._layout_cache[key] = res
            return res

        def _draw_shadow_layout(
            self, cr: cairo.Context, layout: Pango.Layout, x: float, y: float, alpha: float
        ) -> None:
            for dx, dy, sa in ((0.0, 1.4, 0.55), (0.8, 0.8, 0.30)):
                cr.save()
                cr.move_to(x + dx, y + dy)
                cr.set_source_rgba(0.03, 0.04, 0.08, sa * alpha)
                PangoCairo.show_layout(cr, layout)
                cr.restore()

        def _draw_pastel_gradient_layout(
            self,
            cr: cairo.Context,
            layout: Pango.Layout,
            x: float,
            y: float,
            w: float,
            alpha: float,
        ) -> None:
            grad = cairo.LinearGradient(x, y, x + max(120.0, w), y)
            grad.add_color_stop_rgba(0.00, 0.561, 0.847, 0.761, alpha)  # #8fd8c2 薄荷
            grad.add_color_stop_rgba(0.35, 0.706, 0.655, 0.898, alpha)  # #b4a7e5 薰衣草
            grad.add_color_stop_rgba(0.70, 0.910, 0.620, 0.722, alpha)  # #e89eb8 樱粉
            grad.add_color_stop_rgba(1.00, 0.941, 0.718, 0.643, alpha)  # #f0b7a4 杏桃
            cr.save()
            cr.move_to(x, y)
            cr.set_source(grad)
            PangoCairo.show_layout(cr, layout)
            cr.restore()

        def _resolve_display_state(self, pos: float) -> tuple[str, str, float, list[list[Any]]]:
            """返回 (主文本, 译文, 行进度0..1, 逐字words)。"""
            if not self.connected:
                return "✦ 等待 ncm-lite 连接... ✦", "启动 ncm.py tui 即可实时同步歌词", 0.0, []

            if not self.track:
                return "✦ 静候下一段旋律 ✦", "ncm-lite 桌面歌词 · 拖拽可移动", 0.0, []

            title = str(self.track.get("name") or "")
            artists = str(self.track.get("artists") or "")
            song_banner = f"{title} — {artists}" if artists else title

            if not self.lines:
                return f"✦ {song_banner} ✦", "纯音乐 / 暂无歌词", 0.0, []

            idx = bisect.bisect_right(self.line_starts, pos) - 1
            if idx < 0:
                first = self.lines[0]
                wait = max(0.0, first["start"] - pos)
                return (
                    f"♪ {song_banner}" if wait > 1.5 else first["orig"],
                    "即将进入歌词..." if wait <= 1.5 else "前奏中...",
                    0.0,
                    [],
                )

            row = self.lines[idx]
            start = float(row["start"])
            end = float(row["end"])
            dur = max(0.2, end - start)
            line_progress = _clamp01((pos - start) / dur)
            return row["orig"], row.get("trans") or "", line_progress, row.get("words") or []

        def _on_draw(self, _widget: Any, cr: cairo.Context) -> bool:
            alloc = self._da.get_allocation()
            W = float(alloc.width or self.win_w)
            H = float(alloc.height or self.win_h)
            now = time.monotonic()
            pos = self.clock.position(now)

            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)

            # 1) 背景胶囊
            show_bg = self.bg_mode == "capsule" or self._hovered or self._dragging
            if show_bg:
                alpha = self.bg_opacity if self.bg_mode == "capsule" else 0.55
                if self._hovered or self._dragging:
                    alpha = min(0.90, alpha + 0.14)
                self._rounded_rect_path(cr, 1.0, 1.0, W - 2.0, H - 2.0, 14.0)
                cr.set_source_rgba(0.075, 0.082, 0.129, alpha)
                cr.fill_preserve()

                border_grad = cairo.LinearGradient(0, 0, W, H)
                b_alpha = 0.52 if (self._hovered or self._dragging) else 0.22
                border_grad.add_color_stop_rgba(0.0, 0.561, 0.847, 0.761, b_alpha)
                border_grad.add_color_stop_rgba(0.5, 0.706, 0.655, 0.898, b_alpha)
                border_grad.add_color_stop_rgba(1.0, 0.910, 0.620, 0.722, b_alpha)
                cr.set_line_width(1.25 if self._hovered else 1.0)
                cr.set_source(border_grad)
                cr.stroke()

            # 2) 解析当前歌词内容（直接切换，无淡入缓动）
            orig, trans, line_progress, words = self._resolve_display_state(pos)
            has_trans = self.show_trans and bool(trans)

            pad_x = 18.0
            avail_w = max(80.0, W - pad_x * 2.0)

            main_pt = 17.0 if has_trans else 20.5
            trans_pt = 11.8

            main_layout, main_w, main_h = self._make_layout(cr, orig, main_pt, bold=True)

            # 3) 计算逐字卡拉OK像素填充位置（亚像素精度）
            fill_px = 0.0
            is_karaoke = bool(words) and self.clock.playing
            if is_karaoke:
                byte_idx, w_frac = compute_word_fill_ratio(words, orig, pos)
                full_bytes = len(orig.encode("utf-8"))
                if byte_idx >= full_bytes:
                    fill_px = main_w
                else:
                    rect = main_layout.index_to_pos(byte_idx)
                    char_x = rect.x / Pango.SCALE
                    char_w = max(0.0, rect.width / Pango.SCALE)
                    fill_px = max(0.0, min(main_w, char_x + char_w * w_frac))

            # 4) 水平定位（居中或超长文本智能平滑滚动）
            if main_w <= avail_w:
                main_x = (W - main_w) / 2.0
            else:
                overflow = main_w - avail_w
                if is_karaoke:
                    pan = max(0.0, min(overflow, fill_px - avail_w * 0.45))
                else:
                    pan_t = _clamp01((line_progress - 0.12) / 0.76)
                    pan = overflow * pan_t
                main_x = pad_x - pan

            # 垂直排版（适配 64px 高度）
            if has_trans:
                trans_layout, trans_w, trans_h = self._make_layout(cr, trans, trans_pt, bold=False)
                gap = 3.0
                total_h = main_h + gap + trans_h
                main_y = max(4.0, (H - total_h) / 2.0)
                trans_y = main_y + main_h + gap
            else:
                trans_layout, trans_w, trans_h = None, 0.0, 0.0
                main_y = (H - main_h) / 2.0
                trans_y = 0.0

            self._last_line_dynamic = (
                is_karaoke or (main_w > avail_w) or (has_trans and trans_w > avail_w)
            )

            # 裁剪在胶囊内容区内绘制文字，避免长句滚动溢出圆角外
            cr.save()
            self._rounded_rect_path(cr, pad_x - 4.0, 3.0, avail_w + 8.0, H - 6.0, 8.0)
            cr.clip()

            if is_karaoke:
                # 先画未唱部分底色（柔和月光银）
                self._draw_shadow_layout(cr, main_layout, main_x, main_y, 1.0)
                cr.save()
                cr.move_to(main_x, main_y)
                cr.set_source_rgba(0.78, 0.82, 0.92, 0.72)
                PangoCairo.show_layout(cr, main_layout)
                cr.restore()

                # 再按精确亚像素宽度裁剪出已唱部分的粉彩渐变扫光
                if fill_px > 0.5:
                    cr.save()
                    cr.rectangle(main_x - 2.0, 0.0, fill_px + 2.0, H)
                    cr.clip()
                    self._draw_pastel_gradient_layout(cr, main_layout, main_x, main_y, main_w, 1.0)
                    cr.restore()
            else:
                self._draw_shadow_layout(cr, main_layout, main_x, main_y, 1.0)
                if self.clock.playing and not self.clock.paused:
                    self._draw_pastel_gradient_layout(cr, main_layout, main_x, main_y, main_w, 1.0)
                else:
                    cr.save()
                    cr.move_to(main_x, main_y)
                    cr.set_source_rgba(0.867, 0.886, 0.949, 0.88)
                    PangoCairo.show_layout(cr, main_layout)
                    cr.restore()

            if has_trans and trans_layout is not None:
                if trans_w <= avail_w:
                    trans_x = (W - trans_w) / 2.0
                else:
                    t_over = trans_w - avail_w
                    trans_x = pad_x - t_over * _clamp01((line_progress - 0.12) / 0.76)
                self._draw_shadow_layout(cr, trans_layout, trans_x, trans_y, 0.85)
                cr.save()
                cr.move_to(trans_x, trans_y)
                cr.set_source_rgba(0.761, 0.714, 0.910, 0.92)
                PangoCairo.show_layout(cr, trans_layout)
                cr.restore()

            cr.restore()

            # 5) 悬浮或滚轮调节提示角标
            badge = ""
            if now < self._toast_until and self._toast_text:
                badge = self._toast_text
            elif self._hovered:
                badge = f"拖动 · 滚轮调偏移({self.clock.offset_ms:+d}ms) · 右键菜单"
            if badge:
                b_layout, bw, bh = self._make_layout(cr, badge, 9.5, bold=False)
                bx = W - bw - 10.0
                by = 4.0
                self._rounded_rect_path(cr, bx - 5.0, by - 1.0, bw + 10.0, bh + 2.0, 5.0)
                cr.set_source_rgba(0.06, 0.07, 0.11, 0.82)
                cr.fill()
                cr.move_to(bx, by)
                cr.set_source_rgba(0.922, 0.827, 0.655, 0.95)
                PangoCairo.show_layout(cr, b_layout)

            return True

    DesktopLyricWindow()
    Gtk.main()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ncm-lite 常驻顶层桌面歌词窗口")
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    ap.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    ap.add_argument("--offset-ms", type=int, default=None)
    ap.add_argument("--lock", action="store_true", help="开启鼠标点击穿透")
    ap.add_argument("--sock", default=None, help="自定义 IPC socket 路径")
    args = ap.parse_args()
    return int(
        launch_desktop_lyrics(
            width=args.width,
            height=args.height,
            offset_ms=args.offset_ms,
            locked=args.lock,
            foreground=True,
            sock_path=args.sock,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
