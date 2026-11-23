"""ncm-lite 桌面歌词低延迟 Unix Domain Socket 双向通信模块（仅标准库）。"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from typing import Any, Callable


def get_lyric_sock_path() -> str:
    """返回当前用户的桌面歌词 IPC Unix Socket 路径。"""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and os.path.isdir(runtime_dir):
        return os.path.join(runtime_dir, "ncm-lite-lyrics.sock")
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return os.path.join(tempfile.gettempdir(), f"ncm_lite_lyrics_{uid}.sock")


class LyricIpcServer:
    """运行在主播放器进程内的高实时歌词与播放状态广播服务端。"""

    def __init__(
        self,
        sock_path: str | None = None,
        on_command: Callable[[dict[str, Any]], None] | None = None,
        on_peaks_subscriber: Callable[[], None] | None = None,
    ) -> None:
        self.sock_path = sock_path or get_lyric_sock_path()
        self._on_command = on_command
        self._on_peaks_subscriber = on_peaks_subscriber
        self._lock = threading.Lock()
        self._clients: list[socket.socket] = []
        self._peak_clients: set[socket.socket] = set()
        self._last_track_bytes: bytes | None = None
        self._last_state_bytes: bytes | None = None
        self._closed = False
        self._server_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

    def start(self) -> None:
        if os.path.exists(self.sock_path):
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        try:
            os.chmod(self.sock_path, 0o600)
        except OSError:
            pass
        srv.listen(8)
        srv.settimeout(0.5)
        self._server_sock = srv
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def has_clients(self) -> bool:
        with self._lock:
            return bool(self._clients)

    def has_peak_clients(self) -> bool:
        with self._lock:
            return bool(self._peak_clients)

    def _accept_loop(self) -> None:
        srv = self._server_sock
        if srv is None:
            return
        while not self._closed:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(0.5)
            with self._lock:
                self._clients.append(conn)
                track_bytes = self._last_track_bytes
                state_bytes = self._last_state_bytes
            for payload in (track_bytes, state_bytes):
                if payload:
                    if not self._send_one(conn, payload):
                        self._drop_client(conn)
                        conn = None
                        break
            if conn is not None:
                threading.Thread(
                    target=self._client_read_loop, args=(conn,), daemon=True
                ).start()

    def _client_read_loop(self, conn: socket.socket) -> None:
        conn.settimeout(1.0)
        buf = bytearray()
        try:
            while not self._closed:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl]).strip()
                    del buf[: nl + 1]
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(msg, dict):
                        mtype = msg.get("type")
                        if mtype == "hello":
                            want_peaks = bool(msg.get("want_peaks", False))
                            added = False
                            with self._lock:
                                if want_peaks and conn in self._clients and conn not in self._peak_clients:
                                    self._peak_clients.add(conn)
                                    added = True
                                elif not want_peaks:
                                    self._peak_clients.discard(conn)
                            if added and self._on_peaks_subscriber:
                                try:
                                    self._on_peaks_subscriber()
                                except Exception:  # noqa: BLE001
                                    pass
                        elif self._on_command:
                            try:
                                self._on_command(msg)
                            except Exception:  # noqa: BLE001
                                pass
        finally:
            self._drop_client(conn)

    def _drop_client(self, conn: socket.socket) -> None:
        with self._lock:
            if conn in self._clients:
                self._clients.remove(conn)
            self._peak_clients.discard(conn)
        try:
            conn.close()
        except OSError:
            pass

    @staticmethod
    def _send_one(conn: socket.socket, data: bytes) -> bool:
        try:
            conn.sendall(data)
            return True
        except OSError:
            return False

    def _broadcast_bytes(self, data: bytes) -> None:
        with self._lock:
            targets = list(self._clients)
        if not targets:
            return
        dead: list[socket.socket] = []
        for conn in targets:
            if not self._send_one(conn, data):
                dead.append(conn)
        for conn in dead:
            self._drop_client(conn)

    def publish_track(self, track: dict[str, Any] | None, has_yrc: bool, lines: list[dict]) -> None:
        msg = {
            "type": "track",
            "track": {
                "id": track.get("id") if track else None,
                "name": track.get("name", "") if track else "",
                "artists": track.get("artists", "") if track else "",
            }
            if track
            else None,
            "has_yrc": bool(has_yrc),
            "lines": lines,
        }
        raw = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self._last_track_bytes = raw
        self._broadcast_bytes(raw)

    def publish_state(
        self,
        *,
        playing: bool,
        paused: bool,
        buffering: bool,
        time_pos: float,
        time_pos_at: float,
        duration: float,
        speed: float = 1.0,
        seq: int = 0,
    ) -> None:
        msg = {
            "type": "state",
            "playing": playing,
            "paused": paused,
            "buffering": buffering,
            "time_pos": time_pos,
            "time_pos_at": time_pos_at,
            "duration": duration,
            "speed": speed,
            "seq": seq,
        }
        raw = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self._last_state_bytes = raw
        self._broadcast_bytes(raw)

    def publish_peaks(
        self,
        *,
        track_id: int,
        fps: float,
        start_idx: int,
        frames: list[list[float]],
        reset: bool = False,
    ) -> None:
        """增量广播预计算的鼓点/频段前瞻时间轴（仅发给声明 want_peaks 的 Shader 客户端，零服务端副本堆积）。"""
        if not frames and not reset:
            return
        with self._lock:
            targets = list(self._peak_clients)
        if not targets:
            return
        msg = {
            "type": "peaks",
            "track_id": track_id,
            "fps": float(fps),
            "start": int(start_idx),
            "reset": bool(reset),
            "frames": frames,
        }
        raw = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        dead: list[socket.socket] = []
        for conn in targets:
            if not self._send_one(conn, raw):
                dead.append(conn)
        for conn in dead:
            self._drop_client(conn)

    def send_control(self, action: str) -> bool:
        if not self.has_clients():
            return False
        raw = (json.dumps({"type": "control", "action": action}, separators=(",", ":")) + "\n").encode("utf-8")
        self._broadcast_bytes(raw)
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
            self._peak_clients.clear()
        for c in clients:
            try:
                c.close()
            except OSError:
                pass
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None
        if os.path.exists(self.sock_path):
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass


class LyricIpcClient:
    """运行在桌面歌词进程内的自动重连 IPC 客户端。"""

    def __init__(
        self,
        on_message: Callable[[dict[str, Any]], None],
        on_connection_change: Callable[[bool], None] | None = None,
        sock_path: str | None = None,
        want_peaks: bool = False,
    ) -> None:
        self.sock_path = sock_path or get_lyric_sock_path()
        self._on_message = on_message
        self._on_connection_change = on_connection_change
        self._want_peaks = bool(want_peaks)
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._closed = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def send_command(self, action: str, value: Any = None) -> bool:
        msg: dict[str, Any] = {"type": "cmd", "action": action}
        if value is not None:
            msg["value"] = value
        raw = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            sock = self._sock
        if sock is None:
            return False
        try:
            sock.sendall(raw)
            return True
        except OSError:
            return False

    def _set_connected(self, connected: bool) -> None:
        if self._on_connection_change:
            try:
                self._on_connection_change(connected)
            except Exception:  # noqa: BLE001
                pass

    def _run_loop(self) -> None:
        backoff = 0.25
        while not self._closed:
            sock: socket.socket | None = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(1.5)
                sock.connect(self.sock_path)
                sock.settimeout(1.0)
                hello_raw = (
                    json.dumps({"type": "hello", "want_peaks": self._want_peaks}, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
                sock.sendall(hello_raw)
                with self._lock:
                    self._sock = sock
                backoff = 0.25
                self._set_connected(True)

                buf = bytearray()
                while not self._closed:
                    try:
                        chunk = sock.recv(8192)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf.extend(chunk)
                    while True:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        line = bytes(buf[:nl]).strip()
                        del buf[: nl + 1]
                        if not line:
                            continue
                        try:
                            msg = json.loads(line.decode("utf-8", "replace"))
                        except json.JSONDecodeError:
                            continue
                        if isinstance(msg, dict):
                            try:
                                self._on_message(msg)
                            except Exception:  # noqa: BLE001
                                pass
            except OSError:
                pass
            finally:
                with self._lock:
                    was_connected = self._sock is not None
                    self._sock = None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if was_connected:
                    self._set_connected(False)

            if self._closed:
                break
            time.sleep(backoff)
            backoff = min(1.5, backoff * 1.4)

    def close(self) -> None:
        self._closed = True
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
