"""X11 底层无反馈屏幕采集器 (XComposite + XRender + MIT-SHM) 与全局热键监听器。"""
from __future__ import annotations

import ctypes
import ctypes.util
import threading
import time
from typing import Any, Callable

class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("c_class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class _XShmSegmentInfo(ctypes.Structure):
    _fields_ = [
        ("shmseg", ctypes.c_ulong),
        ("shmid", ctypes.c_int),
        ("shmaddr", ctypes.c_void_p),
        ("readOnly", ctypes.c_int),
    ]


class _XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("obdata", ctypes.c_void_p),
    ]


class _XTransform(ctypes.Structure):
    _fields_ = [("matrix", (ctypes.c_int * 3) * 3)]


class X11ScreenGrabber:
    """基于 XComposite + XRender + MIT-SHM 的无反馈底层屏幕实时采集器（自动排除自身叠加窗口）。"""

    IS_VIEWABLE = 2
    INPUT_OUTPUT = 1
    PICT_OP_SRC = 1
    PICT_OP_OVER = 3
    Z_PIXMAP = 2
    IPC_PRIVATE = 0
    IPC_CREAT = 0o1000
    IPC_RMID = 0
    CAPTURE_DIV = 2  # 1/2 分辨率硬件缩放采集（降低 75% SHM/内存带宽/纹理上传开销）

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._active_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._self_xid: int = 0
        self._mon_x: int = 0
        self._mon_y: int = 0
        self._width: int = 0
        self._height: int = 0

        self._cap_w: int = 0
        self._cap_h: int = 0
        self._front_buf: Any = None
        self._back_buf: Any = None
        self._frame_seq: int = 0
        self._uploaded_seq: int = 0
        self._err_cb: Any = None

    def configure(self, self_xid: int, x: int, y: int, width: int, height: int) -> None:
        with self._lock:
            self._self_xid = int(self_xid)
            self._mon_x = int(x)
            self._mon_y = int(y)
            self._width = max(1, int(width))
            self._height = max(1, int(height))

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._active_event.set()
        else:
            self._active_event.clear()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._active_event.set()

    def consume_frame(self) -> tuple[int, int, Any] | None:
        """若存在新帧则取出 (width, height, ctypes_bgra_buf)，零堆分配。"""
        with self._lock:
            if self._front_buf is None or self._frame_seq == self._uploaded_seq:
                return None
            self._uploaded_seq = self._frame_seq
            return self._cap_w, self._cap_h, self._front_buf

    def _run(self) -> None:
        try:
            x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
            xext = ctypes.CDLL(ctypes.util.find_library("Xext") or "libXext.so.6")
            xcomp = ctypes.CDLL(ctypes.util.find_library("Xcomposite") or "libXcomposite.so.1")
            xrender = ctypes.CDLL(ctypes.util.find_library("Xrender") or "libXrender.so.1")
            libc = ctypes.CDLL("libc.so.6")
        except Exception:  # noqa: BLE001
            return

        # 绑定 X11 / XComposite / XRender / MIT-SHM
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
        x11.XDefaultScreen.restype = ctypes.c_int
        x11.XDefaultVisual.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XDefaultVisual.restype = ctypes.c_void_p
        x11.XDefaultDepth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XDefaultDepth.restype = ctypes.c_int
        x11.XCreatePixmap.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        x11.XCreatePixmap.restype = ctypes.c_ulong
        x11.XFreePixmap.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XQueryTree.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x11.XGetWindowAttributes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_XWindowAttributes),
        ]
        x11.XFree.argtypes = [ctypes.c_void_p]
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]

        err_cb_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        self._err_cb = err_cb_t(lambda _d, _e: 0)
        x11.XSetErrorHandler.argtypes = [err_cb_t]
        x11.XSetErrorHandler.restype = ctypes.c_void_p
        x11.XSetErrorHandler(self._err_cb)

        xcomp.XCompositeNameWindowPixmap.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        xcomp.XCompositeNameWindowPixmap.restype = ctypes.c_ulong

        xrender.XRenderFindVisualFormat.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        xrender.XRenderFindVisualFormat.restype = ctypes.c_void_p
        xrender.XRenderCreatePicture.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
        ]
        xrender.XRenderCreatePicture.restype = ctypes.c_ulong
        xrender.XRenderFreePicture.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        xrender.XRenderComposite.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        xrender.XRenderSetPictureTransform.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_XTransform),
        ]
        xrender.XRenderSetPictureFilter.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_int,
        ]

        xext.XShmCreateImage.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(_XShmSegmentInfo),
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        xext.XShmCreateImage.restype = ctypes.POINTER(_XImage)
        xext.XShmAttach.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XShmSegmentInfo)]
        xext.XShmDetach.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XShmSegmentInfo)]
        xext.XShmGetImage.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_XImage),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        xext.XShmGetImage.restype = ctypes.c_int
        x11.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]

        libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
        libc.shmget.restype = ctypes.c_int
        libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        libc.shmat.restype = ctypes.c_void_p
        libc.shmdt.argtypes = [ctypes.c_void_p]
        libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]

        dpy = x11.XOpenDisplay(None)
        if not dpy:
            return

        root = x11.XDefaultRootWindow(dpy)
        scr = x11.XDefaultScreen(dpy)
        vis = x11.XDefaultVisual(dpy, scr)
        depth = x11.XDefaultDepth(dpy, scr)
        root_fmt = xrender.XRenderFindVisualFormat(dpy, vis)

        canvas_px = 0
        canvas_pic = 0
        shminfo = _XShmSegmentInfo()
        ximg = None
        shm_attached = False
        cur_w = 0
        cur_h = 0

        def cleanup_canvas() -> None:
            nonlocal canvas_px, canvas_pic, ximg, shm_attached
            if canvas_pic:
                xrender.XRenderFreePicture(dpy, canvas_pic)
                canvas_pic = 0
            if canvas_px:
                x11.XFreePixmap(dpy, canvas_px)
                canvas_px = 0
            if shm_attached:
                xext.XShmDetach(dpy, ctypes.byref(shminfo))
                shm_attached = False
            if ximg:
                x11.XDestroyImage(ximg)
                ximg = None
            if shminfo.shmaddr:
                libc.shmdt(shminfo.shmaddr)
                shminfo.shmaddr = None
            if shminfo.shmid >= 0:
                libc.shmctl(shminfo.shmid, self.IPC_RMID, None)
                shminfo.shmid = -1

        def resolve_toplevel_xid(target_xid: int) -> int:
            """沿着 XQueryTree 父链向上找到位于 root 下的顶层窗口（兼容 xfwm4 Frame Window）。"""
            if not target_xid:
                return 0
            cur = target_xid
            for _ in range(12):
                rr = ctypes.c_ulong()
                pr = ctypes.c_ulong()
                ch = ctypes.POINTER(ctypes.c_ulong)()
                nc = ctypes.c_uint()
                if not x11.XQueryTree(
                    dpy, cur, ctypes.byref(rr), ctypes.byref(pr), ctypes.byref(ch), ctypes.byref(nc)
                ):
                    break
                if ch:
                    x11.XFree(ch)
                if pr.value == 0 or pr.value == rr.value or pr.value == cur:
                    return cur
                cur = pr.value
            return target_xid

        shminfo.shmid = -1
        cached_self_xid = 0
        cached_toplevel_xid = 0
        fmt_cache: dict[int, int] = {}
        xform = _XTransform()
        scale_x = 1.0
        scale_y = 1.0

        try:
            while not self._stop.is_set():
                if not self._active_event.wait(timeout=0.25):
                    continue
                if self._stop.is_set():
                    break

                with self._lock:
                    w = self._width
                    h = self._height
                    mx = self._mon_x
                    my = self._mon_y
                    self_xid = self._self_xid

                if self_xid and self_xid != cached_self_xid:
                    cached_self_xid = self_xid
                    cached_toplevel_xid = resolve_toplevel_xid(self_xid)
                elif self_xid:
                    # 每次刷新一次以防 WM 延迟 reparent
                    cached_toplevel_xid = resolve_toplevel_xid(self_xid)

                if w <= 1 or h <= 1 or not root_fmt or not self_xid:
                    time.sleep(0.03)
                    continue

                cw = max(1, w // self.CAPTURE_DIV)
                ch_px = max(1, h // self.CAPTURE_DIV)

                if cw != cur_w or ch_px != cur_h:
                    cleanup_canvas()
                    canvas_px = x11.XCreatePixmap(dpy, root, cw, ch_px, depth)
                    canvas_pic = xrender.XRenderCreatePicture(dpy, canvas_px, root_fmt, 0, None)
                    ximg = xext.XShmCreateImage(
                        dpy, vis, depth, self.Z_PIXMAP, None, ctypes.byref(shminfo), cw, ch_px
                    )
                    if ximg:
                        size = ximg.contents.bytes_per_line * ch_px
                        shmid = libc.shmget(self.IPC_PRIVATE, size, self.IPC_CREAT | 0o600)
                        if shmid >= 0:
                            addr = libc.shmat(shmid, None, 0)
                            if addr and addr != ctypes.c_void_p(-1).value:
                                shminfo.shmid = shmid
                                shminfo.shmaddr = addr
                                shminfo.readOnly = 0
                                ximg.contents.data = addr
                                shm_attached = bool(xext.XShmAttach(dpy, ctypes.byref(shminfo)))
                                libc.shmctl(shmid, self.IPC_RMID, None)
                                shminfo.shmid = -1
                    cur_w, cur_h = cw, ch_px
                    scale_x = float(w) / float(cw)
                    scale_y = float(h) / float(ch_px)
                    fx = int(round(scale_x * 65536.0))
                    fy = int(round(scale_y * 65536.0))
                    xform.matrix[0][0] = fx
                    xform.matrix[0][1] = 0
                    xform.matrix[0][2] = 0
                    xform.matrix[1][0] = 0
                    xform.matrix[1][1] = fy
                    xform.matrix[1][2] = 0
                    xform.matrix[2][0] = 0
                    xform.matrix[2][1] = 0
                    xform.matrix[2][2] = 65536
                    tight_bytes = cw * ch_px * 4
                    with self._lock:
                        self._cap_w = cw
                        self._cap_h = ch_px
                        self._front_buf = (ctypes.c_ubyte * tight_bytes)()
                        self._back_buf = (ctypes.c_ubyte * tight_bytes)()

                if not (canvas_pic and shm_attached and ximg):
                    time.sleep(0.1)
                    continue

                t0 = time.monotonic()
                r_ret = ctypes.c_ulong()
                p_ret = ctypes.c_ulong()
                children = ctypes.POINTER(ctypes.c_ulong)()
                n = ctypes.c_uint()
                inv_sx = 1.0 / scale_x
                inv_sy = 1.0 / scale_y

                if x11.XQueryTree(
                    dpy,
                    root,
                    ctypes.byref(r_ret),
                    ctypes.byref(p_ret),
                    ctypes.byref(children),
                    ctypes.byref(n),
                ):
                    try:
                        for i in range(n.value):
                            win = children[i]
                            if win == self_xid or (cached_toplevel_xid and win == cached_toplevel_xid):
                                continue
                            attr = _XWindowAttributes()
                            if not x11.XGetWindowAttributes(dpy, win, ctypes.byref(attr)):
                                continue
                            if (
                                attr.map_state != self.IS_VIEWABLE
                                or attr.c_class != self.INPUT_OUTPUT
                                or attr.width <= 1
                                or attr.height <= 1
                            ):
                                continue
                            # 跳过完全在当前显示器范围外的窗口
                            if (
                                attr.x + attr.width <= mx
                                or attr.y + attr.height <= my
                                or attr.x >= mx + w
                                or attr.y >= my + h
                            ):
                                continue

                            px = xcomp.XCompositeNameWindowPixmap(dpy, win)
                            if not px:
                                continue
                            vis_key = int(attr.visual or 0)
                            fmt = fmt_cache.get(vis_key)
                            if fmt is None and vis_key:
                                fmt = int(xrender.XRenderFindVisualFormat(dpy, attr.visual) or 0)
                                fmt_cache[vis_key] = fmt
                            if fmt:
                                pic = xrender.XRenderCreatePicture(dpy, px, fmt, 0, None)
                                if pic:
                                    xrender.XRenderSetPictureTransform(dpy, pic, ctypes.byref(xform))
                                    xrender.XRenderSetPictureFilter(dpy, pic, b"bilinear", None, 0)
                                    op = self.PICT_OP_OVER if attr.depth == 32 else self.PICT_OP_SRC
                                    dx = int(round((attr.x - mx) * inv_sx))
                                    dy = int(round((attr.y - my) * inv_sy))
                                    dw = max(1, int(round(attr.width * inv_sx)))
                                    dh = max(1, int(round(attr.height * inv_sy)))
                                    xrender.XRenderComposite(
                                        dpy,
                                        op,
                                        pic,
                                        0,
                                        canvas_pic,
                                        0,
                                        0,
                                        0,
                                        0,
                                        dx,
                                        dy,
                                        dw,
                                        dh,
                                    )
                                    xrender.XRenderFreePicture(dpy, pic)
                            x11.XFreePixmap(dpy, px)
                    finally:
                        if children:
                            x11.XFree(children)

                ok = xext.XShmGetImage(dpy, canvas_px, ximg, 0, 0, 0xFFFFFFFF)
                x11.XSync(dpy, 0)
                if ok and shminfo.shmaddr and self._back_buf is not None:
                    bpl = ximg.contents.bytes_per_line
                    row_bytes = cur_w * 4
                    dst_base = ctypes.addressof(self._back_buf)
                    if bpl == row_bytes:
                        ctypes.memmove(dst_base, shminfo.shmaddr, row_bytes * cur_h)
                    else:
                        for ry in range(cur_h):
                            ctypes.memmove(
                                dst_base + ry * row_bytes,
                                shminfo.shmaddr + ry * bpl,
                                row_bytes,
                            )
                    with self._lock:
                        self._front_buf, self._back_buf = self._back_buf, self._front_buf
                        self._frame_seq += 1

                # 控制在 ~40Hz 采集，给 XServer 留足呼吸空间
                elapsed = time.monotonic() - t0
                if elapsed < 0.024:
                    time.sleep(0.024 - elapsed)
        finally:
            cleanup_canvas()
            x11.XCloseDisplay(dpy)



class _XKeyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xkey", _XKeyEvent), ("pad", ctypes.c_long * 24)]


class X11GlobalHotkey:
    """零依赖 X11 全局热键监听器（支持多热键绑定，默认 Ctrl+Alt+V / Ctrl+Alt+L）。"""

    KEY_PRESS = 2
    GRAB_MODE_ASYNC = 1
    MOD_SHIFT = 1 << 0
    MOD_LOCK = 1 << 1
    MOD_CTRL = 1 << 2
    MOD_ALT = 1 << 3
    MOD_NUM = 1 << 4
    MOD_SUPER = 1 << 6

    def __init__(
        self,
        hotkey_spec: str | None = None,
        on_trigger: Callable[[], None] | None = None,
        bindings: list[tuple[str, Callable[[], None]]] | None = None,
    ) -> None:
        self._bindings: list[tuple[str, Callable[[], None]]] = []
        if bindings:
            self._bindings.extend(bindings)
        elif hotkey_spec and on_trigger:
            self._bindings.append((hotkey_spec, on_trigger))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._err_cb: Any = None

    @classmethod
    def _parse_hotkey(cls, spec: str) -> tuple[int, str]:
        mods = 0
        s = spec.strip()
        while s.startswith("<"):
            end = s.find(">")
            if end < 0:
                break
            token = s[1:end].lower()
            if token in ("ctrl", "control", "primary"):
                mods |= cls.MOD_CTRL
            elif token in ("alt", "mod1"):
                mods |= cls.MOD_ALT
            elif token in ("shift",):
                mods |= cls.MOD_SHIFT
            elif token in ("super", "mod4", "win"):
                mods |= cls.MOD_SUPER
            s = s[end + 1 :]
        key = s.strip() or "v"
        return mods, key

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        x11_path = ctypes.util.find_library("X11") or "libX11.so.6"
        try:
            x11 = ctypes.CDLL(x11_path)
        except Exception:  # noqa: BLE001
            return

        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x11.XStringToKeysym.restype = ctypes.c_ulong
        x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XKeysymToKeycode.restype = ctypes.c_uint
        x11.XGrabKey.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        x11.XPending.argtypes = [ctypes.c_void_p]
        x11.XPending.restype = ctypes.c_int
        x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XEvent)]
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XFlush.restype = ctypes.c_int
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.restype = ctypes.c_int

        err_cb_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        self._err_cb = err_cb_type(lambda _d, _e: 0)
        x11.XSetErrorHandler.argtypes = [err_cb_type]
        x11.XSetErrorHandler.restype = ctypes.c_void_p
        x11.XSetErrorHandler(self._err_cb)

        dpy = x11.XOpenDisplay(None)
        if not dpy:
            return

        try:
            root = x11.XDefaultRootWindow(dpy)
            grabbed: list[tuple[int, int, Callable[[], None]]] = []
            for spec, cb in self._bindings:
                if not spec:
                    continue
                mods, key_name = self._parse_hotkey(spec)
                keysym = x11.XStringToKeysym(key_name.encode("ascii"))
                if not keysym:
                    continue
                keycode = x11.XKeysymToKeycode(dpy, keysym)
                if not keycode:
                    continue
                for extra in (0, self.MOD_NUM, self.MOD_LOCK, self.MOD_NUM | self.MOD_LOCK):
                    x11.XGrabKey(
                        dpy,
                        int(keycode),
                        mods | extra,
                        root,
                        0,
                        self.GRAB_MODE_ASYNC,
                        self.GRAB_MODE_ASYNC,
                    )
                grabbed.append((int(keycode), mods, cb))
            if not grabbed:
                return
            x11.XFlush(dpy)

            ev = _XEvent()
            while not self._stop.is_set():
                if x11.XPending(dpy) > 0:
                    x11.XNextEvent(dpy, ctypes.byref(ev))
                    if ev.type == self.KEY_PRESS:
                        clean_state = ev.xkey.state & ~(self.MOD_NUM | self.MOD_LOCK)
                        for keycode, mods, cb in grabbed:
                            if ev.xkey.keycode == keycode and (clean_state & mods) == mods:
                                cb()
                else:
                    time.sleep(0.03)
        finally:
            x11.XCloseDisplay(dpy)

