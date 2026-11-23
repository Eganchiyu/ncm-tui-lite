"""GTK3 GtkGLArea 全屏透明叠加窗口、进程启动器与 CLI 入口。"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from lyric_ipc import LyricIpcClient, get_lyric_sock_path

from .audio_analyzer import AudioBeatAnalyzer, LookaheadPeakTimeline, SyncedClock
from .big_lyrics import BigLyricsRenderer
from .shaders import (
    FRAG_SHADER,
    GL_ARRAY_BUFFER,
    GL_BGRA,
    GL_BLEND,
    GL_CLAMP_TO_EDGE,
    GL_COLOR_BUFFER_BIT,
    GL_FALSE,
    GL_FLOAT,
    GL_LINEAR,
    GL_LINEAR_MIPMAP_LINEAR,
    GL_ONE,
    GL_ONE_MINUS_SRC_ALPHA,
    GL_R8,
    GL_RED,
    GL_RGBA8,
    GL_STREAM_DRAW,
    GL_TEXTURE0,
    GL_TEXTURE1,
    GL_TEXTURE_2D,
    GL_TEXTURE_MAG_FILTER,
    GL_TEXTURE_MIN_FILTER,
    GL_TEXTURE_WRAP_S,
    GL_TEXTURE_WRAP_T,
    GL_TRIANGLES,
    GL_UNPACK_ALIGNMENT,
    GL_UNSIGNED_BYTE,
    LYRIC_FRAG_SHADER,
    LYRIC_VERT_SHADER,
    VERT_SHADER,
    _GLBindings,
)
from .x11_utils import X11GlobalHotkey, X11ScreenGrabber

CONFIG_PATH = Path.home() / ".config" / "ncm-lite" / "shader_overlay.json"
_ENTRYPOINT_SCRIPT = str(Path(__file__).resolve().parent.parent / "screen_shader.py")


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


def _load_config() -> dict[str, Any]:
    try:
        if CONFIG_PATH.is_file():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_config(updates: dict[str, Any]) -> None:
    try:
        cfg = _load_config()
        cfg.update(updates)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def launch_screen_shader(
    *,
    active: bool = False,
    big_lyrics: bool | None = None,
    hotkey: str = "<Ctrl><Alt>v",
    lyrics_hotkey: str = "<Ctrl><Alt>l",
    lookahead_ms: int = 75,
    foreground: bool = True,
    sock_path: str | None = None,
) -> int | subprocess.Popen:
    """启动全屏流质 Shader 叠加进程（支持从 venv 自动切换至系统带 GTK 的 Python）。"""
    py = find_gtk_python()
    if not py:
        msg = "未找到可用的 PyGObject (GTK 3.0) 环境，请确认已安装 python3-gi / python3-gi-cairo。"
        if foreground:
            print(msg, file=sys.stderr)
            return 1
        raise RuntimeError(msg)

    cmd = [
        py,
        _ENTRYPOINT_SCRIPT,
        "--hotkey",
        hotkey,
        "--lyrics-hotkey",
        lyrics_hotkey,
        "--lookahead-ms",
        str(int(lookahead_ms)),
    ]
    if active:
        cmd.append("--active")
    if big_lyrics is True:
        cmd.append("--big-lyrics")
    elif big_lyrics is False:
        cmd.append("--no-big-lyrics")
    if sock_path:
        cmd.extend(["--sock", sock_path])

    if foreground:
        if os.path.abspath(py) == os.path.abspath(sys.executable):
            return _run_gui(
                active=active,
                big_lyrics=big_lyrics,
                hotkey=hotkey,
                lyrics_hotkey=lyrics_hotkey,
                lookahead_ms=lookahead_ms,
                sock_path=sock_path,
            )
        return subprocess.call(cmd)

    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _run_gui(
    *,
    active: bool = False,
    big_lyrics: bool | None = None,
    hotkey: str = "<Ctrl><Alt>v",
    lyrics_hotkey: str = "<Ctrl><Alt>l",
    lookahead_ms: int = 75,
    sock_path: str | None = None,
) -> int:
    import cairo
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Gdk, GLib, Gtk

    cfg = _load_config()
    init_big_lyrics = bool(cfg.get("big_lyrics", False)) if big_lyrics is None else bool(big_lyrics)
    lookahead_sec = max(0.0, min(0.4, float(lookahead_ms) / 1000.0))

    def smoothstep_py(edge0: float, edge1: float, x: float) -> float:
        t = max(0.0, min(1.0, (x - edge0) / max(1e-6, edge1 - edge0)))
        return t * t * (3.0 - 2.0 * t)

    class ScreenShaderWindow(Gtk.Window):
        def __init__(self) -> None:
            super().__init__(type=Gtk.WindowType.TOPLEVEL)
            self._active: bool = bool(active)
            self._big_lyrics_enabled: bool = init_big_lyrics
            self._lyric_alpha: float = 0.0
            self._intro: float = 0.0
            self._intro_active: float = 0.0
            self._intro_phase: str = "in" if self._active else "idle"
            self._intro_timer: float = 0.0
            self._seed: float = random.uniform(0.0, 100.0)
            self._start_mono: float = time.monotonic()
            self._last_frame_mono: float = self._start_mono
            self._last_render_mono: float = self._start_mono

            self._gl: _GLBindings | None = None
            self._prog: int = 0
            self._vao: int = 0
            self._lyric_prog: int = 0
            self._lyric_vao: int = 0
            self._lyric_vbo: int = 0
            self._tex: int = 0
            self._tex_w: int = 0
            self._tex_h: int = 0
            self._lyric_tex: int = 0
            self._lyric_tex_init: bool = False
            self._has_screen: bool = False
            self._uniforms: dict[str, int] = {}
            self._lyric_uniforms: dict[str, int] = {}
            self._tick_id: int = 0
            self._wakeup_timer_id: int = 0
            self._silent_frames: int = 0

            self._clock = SyncedClock()
            self._lookahead = LookaheadPeakTimeline()
            self._last_lookahead_mono: float = 0.0
            self._audio = AudioBeatAnalyzer()
            self._audio.start(capture_enabled=bool(self._active))

            self._grabber = X11ScreenGrabber()
            self._grabber.start()

            self._setup_window()
            self._lyrics_renderer = BigLyricsRenderer(self.get_pango_context())

            self._gl_area = Gtk.GLArea()
            self._gl_area.set_has_alpha(True)
            self._gl_area.set_required_version(3, 3)
            self._gl_area.set_auto_render(True)
            self._gl_area.connect("realize", self._on_gl_realize)
            self._gl_area.connect("unrealize", self._on_gl_unrealize)
            self._gl_area.connect("render", self._on_gl_render)
            self.add(self._gl_area)

            self.connect("destroy", self._on_destroy)
            self.connect("realize", lambda *_: self._sync_grabber_config())
            self.connect("map-event", lambda *_: self._on_mapped())
            self.connect("configure-event", lambda *_: self._sync_grabber_config() or False)

            hotkey_bindings: list[tuple[str, Callable[[], None]]] = [
                (hotkey, lambda: GLib.idle_add(self.toggle_active)),
            ]
            if lyrics_hotkey:
                hotkey_bindings.append(
                    (lyrics_hotkey, lambda: GLib.idle_add(self.toggle_big_lyrics))
                )
            self._hotkey = X11GlobalHotkey(bindings=hotkey_bindings)
            self._hotkey.start()

            self._ipc = LyricIpcClient(
                on_message=lambda m: GLib.idle_add(self._handle_ipc, m),
                sock_path=sock_path or get_lyric_sock_path(),
            )
            self._ipc.start()

            if self._active:
                self._start_intro()
            else:
                self.hide()

        def _setup_window(self) -> None:
            self.set_title("ncm-lite-shader")
            self.set_decorated(False)
            self.set_app_paintable(True)
            self.set_skip_taskbar_hint(True)
            self.set_skip_pager_hint(True)
            self.set_accept_focus(False)
            self.set_focus_on_map(False)
            self.set_keep_above(True)
            self.set_type_hint(Gdk.WindowTypeHint.UTILITY)

            screen = self.get_screen()
            visual = screen.get_rgba_visual()
            if visual is not None:
                self.set_visual(visual)

            display = Gdk.Display.get_default()
            mon = display.get_primary_monitor() if display else None
            if mon is not None:
                geom = mon.get_geometry()
                self.move(geom.x, geom.y)
                self.set_default_size(geom.width, geom.height)
                self.resize(geom.width, geom.height)
            else:
                self.fullscreen()
            self.stick()

        def _sync_grabber_config(self) -> None:
            gdk_win = self.get_window()
            if gdk_win is not None and hasattr(gdk_win, "get_xid"):
                xid = gdk_win.get_xid()
                _ok, ox, oy = gdk_win.get_origin()
                scale = gdk_win.get_scale_factor() or 1
                w = max(1, gdk_win.get_width() * scale)
                h = max(1, gdk_win.get_height() * scale)
                self._grabber.configure(xid, ox * scale, oy * scale, w, h)

        def _on_mapped(self) -> bool:
            self._sync_grabber_config()
            return self._apply_click_through()

        def _apply_click_through(self) -> bool:
            gdk_win = self.get_window()
            if gdk_win is not None:
                gdk_win.input_shape_combine_region(cairo.Region(), 0, 0)
                gdk_win.set_pass_through(True)
            return False

        def _start_intro(self) -> None:
            self._active = True
            self._intro = 0.0
            self._intro_active = 1.0
            self._intro_phase = "in"
            self._intro_timer = 0.0
            self._seed = random.uniform(0.0, 100.0)
            self._last_frame_mono = time.monotonic()

            self.show_all()
            self.set_keep_above(True)
            self._sync_grabber_config()
            self._apply_click_through()
            self._grabber.set_enabled(True)
            self._ensure_tick_active()

        def _start_outro(self) -> None:
            self._active = False
            self._intro = 1.0
            self._intro_active = 1.0
            self._intro_phase = "out"
            self._intro_timer = 0.0
            self._ensure_tick_active()

        def toggle_active(self) -> None:
            if self._active:
                self._start_outro()
            else:
                self._start_intro()

        def set_big_lyrics(self, enabled: bool) -> None:
            self._big_lyrics_enabled = bool(enabled)
            _save_config({"big_lyrics": self._big_lyrics_enabled})
            if self._big_lyrics_enabled and not self._active:
                self._start_intro()
            elif self._active:
                self._ensure_tick_active()

        def toggle_big_lyrics(self) -> None:
            self.set_big_lyrics(not self._big_lyrics_enabled)

        def _handle_ipc(self, msg: dict[str, Any]) -> None:
            mtype = msg.get("type")
            if mtype == "track":
                self._lyrics_renderer.apply_track(msg)
                if self._active:
                    self._ensure_tick_active()
                return
            if mtype == "state":
                self._clock.update_from_state(msg)
                if self._active and self._clock.is_running():
                    self._ensure_tick_active()
                return
            if mtype == "peaks":
                self._lookahead.apply_ipc(msg)
                return
            if mtype != "control":
                return
            action = msg.get("action")
            if action == "toggle_shader":
                self.toggle_active()
            elif action == "shader_on" and not self._active:
                self._start_intro()
            elif action == "shader_off" and self._active:
                self._start_outro()
            elif action == "toggle_big_lyrics":
                self.toggle_big_lyrics()
            elif action == "big_lyrics_on":
                self.set_big_lyrics(True)
            elif action == "big_lyrics_off":
                self.set_big_lyrics(False)
            elif action == "quit":
                Gtk.main_quit()

        def _ensure_tick_active(self) -> None:
            self._silent_frames = 0
            if self._wakeup_timer_id:
                GLib.source_remove(self._wakeup_timer_id)
                self._wakeup_timer_id = 0
            if self._tick_id:
                return
            self._last_frame_mono = time.monotonic()
            self._tick_id = self._gl_area.add_tick_callback(self._on_tick)

        def _stop_tick(self) -> None:
            if self._tick_id:
                self._gl_area.remove_tick_callback(self._tick_id)
                self._tick_id = 0

        def _stop_wakeup_timer(self) -> None:
            if self._wakeup_timer_id:
                GLib.source_remove(self._wakeup_timer_id)
                self._wakeup_timer_id = 0

        def _poll_audio_wakeup(self) -> bool:
            """独立 PipeWire 监听模式下低频检测系统出声以恢复 VSync 渲染。"""
            if not self._active or self._tick_id:
                self._wakeup_timer_id = 0
                return False
            now = time.monotonic()
            if self._clock.is_running() or (now - self._audio._last_audio_at) < 0.85:
                self._wakeup_timer_id = 0
                self._ensure_tick_active()
                return False
            return True

        def _release_lyric_resources(self) -> None:
            """释放大歌词 Cairo CPU 表面与 OpenGL 图集显存。"""
            self._lyrics_renderer.release_resources()
            if self._gl and self._lyric_tex:
                self._gl_area.make_current()
                t = ctypes.c_uint(self._lyric_tex)
                self._gl.glDeleteTextures(1, ctypes.byref(t))
                self._lyric_tex = 0
                self._lyric_tex_init = False

        def _on_tick(self, _widget: Any, _frame_clock: Any) -> bool:
            now = time.monotonic()
            dt = max(0.001, min(0.05, now - self._last_frame_mono))
            self._last_frame_mono = now

            lookahead_sample = None
            if self._clock.is_running():
                pos = self._clock.position(now)
                lookahead_sample = self._lookahead.sample_aligned(pos, lookahead_sec)
                if lookahead_sample is not None:
                    self._last_lookahead_mono = now

            using_lookahead = (lookahead_sample is not None) or (
                self._clock.is_running() and (now - self._last_lookahead_mono) < 1.5
            )
            need_rt_capture = (self._active or self._intro_phase in ("in", "out")) and (
                not using_lookahead
            )
            self._audio.set_capture_enabled(need_rt_capture)
            self._audio.step(dt, now, lookahead=lookahead_sample)

            target_la = 1.0 if (self._big_lyrics_enabled and self._lyrics_renderer.has_content) else 0.0
            rate = 7.5 if target_la > self._lyric_alpha else 5.5
            self._lyric_alpha += (target_la - self._lyric_alpha) * min(1.0, dt * rate)
            if self._lyric_alpha < 0.002 and target_la == 0.0:
                self._lyric_alpha = 0.0

            if self._intro_phase == "in":
                self._intro_timer += dt
                t = min(1.0, self._intro_timer / 1.60)
                self._intro = 1.0 - pow(1.0 - t, 3.2)
                self._intro_active = 1.0 - smoothstep_py(0.78, 1.0, t)
                if t >= 1.0:
                    self._intro_phase = "hold"
                    self._intro_active = 0.0
            elif self._intro_phase == "out":
                self._intro_timer += dt
                t = min(1.0, self._intro_timer / 1.05)
                self._intro = 1.0 - pow(t, 2.2)
                self._intro_active = 1.0 - smoothstep_py(0.72, 1.0, t)
                if t >= 1.0:
                    self._intro_phase = "idle"
                    self._intro_active = 0.0
                    self._grabber.set_enabled(False)
                    self._audio.set_capture_enabled(False)
                    self._stop_wakeup_timer()
                    self._release_lyric_resources()
                    self.hide()
                    self._stop_tick()
                    return False

            if not self._big_lyrics_enabled and self._lyric_alpha == 0.0 and (
                self._lyric_tex or self._lyrics_renderer.atlas.surface is not None
            ):
                self._release_lyric_resources()

            need_grab = (
                (self._intro_phase in ("in", "out"))
                or (self._audio.presence > 0.015)
                or (self._big_lyrics_enabled and self._lyrics_renderer.has_content)
                or (self._lyric_alpha > 0.005)
            )
            self._grabber.set_enabled(need_grab)

            is_silent_hold = (
                self._intro_phase == "hold"
                and not self._clock.is_running()
                and self._audio.presence < 0.002
                and self._lyric_alpha == 0.0
                and not (self._big_lyrics_enabled and self._lyrics_renderer.has_content)
            )
            if is_silent_hold:
                self._silent_frames += 1
                self._gl_area.queue_render()
                if self._silent_frames >= 2:
                    self._tick_id = 0
                    if need_rt_capture and not self._wakeup_timer_id:
                        self._wakeup_timer_id = GLib.timeout_add(80, self._poll_audio_wakeup)
                    return False
                return True

            self._silent_frames = 0
            self._gl_area.queue_render()
            return True

        def _on_gl_realize(self, area: Any) -> None:
            area.make_current()
            err = area.get_error()
            if err is not None:
                print(f"GLArea 初始化失败: {err}", file=sys.stderr)
                return
            try:
                gl = _GLBindings()
                prog = gl.compile_program(VERT_SHADER, FRAG_SHADER)
                lyric_prog = gl.compile_program(LYRIC_VERT_SHADER, LYRIC_FRAG_SHADER)

                vaos = (ctypes.c_uint * 2)()
                gl.glGenVertexArrays(2, vaos)
                vbo = ctypes.c_uint(0)
                gl.glGenBuffers(1, ctypes.byref(vbo))

                # 配置 Pass 2 全屏动态排印歌词 VAO/VBO（16 floats/vertex = 64 bytes stride）
                stride = BigLyricsRenderer.FLOATS_PER_VERTEX * 4
                gl.glBindVertexArray(vaos[1])
                gl.glBindBuffer(GL_ARRAY_BUFFER, vbo.value)
                for loc, comps, offset in (
                    (0, 2, 0),   # a_pos
                    (1, 2, 8),   # a_uv
                    (2, 4, 16),  # a_cell_uv
                    (3, 4, 32),  # a_state
                    (4, 4, 48),  # a_meta
                ):
                    gl.glEnableVertexAttribArray(loc)
                    gl.glVertexAttribPointer(
                        loc, comps, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(offset)
                    )
                gl.glBindBuffer(GL_ARRAY_BUFFER, 0)
                gl.glBindVertexArray(0)

                gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
                tex = ctypes.c_uint(0)
                gl.glGenTextures(1, ctypes.byref(tex))
                # 屏幕抓取纹理（大歌词单通道 GL_R8 图集按需延迟创建）
                gl.glBindTexture(GL_TEXTURE_2D, tex.value)
                gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
                gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
                gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
                gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
                gl.glBindTexture(GL_TEXTURE_2D, 0)

                self._gl = gl
                self._prog = prog
                self._vao = vaos[0]
                self._lyric_prog = lyric_prog
                self._lyric_vao = vaos[1]
                self._lyric_vbo = vbo.value
                self._tex = tex.value
                self._lyric_tex = 0
                self._lyric_tex_init = False
                self._bands_buf = (ctypes.c_float * 8)()
                for uname in (
                    "u_screen",
                    "u_has_screen",
                    "u_res",
                    "u_time",
                    "u_flow_time",
                    "u_flow_vel",
                    "u_presence",
                    "u_energy",
                    "u_sub",
                    "u_sub_sustain",
                    "u_sub_phase",
                    "u_bass",
                    "u_mid",
                    "u_high",
                    "u_beat",
                    "u_heavy_beat",
                    "u_beat_kick",
                    "u_beat_rand",
                    "u_beat_dir",
                    "u_beat_focus",
                    "u_edge_seed",
                    "u_sparkle",
                    "u_centroid",
                    "u_bands",
                    "u_intro",
                    "u_intro_active",
                    "u_seed",
                ):
                    self._uniforms[uname] = gl.glGetUniformLocation(prog, uname.encode("ascii"))

                for uname in (
                    "u_atlas",
                    "u_screen",
                    "u_has_screen",
                    "u_res",
                    "u_atlas_size",
                    "u_time",
                    "u_flow_time",
                    "u_presence",
                    "u_sub",
                    "u_sub_sustain",
                    "u_beat_kick",
                    "u_beat_rand",
                    "u_beat_dir",
                    "u_high",
                    "u_sparkle",
                    "u_centroid",
                    "u_intro",
                    "u_intro_active",
                    "u_lyric_alpha",
                ):
                    self._lyric_uniforms[uname] = gl.glGetUniformLocation(
                        lyric_prog, uname.encode("ascii")
                    )
            except Exception as e:  # noqa: BLE001
                print(f"Shader 加载异常: {e}", file=sys.stderr)

        def _on_gl_unrealize(self, area: Any) -> None:
            if not self._gl:
                return
            area.make_current()
            for attr in ("_prog", "_lyric_prog"):
                pid = getattr(self, attr, 0)
                if pid:
                    self._gl.glDeleteProgram(pid)
                    setattr(self, attr, 0)
            for attr in ("_tex", "_lyric_tex"):
                tid = getattr(self, attr, 0)
                if tid:
                    t = ctypes.c_uint(tid)
                    self._gl.glDeleteTextures(1, ctypes.byref(t))
                    setattr(self, attr, 0)
            for attr in ("_vao", "_lyric_vao"):
                vid = getattr(self, attr, 0)
                if vid:
                    v = ctypes.c_uint(vid)
                    self._gl.glDeleteVertexArrays(1, ctypes.byref(v))
                    setattr(self, attr, 0)
            if self._lyric_vbo:
                vb = ctypes.c_uint(self._lyric_vbo)
                self._gl.glDeleteBuffers(1, ctypes.byref(vb))
                self._lyric_vbo = 0

        def _on_gl_render(self, area: Any, _ctx: Any) -> bool:
            gl = self._gl
            if gl is None or not self._prog:
                return False

            alloc = area.get_allocation()
            scale = area.get_scale_factor() or 1
            w = max(1, alloc.width * scale)
            h = max(1, alloc.height * scale)

            frame = self._grabber.consume_frame()
            if frame is not None and self._tex:
                fw, fh, fbuf = frame
                fptr = ctypes.byref(fbuf)
                gl.glActiveTexture(GL_TEXTURE0)
                gl.glBindTexture(GL_TEXTURE_2D, self._tex)
                if fw != self._tex_w or fh != self._tex_h:
                    gl.glTexImage2D(
                        GL_TEXTURE_2D, 0, GL_RGBA8, fw, fh, 0, GL_BGRA, GL_UNSIGNED_BYTE, fptr
                    )
                    self._tex_w, self._tex_h = fw, fh
                else:
                    gl.glTexSubImage2D(
                        GL_TEXTURE_2D, 0, 0, 0, fw, fh, GL_BGRA, GL_UNSIGNED_BYTE, fptr
                    )
                self._has_screen = True

            now = time.monotonic()
            render_dt = max(0.001, min(0.05, now - self._last_render_mono))
            self._last_render_mono = now

            lyric_vbuf = None
            lyric_vcount = 0
            if self._big_lyrics_enabled or self._lyric_alpha > 0.002:
                pos = self._clock.position(now)
                upload, lyric_vbuf, lyric_vcount = self._lyrics_renderer.update(
                    pos, self._clock.is_running(), render_dt, now, w, h, self._audio
                )
                if upload is not None:
                    is_full, uy0, uh, uptr, _keepalive = upload
                    aw = self._lyrics_renderer.atlas.ATLAS_W
                    ah = self._lyrics_renderer.atlas.ATLAS_H
                    if not self._lyric_tex:
                        lt = ctypes.c_uint(0)
                        gl.glGenTextures(1, ctypes.byref(lt))
                        self._lyric_tex = lt.value
                        self._lyric_tex_init = False
                        gl.glActiveTexture(GL_TEXTURE1)
                        gl.glBindTexture(GL_TEXTURE_2D, self._lyric_tex)
                        gl.glTexParameteri(
                            GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR
                        )
                        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
                        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
                        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
                    else:
                        gl.glActiveTexture(GL_TEXTURE1)
                        gl.glBindTexture(GL_TEXTURE_2D, self._lyric_tex)
                    if not self._lyric_tex_init or is_full:
                        gl.glTexImage2D(
                            GL_TEXTURE_2D,
                            0,
                            GL_R8,
                            aw,
                            ah,
                            0,
                            GL_RED,
                            GL_UNSIGNED_BYTE,
                            ctypes.c_void_p(uptr),
                        )
                        self._lyric_tex_init = True
                    else:
                        gl.glTexSubImage2D(
                            GL_TEXTURE_2D,
                            0,
                            0,
                            uy0,
                            aw,
                            uh,
                            GL_RED,
                            GL_UNSIGNED_BYTE,
                            ctypes.c_void_p(uptr),
                        )
                    gl.glGenerateMipmap(GL_TEXTURE_2D)

            gl.glViewport(0, 0, w, h)
            gl.glClearColor(0.0, 0.0, 0.0, 0.0)
            gl.glClear(GL_COLOR_BUFFER_BIT)
            gl.glDisable(GL_BLEND)

            # ── Pass 1: 现有全屏流质 Shader（边缘折射 + 屏幕中央 Early-Out） ──
            gl.glUseProgram(self._prog)
            u = self._uniforms
            a = self._audio

            gl.glActiveTexture(GL_TEXTURE0)
            gl.glBindTexture(GL_TEXTURE_2D, self._tex)
            gl.glUniform1i(u["u_screen"], 0)
            gl.glUniform1f(u["u_has_screen"], 1.0 if self._has_screen else 0.0)

            gl.glUniform2f(u["u_res"], float(w), float(h))
            gl.glUniform1f(u["u_time"], float(now - self._start_mono))
            gl.glUniform1f(u["u_flow_time"], float(a.flow_time))
            gl.glUniform2f(u["u_flow_vel"], float(a.flow_vel_x), float(a.flow_vel_y))
            gl.glUniform1f(u["u_presence"], float(a.presence))
            gl.glUniform1f(u["u_energy"], float(a.energy))
            gl.glUniform1f(u["u_sub"], float(a.sub))
            gl.glUniform1f(u["u_sub_sustain"], float(a.sub_sustain))
            gl.glUniform1f(u["u_sub_phase"], float(a.sub_phase))
            gl.glUniform1f(u["u_bass"], float(a.bass))
            gl.glUniform1f(u["u_mid"], float(a.mid))
            gl.glUniform1f(u["u_high"], float(a.high))
            gl.glUniform1f(u["u_beat"], float(a.beat))
            gl.glUniform1f(u["u_heavy_beat"], float(a.heavy_beat))
            gl.glUniform1f(u["u_beat_kick"], float(a.beat_kick))
            gl.glUniform1f(u["u_beat_rand"], float(a.beat_rand_amp))
            gl.glUniform2f(u["u_beat_dir"], float(a.beat_dir_x), float(a.beat_dir_y))
            gl.glUniform2f(u["u_beat_focus"], float(a.beat_focus_x), float(a.beat_focus_y))
            gl.glUniform1f(u["u_edge_seed"], float(a.edge_seed))
            gl.glUniform1f(u["u_sparkle"], float(a.sparkle))
            gl.glUniform1f(u["u_centroid"], float(a.centroid))

            for i in range(8):
                self._bands_buf[i] = float(a.bands[i])
            gl.glUniform1fv(u["u_bands"], 8, self._bands_buf)

            gl.glUniform1f(u["u_intro"], float(self._intro))
            gl.glUniform1f(u["u_intro_active"], float(self._intro_active))
            gl.glUniform1f(u["u_seed"], float(self._seed))

            gl.glBindVertexArray(self._vao)
            gl.glDrawArrays(GL_TRIANGLES, 0, 3)
            gl.glBindVertexArray(0)

            # ── Pass 2: 全屏流体巨型动态排印歌词层 (Premultiplied Alpha) ──────
            if (
                self._lyric_prog
                and self._lyric_tex_init
                and lyric_vbuf is not None
                and lyric_vcount > 0
                and self._lyric_alpha > 0.002
            ):
                gl.glEnable(GL_BLEND)
                gl.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
                gl.glUseProgram(self._lyric_prog)
                lu = self._lyric_uniforms

                gl.glActiveTexture(GL_TEXTURE0)
                gl.glBindTexture(GL_TEXTURE_2D, self._tex)
                gl.glUniform1i(lu["u_screen"], 0)
                gl.glUniform1f(lu["u_has_screen"], 1.0 if self._has_screen else 0.0)

                gl.glActiveTexture(GL_TEXTURE1)
                gl.glBindTexture(GL_TEXTURE_2D, self._lyric_tex)
                gl.glUniform1i(lu["u_atlas"], 1)

                gl.glUniform2f(lu["u_res"], float(w), float(h))
                gl.glUniform2f(
                    lu["u_atlas_size"],
                    float(self._lyrics_renderer.atlas.ATLAS_W),
                    float(self._lyrics_renderer.atlas.ATLAS_H),
                )
                gl.glUniform1f(lu["u_time"], float(now - self._start_mono))
                gl.glUniform1f(lu["u_flow_time"], float(a.flow_time))
                gl.glUniform1f(lu["u_presence"], float(a.presence))
                gl.glUniform1f(lu["u_sub"], float(a.sub))
                gl.glUniform1f(lu["u_sub_sustain"], float(a.sub_sustain))
                gl.glUniform1f(lu["u_beat_kick"], float(a.beat_kick))
                gl.glUniform1f(lu["u_beat_rand"], float(a.beat_rand_amp))
                gl.glUniform2f(lu["u_beat_dir"], float(a.beat_dir_x), float(a.beat_dir_y))
                gl.glUniform1f(lu["u_high"], float(a.high))
                gl.glUniform1f(lu["u_sparkle"], float(a.sparkle))
                gl.glUniform1f(lu["u_centroid"], float(a.centroid))
                gl.glUniform1f(lu["u_intro"], float(self._intro))
                gl.glUniform1f(lu["u_intro_active"], float(self._intro_active))
                gl.glUniform1f(lu["u_lyric_alpha"], float(self._lyric_alpha))

                ptr, count = lyric_vbuf.buffer_info()
                gl.glBindVertexArray(self._lyric_vao)
                gl.glBindBuffer(GL_ARRAY_BUFFER, self._lyric_vbo)
                gl.glBufferData(
                    GL_ARRAY_BUFFER,
                    count * lyric_vbuf.itemsize,
                    ctypes.c_void_p(ptr),
                    GL_STREAM_DRAW,
                )
                gl.glDrawArrays(GL_TRIANGLES, 0, lyric_vcount)
                gl.glBindBuffer(GL_ARRAY_BUFFER, 0)
                gl.glBindVertexArray(0)
                gl.glDisable(GL_BLEND)

            gl.glUseProgram(0)
            return True

        def _on_destroy(self, _w: Any) -> None:
            self._stop_tick()
            self._stop_wakeup_timer()
            self._hotkey.stop()
            self._grabber.stop()
            self._audio.stop()
            self._ipc.close()
            Gtk.main_quit()

    win = ScreenShaderWindow()
    _ = win
    Gtk.main()
    return 0


def main() -> int:
    cfg = _load_config()
    ap = argparse.ArgumentParser(description="ncm-lite 全屏流质 Shader 视觉叠加层")
    ap.add_argument("--active", action="store_true", help="启动时立即触发左下->右上液态玻璃入场")
    ap.add_argument("--big-lyrics", dest="big_lyrics", action="store_true", default=None, help="开启全屏大歌词显示")
    ap.add_argument("--no-big-lyrics", dest="big_lyrics", action="store_false", help="关闭全屏大歌词显示")
    ap.add_argument("--hotkey", default=str(cfg.get("hotkey", "<Ctrl><Alt>v")), help="全局切换流质视效热键")
    ap.add_argument(
        "--lyrics-hotkey",
        default=str(cfg.get("lyrics_hotkey", "<Ctrl><Alt>l")),
        help="全局切换全屏大歌词热键",
    )
    ap.add_argument(
        "--lookahead-ms",
        type=int,
        default=int(cfg.get("lookahead_ms", 75)),
        help="前瞻延迟补偿毫秒数（默认 75ms）",
    )
    ap.add_argument("--sock", default=None, help="自定义 IPC socket 路径")
    args = ap.parse_args()
    return _run_gui(
        active=args.active,
        big_lyrics=args.big_lyrics,
        hotkey=args.hotkey,
        lyrics_hotkey=args.lyrics_hotkey,
        lookahead_ms=args.lookahead_ms,
        sock_path=args.sock,
    )
